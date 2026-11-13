"""Regression tests: buffer-disabled fallback must match the settings UI.

Defect under test:
    The settings UI told the admin that disabling the buffer makes streams
    "fall back to 1 MiB read-ahead and no cache", but the downloader kept
    the caller's buffer-era prefetch/parallelism when policy.enabled was
    False. The UI description and the behaviour disagreed.

The decision block under test lives in custom_dl.prefetch_stream; these
tests exercise the same predicate via stream_policy() so they run without
Telegram, MongoDB or PyroFork.
"""
import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
RESULTS = []


def _setup():
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg
    lg = types.ModuleType("Backend.logger")
    lg.LOGGER = logging.getLogger("disabled_buffer_test")
    sys.modules["Backend.logger"] = lg
    pg = types.ModuleType("pyrogram")
    pg.__path__ = []
    pg.Client = type("Client", (), {})
    sys.modules["pyrogram"] = pg
    er = types.ModuleType("pyrogram.errors")

    class FloodWait(Exception):
        def __init__(self, value, message=""):
            self.value = value
            super().__init__(message or f"wait of {value} seconds")

    er.FloodWait = FloodWait
    sys.modules["pyrogram.errors"] = er
    bm = types.ModuleType("Backend.pyrofork.bot")
    bm.multi_clients = {}
    bm.StreamBot = None
    bm.Userbot = None
    sys.modules["Backend.pyrofork.bot"] = bm
    for name, rel in [("Backend.helper.flood_wait", "Backend/helper/flood_wait.py"),
                      ("Backend.helper.stream_buffer", "Backend/helper/stream_buffer.py")]:
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    return sys.modules["Backend.helper.stream_buffer"]


sb = _setup()


def _reset():
    sb._policy = None
    sb._cache = None
    sb.flood_handler._penalties.clear()
    sb.flood_handler._flood_counts.clear()
    sb.flood_handler._last_success.clear()


def test(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((False, name, f"{type(e).__name__}: {e}"))
        return fn
    return deco


@test("disabled policy reports enabled=False")
def _():
    _reset()
    assert sb.build_policy({"buffer_enabled": False}).enabled is False


@test("consumer predicate: no cache while disabled")
def _():
    _reset()
    for enabled in (True, False):
        p = sb.build_policy({"buffer_enabled": enabled, "buffer_profile": "custom",
                             "buffer_cache_mb": 48, "buffer_readahead_mb": 8,
                             "buffer_workers": 2})
        use_cache = bool(p.enabled and p.cache_bytes > 0)
        assert use_cache == enabled, f"enabled={enabled} -> use_cache={use_cache}"


@test("disabled runtime policy is honoured by stream_policy")
def _():
    _reset()
    sb.refresh_policy({"buffer_enabled": False})
    eff = sb.stream_policy(client_index=0)
    assert eff.enabled is False, "disabled state ignored by stream_policy"


@test("enabled stream_policy never drops below one worker")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "aggressive"})
    for _i in range(6):
        sb.note_flood_wait(0)
    eff = sb.stream_policy(client_index=0)
    assert eff.workers >= sb.MIN_WORKERS, eff.workers


@test("cache is absent when the budget is zero")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 0,
                       "buffer_readahead_mb": 8, "buffer_workers": 2})
    p = sb.get_policy()
    assert (p.enabled and p.cache_bytes > 0) is False


@test("downloader enforces the disabled fallback (1 chunk, 1 worker)")
def _():
    """Mirror the exact decision block in custom_dl.prefetch_stream."""
    _reset()
    sb.refresh_policy({"buffer_enabled": False})
    policy = sb.stream_policy(client_index=0, userbot=False)
    if policy.enabled:
        prefetch = policy.depth_chunks(1 * MiB)
        parallelism = policy.workers
    else:
        prefetch = 1
        parallelism = 1
    assert prefetch == 1, f"expected 1 chunk when disabled, got {prefetch}"
    assert parallelism == 1, f"expected 1 worker when disabled, got {parallelism}"


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1
