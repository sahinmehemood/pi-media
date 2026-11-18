"""Core engine tests: policy, AIMD window, speed tracker, cache, flood budget."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_stream_buffer

sb = load_stream_buffer()


def test_policy_defaults_are_balanced():
    p = sb.build_policy(None)
    assert p.enabled and p.adaptive and p.profile == "balanced"
    assert (p.workers, p.readahead_mb, p.cache_mb, p.per_client_cap) == (4, 16, 48, 4)
    assert p.stall_timeout_s == 90 and p.max_flood_wait_s == 300
    assert p.readahead_chunks(1024 * 1024) == 16


def test_policy_profiles():
    c = sb.build_policy({"buffer_profile": "conservative"})
    assert (c.workers, c.readahead_mb, c.cache_mb) == (2, 8, 16)
    a = sb.build_policy({"buffer_profile": "aggressive"})
    assert (a.workers, a.readahead_mb, a.cache_mb) == (8, 32, 128)


def test_policy_custom_values_and_clamps():
    p = sb.build_policy({
        "buffer_profile": "custom", "buffer_workers": 99,
        "buffer_readahead_mb": 1, "buffer_cache_mb": -5,
        "buffer_stall_timeout": 99999,
    })
    assert p.workers == 16  # clamped to range max
    assert p.readahead_mb == 2  # clamped to range min
    assert p.cache_mb == 0
    assert p.stall_timeout_s == 600


def test_policy_rejects_bool_and_junk_numbers():
    p = sb.build_policy({"buffer_profile": "custom", "buffer_workers": True})
    assert p.workers == 4  # bool is not a setting; default applies
    p = sb.build_policy({"buffer_profile": "custom", "buffer_workers": "6"})
    assert p.workers == 6  # integer strings accepted
    p = sb.build_policy({"buffer_profile": "custom", "buffer_workers": "fast"})
    assert p.workers == 4


def test_policy_unknown_profile_is_custom():
    p = sb.build_policy({"buffer_profile": "turbo", "buffer_workers": 3})
    assert p.profile == "custom" and p.workers == 3


def test_readahead_chunks_bounds():
    p = sb.build_policy({"buffer_profile": "custom", "buffer_readahead_mb": 256})
    assert p.readahead_chunks(1024) == sb.MAX_READAHEAD_CHUNKS  # hard ceiling
    assert p.readahead_chunks(0) == 1
    assert sb.build_policy(None).readahead_chunks(4 * 1024 * 1024) == 4


def test_adaptive_window_grows_and_halves():
    w = sb.AdaptiveWindow(1, 4)
    w.note_success()
    assert w.window == 2
    for _ in range(2):
        w.note_success()
    assert w.window == 3
    w.note_flood()
    assert w.window == 1
    w.note_stall()
    assert w.window == 1  # floor


def test_adaptive_window_fixed_when_not_adaptive():
    w = sb.AdaptiveWindow(3, 3, adaptive=False)
    for _ in range(10):
        w.note_success()
    w.note_flood()
    assert w.window == 3


def test_speed_tracker_gating_and_multiplier():
    s = sb.SpeedTracker()
    assert s.multiplier() == 1.0  # no samples yet
    assert not s.observe(0) and not s.observe(-3)
    assert not s.observe(float("inf")) and not s.observe(float("nan"))
    assert not s.observe(5000)  # absurd for Telegram
    assert not s.observe("fast")
    assert s.observe(8.0)
    assert abs(s.mbps() - 8.0) < 1e-9 and abs(s.multiplier() - 1.0) < 1e-9
    assert s.observe(16.0)
    assert s.mbps() > 8.0 and 1.0 < s.multiplier() <= 1.5


def test_cache_lru_eviction_and_budget():
    c = sb.ChunkCache(10)
    c.put(("m", 1, 0), b"123456")
    c.put(("m", 1, 1), b"123456")  # evicts key 0 (LRU, over budget)
    assert c.get(("m", 1, 0)) is None
    assert c.get(("m", 1, 1)) == b"123456"
    st = c.stats()
    assert st["entries"] == 1 and st["size_bytes"] == 6 and st["hits"] == 1


def test_cache_oversize_and_disabled():
    c = sb.ChunkCache(4)
    c.put(("m", 1, 0), b"12345")  # larger than budget: never stored
    assert c.stats()["entries"] == 0
    c0 = sb.ChunkCache(0)
    c0.put(("m", 1, 0), b"x")
    assert c0.stats()["entries"] == 0


def test_cache_resize_evicts():
    c = sb.ChunkCache(12)
    c.put(("m", 1, 0), b"123456")
    c.put(("m", 1, 1), b"123456")
    c.resize(6)
    assert c.stats()["entries"] == 1 and c.stats()["size_bytes"] == 6


def test_flood_budget_charge_and_exhaustion():
    f = sb.FloodBudget(300)
    assert f.register_flood(0, 120)
    assert f.register_flood(1, 120)
    assert not f.register_flood(0, 120)  # 360 > 300 budget
    assert f.spent_s() == 240
    assert f.penalty(0) == 2 and f.penalty(1) == 1


def test_flood_budget_zero_means_never_wait():
    f = sb.FloodBudget(0)
    assert not f.register_flood(0, 1)


def test_flood_budget_shed_and_penalty_decay():
    f = sb.FloodBudget(300)
    f.register_flood(2, 10)
    assert f.penalty(2) == 1
    for _ in range(16):
        f.record_success(2)
    assert f.penalty(2) == 0  # decayed after 16 successes
    f.shed(2)
    assert f.is_shed(2) and f.shed_clients() == (2,)


def test_module_state_refresh_and_snapshot():
    sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 1})
    snap = sb.snapshot()
    assert snap["policy"]["profile"] == "custom"
    assert snap["cache"]["budget_bytes"] == 1024 * 1024
    assert sb.note_stream_speed(12.0)
    assert sb.snapshot()["speed_samples"] >= 1
    sb.refresh_policy(None)  # restore defaults for other tests


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} core tests passed")
