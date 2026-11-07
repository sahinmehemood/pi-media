"""Unit tests for Backend/helper/stream_buffer.py.

Runs without the application stack: the ``Backend`` package would pull in
pymongo/pyrogram on import, so the package and its logger are stubbed and the
module is loaded straight from its file path.

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


def _load_module():
    if "Backend" not in sys.modules:
        package = types.ModuleType("Backend")
        package.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = package

        logger_module = types.ModuleType("Backend.logger")
        logger_module.LOGGER = logging.getLogger("stream_buffer_test")
        sys.modules["Backend.logger"] = logger_module

    spec = importlib.util.spec_from_file_location(
        "stream_buffer_under_test", ROOT / "Backend" / "helper" / "stream_buffer.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sb = _load_module()


def _reset(policy=None):
    """Clear module singletons; pin an explicit policy instead of reading settings."""
    sb.SPEED.__init__()
    sb._flood_penalty = 0
    sb._cache = None
    sb.METRICS.__init__()
    sb._policy = policy if policy is not None else sb.build_policy({})
    return sb._policy


#----- Policy -------------------------------------------------------------

def test_defaults_are_balanced():
    policy = sb.build_policy({})
    assert policy.enabled is True
    assert policy.profile == "balanced"
    assert policy.readahead_mb == 12
    assert policy.workers == 3
    assert policy.cache_mb == 48
    assert policy.depth_chunks(MiB) == 12


def test_named_profile_ignores_manual_numbers():
    policy = sb.build_policy({"buffer_profile": "aggressive", "buffer_readahead_mb": 4})
    assert (policy.readahead_mb, policy.workers, policy.cache_mb) == (24, 5, 96)


def test_custom_profile_honours_numbers_and_clamps():
    original = sb.ram_budget_bytes
    sb.ram_budget_bytes = lambda: 4096 * MiB   # isolate the hard limits from the RAM ceiling
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
    assert policy.clamped is False


def test_unknown_profile_falls_back_to_balanced():
    policy = sb.build_policy({"buffer_profile": "turbo"})
    assert policy.profile == "balanced"


def test_ram_budget_ceiling_is_enforced():
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
    policy = sb.build_policy({"buffer_profile": "balanced"})
    assert policy.depth_chunks(1024 * 1024) == 12
    assert policy.depth_chunks(512 * 1024) == 24
    assert policy.depth_chunks(16 * MiB) == 1
    assert policy.depth_chunks(0) == 1


def test_disabled_policy_reports_disabled():
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
    assert cache.size_bytes == 2 * MiB


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


def test_cache_resize_shrinks_in_place():
    _reset()
    cache = sb.ChunkCache(4 * MiB)
    for index in range(4):
        cache.put((1, MiB, index), bytes(MiB))
    cache.resize(2 * MiB)
    assert cache.size_bytes <= 2 * MiB


def test_cache_key_rejects_junk():
    assert sb.cache_key("nope", MiB, 0) is None
    assert sb.cache_key(123, MiB, 4) == (123, MiB, 4)


def test_chunk_index_is_absolute_not_stream_relative():
    #----- Stream A starts at 0, stream B starts at chunk 500 of the same file.
    #----- Their first chunks must not share a cache slot.
    assert sb.chunk_index(0, MiB) == 0
    assert sb.chunk_index(500 * MiB, MiB) == 500
    assert sb.chunk_index(500 * MiB + MiB - 1, MiB) == 500
    assert sb.cache_key(7, MiB, sb.chunk_index(500 * MiB, MiB)) != sb.cache_key(7, MiB, sb.chunk_index(0, MiB))
    assert sb.cache_key(7, MiB, sb.chunk_index(500 * MiB, MiB)) == sb.cache_key(7, MiB, 500)
    assert sb.chunk_index(-5, MiB) == 0
    assert sb.chunk_index(10, 0) == 0


#----- Adaptive -----------------------------------------------------------

def test_adaptive_deepens_on_slow_link():
    policy = _reset(sb.build_policy({"buffer_profile": "balanced"}))
    sb.SPEED.observe(1.0)
    sb.SPEED.observe(1.0)
    assert sb.SPEED.multiplier() == 2.0
    effective = sb.stream_policy()
    assert effective.readahead_bytes == 2 * policy.readahead_bytes
    assert effective.readahead_bytes <= policy.max_readahead_bytes


def test_adaptive_relaxes_on_fast_link():
    policy = _reset(sb.build_policy({"buffer_profile": "balanced"}))
    for _ in range(5):
        sb.SPEED.observe(60.0)
    assert sb.SPEED.multiplier() == 0.75
    assert sb.stream_policy().readahead_bytes < policy.readahead_bytes


def test_adaptive_needs_two_samples():
    _reset()
    sb.SPEED.observe(0.5)
    assert sb.SPEED.multiplier() == 1.0


def test_flood_penalty_sheds_workers_then_recovers():
    base = _reset(sb.build_policy({"buffer_profile": "balanced"}))
    assert sb.stream_policy().workers == base.workers
    sb.note_flood_wait()
    assert sb.stream_policy().workers == base.workers - 1
    for _ in range(10):
        sb.note_flood_wait()
    assert sb.stream_policy().workers == sb.MIN_WORKERS
    for _ in range(sb.FLOOD_PENALTY_MAX):
        sb.record_stream_speed(10.0)
    assert sb.stream_policy().workers == base.workers


def test_userbot_workers_are_capped():
    policy = _reset(sb.build_policy({"buffer_profile": "aggressive"}))
    assert policy.workers == 5
    assert sb.stream_policy(userbot=True).workers == sb.USERBOT_MAX_WORKERS


def test_disabled_policy_is_not_adapted():
    base = _reset(sb.build_policy({"buffer_enabled": False}))
    sb.SPEED.observe(0.2)
    sb.SPEED.observe(0.2)
    effective = sb.stream_policy()
    assert effective.enabled is False
    assert effective.readahead_bytes == base.readahead_bytes


#----- Snapshot -----------------------------------------------------------

def test_snapshot_shape():
    _reset()
    sb.get_shared_cache().put((1, MiB, 0), bytes(MiB))
    data = sb.snapshot()
    for key in ("enabled", "profile", "readahead_mb", "workers", "cache", "counters"):
        assert key in data
    assert data["cache"]["chunks"] == 1
    assert data["ram_budget_mb"] > 0


def _main():
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - test harness
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
