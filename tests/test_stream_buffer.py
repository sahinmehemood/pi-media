"""Unit tests for Backend/helper/stream_buffer.py.

Runs without the application stack: stubs ``Backend.logger``,
``pyrogram.errors.FloodWait``, ``Backend.pyrofork.bot``, and loads
``Backend.helper.flood_wait`` first (stream_buffer imports from it).

    python3 tests/test_stream_buffer.py     # plain asserts
    pytest tests/test_stream_buffer.py      # same tests, if pytest is around
"""

import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024


#----- Stub the minimal dependency graph ----------------------------------

def _setup():
    # Backend package + logger
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg

    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("stream_buffer_test")
    sys.modules["Backend.logger"] = logger_mod

    # pyrogram + pyrogram.errors.FloodWait
    if "pyrogram" not in sys.modules:
        pg = types.ModuleType("pyrogram")
        pg.__path__ = []
        pg.Client = type("Client", (), {})
        sys.modules["pyrogram"] = pg
    if "pyrogram.errors" not in sys.modules:
        errs = types.ModuleType("pyrogram.errors")

        class FloodWait(Exception):
            def __init__(self, value, message=""):
                self.value = value
                super().__init__(message or f"wait of {value} seconds")

        errs.FloodWait = FloodWait
        sys.modules["pyrogram.errors"] = errs

    # Backend.pyrofork.bot (used by flood_wait.client_index_of)
    bot_mod = types.ModuleType("Backend.pyrofork.bot")
    bot_mod.multi_clients = {}
    bot_mod.StreamBot = None
    bot_mod.Userbot = None
    sys.modules["Backend.pyrofork.bot"] = bot_mod

    # Load flood_wait under its real import path
    spec = importlib.util.spec_from_file_location(
        "Backend.helper.flood_wait",
        ROOT / "Backend" / "helper" / "flood_wait.py",
    )
    fw = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fw
    spec.loader.exec_module(fw)

    # Load stream_buffer under its real import path (imports flood_wait)
    spec = importlib.util.spec_from_file_location(
        "Backend.helper.stream_buffer",
        ROOT / "Backend" / "helper" / "stream_buffer.py",
    )
    sb = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sb
    spec.loader.exec_module(sb)

    return fw, sb


fw, sb = _setup()
handler = fw.flood_handler
PRIMARY_CLIENT = fw.PRIMARY_CLIENT


#----- Helpers -------------------------------------------------------------

def _reset():
    """Clear module singletons."""
    sb.SPEED.__init__()
    handler._penalties.clear()
    handler._flood_counts.clear()
    handler._last_success.clear()
    sb._cache = None
    sb.METRICS.__init__()
    sb._policy = None


def _make_policy(**overrides):
    data = {
        "buffer_enabled": True,
        "buffer_profile": "balanced",
        "buffer_adaptive": True,
        "buffer_readahead_mb": 12,
        "buffer_workers": 3,
        "buffer_cache_mb": 48,
        "buffer_stall_timeout": 90,
    }
    data.update(overrides)
    return sb.build_policy(data)


#----- Policy -------------------------------------------------------------

def test_defaults_are_balanced():
    _reset()
    policy = sb.build_policy({})
    assert policy.enabled is True
    assert policy.profile == "balanced"
    assert policy.readahead_mb == 12
    assert policy.workers == 3
    assert policy.cache_mb == 48


def test_named_profile_ignores_manual_numbers():
    _reset()
    policy = sb.build_policy({"buffer_profile": "aggressive"})
    assert (policy.readahead_mb, policy.workers, policy.cache_mb) == (24, 5, 96)


def test_custom_profile_honours_numbers_and_clamps():
    _reset()
    original = sb.ram_budget_bytes
    sb.ram_budget_bytes = lambda: 4096 * MiB
    try:
        policy = sb.build_policy({
            "buffer_profile": "custom",
            "buffer_readahead_mb": 999,
            "buffer_workers": 99,
            "buffer_cache_mb": 9999,
            "buffer_stall_timeout": 1,
        })
    finally:
        sb.ram_budget_bytes = original
    assert policy.readahead_mb == sb.MAX_READAHEAD_MB
    assert policy.workers == sb.MAX_WORKERS
    assert policy.cache_mb == sb.MAX_CACHE_MB
    assert policy.stall_timeout == sb.MIN_STALL_TIMEOUT


def test_unknown_profile_falls_back_to_balanced():
    _reset()
    policy = sb.build_policy({"buffer_profile": "turbo"})
    assert policy.profile == "balanced"


def test_ram_budget_ceiling_is_enforced():
    _reset()
    original = sb.ram_budget_bytes
    sb.ram_budget_bytes = lambda: 40 * MiB
    try:
        policy = sb.build_policy({
            "buffer_profile": "custom",
            "buffer_readahead_mb": 128,
            "buffer_cache_mb": 512,
            "buffer_workers": 3,
        })
    finally:
        sb.ram_budget_bytes = original
    assert policy.clamped is True
    assert policy.readahead_bytes + policy.cache_bytes <= 40 * MiB


def test_depth_chunks_maths():
    _reset()
    policy = sb.build_policy({"buffer_profile": "balanced"})
    assert policy.depth_chunks(1024 * 1024) == 12
    assert policy.depth_chunks(512 * 1024) == 24
    assert policy.depth_chunks(16 * MiB) == 1
    assert policy.depth_chunks(0) == 1


def test_disabled_policy_reports_disabled():
    _reset()
    policy = sb.build_policy({"buffer_enabled": False})
    assert policy.enabled is False


#----- Cache --------------------------------------------------------------

def test_cache_put_get_counts():
    _reset()
    cache = sb.ChunkCache(4 * MiB)
    assert cache.get((1, MiB, 0)) is None
    cache.put((1, MiB, 0), b"x" * MiB)
    assert cache.get((1, MiB, 0)) == b"x" * MiB
    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    assert stats["used_mb"] == 1.0


def test_cache_evicts_least_recently_used():
    _reset()
    cache = sb.ChunkCache(2 * MiB)
    cache.put((1, MiB, 0), b"a" * MiB)
    cache.put((1, MiB, 1), b"b" * MiB)
    cache.get((1, MiB, 0))              # chunk 0 is now most recently used
    cache.put((1, MiB, 2), b"c" * MiB)  # must evict chunk 1
    assert cache.get((1, MiB, 1)) is None
    assert cache.get((1, MiB, 0)) is not None


def test_cache_never_exceeds_budget():
    _reset()
    cache = sb.ChunkCache(1 * MiB)
    for index in range(50):
        cache.put((1, MiB, index), bytes(1) * MiB)
    assert cache.size_bytes <= MiB
    assert cache.entries == 1


def test_cache_rejects_oversized_chunk_and_zero_budget():
    _reset()
    cache = sb.ChunkCache(1 * MiB)
    cache.put((1, MiB, 0), bytes(2 * MiB))
    assert cache.entries == 0
    cache.put((1, MiB, 1), bytes(MiB))
    assert cache.entries == 1
    cache.resize(0)
    assert cache.entries == 0 and cache.budget_bytes == 0


#----- Cache key helpers --------------------------------------------------

def test_cache_key_rejects_junk():
    _reset()
    assert sb.cache_key("nope", MiB, 0) is None
    assert sb.cache_key(123, MiB, 4) == (123, MiB, 4)


def test_chunk_index_is_absolute_not_stream_relative():
    _reset()
    assert sb.chunk_index(0, MiB) == 0
    assert sb.chunk_index(500 * MiB, MiB) == 500
    assert sb.chunk_index(500 * MiB + MiB - 1, MiB) == 500
    assert sb.cache_key(7, MiB, sb.chunk_index(500 * MiB, MiB)) != \
        sb.cache_key(7, MiB, sb.chunk_index(0, MiB))
    assert sb.cache_key(7, MiB, sb.chunk_index(500 * MiB, MiB)) == \
        sb.cache_key(7, MiB, 500)
    assert sb.chunk_index(-5, MiB) == 0
    assert sb.chunk_index(10, 0) == 0


#----- Adaptive -----------------------------------------------------------

def test_adaptive_deepens_on_slow_link():
    _reset()
    sb.SPEED.observe(1.0)
    sb.SPEED.observe(1.0)
    assert sb.SPEED.multiplier() == 2.0
    policy = sb.build_policy({"buffer_profile": "balanced"})
    effective = sb.stream_policy(client_index=-1)
    assert effective.readahead_bytes == 2 * policy.readahead_bytes


def test_adaptive_relaxes_on_fast_link():
    _reset()
    for _ in range(5):
        sb.SPEED.observe(60.0)
    assert sb.SPEED.multiplier() == 0.75
    policy = sb.build_policy({"buffer_profile": "balanced"})
    assert sb.stream_policy(client_index=-1).readahead_bytes < policy.readahead_bytes


def test_adaptive_needs_two_samples():
    _reset()
    sb.SPEED.observe(0.5)
    assert sb.SPEED.multiplier() == 1.0


def test_stream_policy_uses_client_penalty():
    _reset()
    base = sb.build_policy({"buffer_profile": "balanced"})
    handler.note_flood_wait(0)
    assert handler.penalty(0) == 1
    p0 = sb.stream_policy(client_index=0)
    p1 = sb.stream_policy(client_index=1)
    assert p0.workers == base.workers - 1
    assert p1.workers == base.workers


#----- Snapshot -----------------------------------------------------------

def test_snapshot_shape():
    _reset()
    cache = sb.get_shared_cache()
    cache.put((1, MiB, 0), bytes(MiB))
    data = sb.snapshot()
    for key in ("enabled", "profile", "readahead_mb", "workers", "cache", "counters"):
        assert key in data
    assert data["cache"]["chunks"] == 1
    assert data["ram_budget_mb"] > 0


def main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 - test harness
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
