"""Regression tests: runtime buffer policy must follow persisted settings.

Defect under test:
    get_policy()/refresh_policy() called build_policy() with NO arguments.
    build_policy(data=None) substitutes data={} and returns hardcoded
    DEFAULTS, never the admin's persisted configuration.

User-visible symptom: the admin saves e.g. profile "aggressive", the settings
    page reports success, but streaming keeps using defaults until restart.

Runs without the application stack: stubs Backend.logger, pyrogram.errors,
    Backend.pyrofork.bot, and loads flood_wait + stream_buffer directly
    (same bootstrap pattern as test_stream_buffer.py).
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
    lg.LOGGER = logging.getLogger("policy_sync_test")
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


SAVED = {
    "buffer_enabled": True,
    "buffer_profile": "aggressive",
    "buffer_adaptive": False,
    "buffer_readahead_mb": 64,
    "buffer_workers": 6,
    "buffer_cache_mb": 128,
    "buffer_stall_timeout": 30,
}


@test("refresh_policy reflects saved aggressive profile")
def _():
    _reset()
    sb.refresh_policy(dict(SAVED))
    p = sb.get_policy()
    assert p.profile == "aggressive", f"expected aggressive, got {p.profile!r}"
    # A *named* profile owns readahead/workers/cache by design (see
    # test_named_profile_ignores_manual_numbers); adaptive and stall_timeout
    # are honoured alongside it.
    assert p.workers == sb.PROFILES["aggressive"]["workers"], (
        f"expected {sb.PROFILES['aggressive']['workers']} workers, got {p.workers}")
    assert p.adaptive is False, "expected adaptive off"
    assert p.stall_timeout == 30.0, f"expected 30s stall, got {p.stall_timeout}"


@test("custom profile honours saved numbers")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "custom", "buffer_readahead_mb": 64,
                       "buffer_workers": 6, "buffer_cache_mb": 128})
    p = sb.get_policy()
    assert p.readahead_mb == 64, p.readahead_mb
    assert p.workers == 6, p.workers
    assert p.cache_mb == 128, p.cache_mb


@test("disabled buffer is honoured from settings")
def _():
    _reset()
    p = sb.refresh_policy({"buffer_enabled": False})
    assert p.enabled is False, "disabled setting ignored"


@test("invalid persisted values are clamped, not crashing")
def _():
    _reset()
    p = sb.refresh_policy({"buffer_profile": "turbo", "buffer_readahead_mb": 99999,
                           "buffer_workers": -5, "buffer_cache_mb": "abc",
                           "buffer_stall_timeout": 0})
    assert p.profile in sb.PROFILES or p.profile == "custom", p.profile
    assert sb.MIN_READAHEAD_MB <= p.readahead_mb <= sb.MAX_READAHEAD_MB, p.readahead_mb
    assert sb.MIN_WORKERS <= p.workers <= sb.MAX_WORKERS, p.workers
    assert p.stall_timeout >= sb.MIN_STALL_TIMEOUT, p.stall_timeout


@test("changed cache budget resizes the live cache")
def _():
    _reset()
    # "custom" is required: named profiles own cache_mb by design.
    sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 16,
                       "buffer_readahead_mb": 8, "buffer_workers": 2})
    cache = sb.get_shared_cache()
    assert cache is not None, "cache not created"
    assert cache.budget_bytes == 16 * MiB, f"budget {cache.budget_bytes}"
    # Shrink via a settings change: the live cache must follow.
    sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 4,
                       "buffer_readahead_mb": 8, "buffer_workers": 2})
    assert cache.budget_bytes == 4 * MiB, f"resize ignored: {cache.budget_bytes}"
    # Disabling the cache must take effect at runtime too.
    sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 0,
                       "buffer_readahead_mb": 8, "buffer_workers": 2})
    assert sb.get_policy().cache_bytes == 0, "disabling cache did not take effect"


@test("repeat refresh never exposes partial policy state")
def _():
    _reset()
    for _i in range(50):
        p = sb.refresh_policy(dict(SAVED))
        assert p is not None
        assert p.profile
        assert p.readahead_bytes > 0
        assert isinstance(p.workers, int)


@test("STARTUP path: get_policy() reads persisted settings when _policy is None")
def _():
    """The startup path, which is the one the original defect broke.

    Every other test here calls refresh_policy(data) with explicit data, so
    they all pass even if get_policy() is reverted to `build_policy()` with no
    arguments. This test exercises the cold path: no explicit data, _policy
    unset, only persisted settings available. It is the regression test for
    "runtime policy silently falls back to defaults".
    """
    import sys as _sys
    _reset()
    assert sb._policy is None, "precondition: cold policy"

    persisted = {
        "buffer_enabled": True,
        "buffer_profile": "aggressive",
        "buffer_adaptive": False,
        "buffer_readahead_mb": 64,
        "buffer_workers": 6,
        "buffer_cache_mb": 128,
        "buffer_stall_timeout": 30,
    }

    class Snapshot:
        def to_dict(self):
            return dict(persisted)

    class PersistedManager:
        @classmethod
        def current(cls):
            return Snapshot()

    fake = types.ModuleType("Backend.helper.settings_manager")
    fake.SettingsManager = PersistedManager
    _sys.modules["Backend.helper.settings_manager"] = fake
    try:
        p = sb.get_policy()              # cold call, no explicit data
        assert p.profile == "aggressive", (
            f"startup ignored persisted profile: got {p.profile!r}")
        assert p.adaptive is False, "startup ignored persisted adaptive flag"
        assert p.stall_timeout == 30.0, (
            f"startup ignored persisted stall timeout: {p.stall_timeout}")
    finally:
        _sys.modules.pop("Backend.helper.settings_manager", None)
        sb._policy = None


@test("refresh after a failing settings read degrades to defaults, not an error")
def _():
    _reset()
    # Simulate the real failure mode: SettingsManager exists but current()
    # raises (e.g. a half-initialised manager). _persisted_settings must
    # swallow that and let build_policy fall back to safe defaults.
    import sys as _sys
    fake = types.ModuleType("Backend.helper.settings_manager")

    class BoomManager:
        @classmethod
        def current(cls):
            raise RuntimeError("simulated settings outage")

    fake.SettingsManager = BoomManager
    _sys.modules["Backend.helper.settings_manager"] = fake
    try:
        p = sb.refresh_policy()          # must not raise
        assert p is not None
        assert p.profile in sb.PROFILES
        assert p.readahead_bytes > 0
    finally:
        _sys.modules.pop("Backend.helper.settings_manager", None)


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1
