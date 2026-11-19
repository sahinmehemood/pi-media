"""Wave 2 Phase A tests: DC speed table, flood alerter, disk tier,
policy disk budget, seek-burst window, stream source classifier."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_buffered_streamer, load_module, load_stream_buffer

sb = load_stream_buffer()
src = load_module("stream_source_under_test", "Backend/helper/stream_source.py")


#----- DcSpeedTable

def test_dc_speed_table_ewma_and_gating():
    t = sb.DcSpeedTable()
    assert t.mbps(0, 2) == 0.0
    assert t.observe(0, 2, 10.0) is True
    assert t.mbps(0, 2) == 10.0
    assert t.observe(0, 2, 20.0) is True
    assert abs(t.mbps(0, 2) - (0.3 * 20.0 + 0.7 * 10.0)) < 1e-9
    # Per-(client, DC) isolation
    assert t.mbps(0, 5) == 0.0 and t.mbps(1, 2) == 0.0
    # Gating: absurd / non-positive samples rejected
    assert t.observe(0, 2, 0.0) is False
    assert t.observe(0, 2, -3.0) is False
    assert t.observe(0, 2, 99999.0) is False
    assert t.entries() == 1


#----- FloodAlerter

def test_flood_alerter_immediate_then_batched():
    a = sb.FloodAlerter(min_wait_s=10.0, batch_window_s=600.0)
    # Below the threshold: never reported
    assert a.note(0, 5.0, now=1000.0) == []
    # First qualifying event after a quiet period: immediate
    out = a.note(0, 30.0, "Movie", now=1000.0)
    assert len(out) == 1 and out[0]["kind"] == "flood"
    assert out[0]["client_index"] == 0 and out[0]["wait_s"] == 30.0
    # Inside the window: batched, nothing returned
    assert a.note(1, 20.0, now=1100.0) == []
    assert a.note(1, 45.0, now=1200.0) == []
    assert a.pending_count() == 2
    # After the window: summary of the batch, then the new event
    out = a.note(2, 15.0, now=1700.0)
    assert [e["kind"] for e in out] == ["flood_summary", "flood"]
    assert out[0]["count"] == 2 and out[0]["max_wait_s"] == 45.0
    assert out[0]["clients"] == [1]
    assert a.pending_count() == 0


def test_flood_sender_registry_roundtrip():
    seen = []

    async def sender(event):
        seen.append(event)

    sb.set_flood_sender(sender)
    try:
        assert sb.get_flood_sender() is sender
        import asyncio
        asyncio.run(sb.publish_flood_event({"kind": "flood"}))
        assert seen == [{"kind": "flood"}]
    finally:
        sb.set_flood_sender(None)
    assert sb.get_flood_sender() is None


#----- DiskTier + ChunkCache integration

def test_disk_tier_spill_promote_and_stats():
    with tempfile.TemporaryDirectory() as root:
        disk = sb.DiskTier(root, 3 * 1024)
        cache = sb.ChunkCache(1024, disk=disk)
        k = lambda n: ("media", 1024, ("chunk", n))
        cache.put(k(0), b"a" * 1024)
        cache.put(k(1), b"b" * 1024)  # evicts k(0) -> spills to disk
        cache.put(k(2), b"c" * 1024)  # evicts k(1) -> spills to disk
        stats = cache.stats()
        assert stats["disk"]["entries"] == 2, stats
        assert stats["disk"]["size_bytes"] == 2048
        # Memory miss -> disk hit, promoted back into memory
        assert cache.get(k(0)) == b"a" * 1024
        assert cache.stats()["disk"]["hits"] == 1
        assert cache.get(k(9)) is None


def test_disk_tier_survives_reload_and_drops_corruption():
    with tempfile.TemporaryDirectory() as root:
        key = ("media", 1024, ("chunk", 7))
        d1 = sb.DiskTier(root, 8192)
        d1.put(key, b"x" * 512)
        # A fresh tier on the same root rehydrates from the manifest
        d2 = sb.DiskTier(root, 8192)
        assert d2.get(key) == b"x" * 512
        # Corrupt the chunk file: size mismatch -> silent miss, entry dropped
        name = sb.DiskTier._name_for(key)
        with open(Path(root) / name, "wb") as fh:
            fh.write(b"short")
        d3 = sb.DiskTier(root, 8192)
        assert d3.get(key) is None
        assert d3.stats()["entries"] == 0


def test_disk_tier_budget_zero_is_inert():
    with tempfile.TemporaryDirectory() as root:
        disk = sb.DiskTier(root, 0)
        disk.put(("m", 1, ("chunk", 0)), b"z" * 100)
        assert disk.get(("m", 1, ("chunk", 0))) is None
        assert disk.stats()["entries"] == 0
        # Enabling then shrinking to 0 evicts everything
        disk.resize(4096)
        disk.put(("m", 1, ("chunk", 0)), b"z" * 100)
        assert disk.stats()["entries"] == 1
        disk.resize(0)
        assert disk.stats()["entries"] == 0


#----- Policy: disk cache budget

def test_policy_disk_cache_mb():
    p = sb.build_policy(None)
    assert p.disk_cache_mb == 512
    assert p.disk_cache_bytes() == 512 * 1024 * 1024
    assert sb.build_policy({"buffer_disk_cache_mb": 128}).disk_cache_mb == 128
    assert sb.build_policy({"buffer_disk_cache_mb": 99999}).disk_cache_mb == 8192
    assert sb.build_policy({"buffer_disk_cache_mb": 0}).disk_cache_mb == 0
    assert p.to_dict()["disk_cache_mb"] == 512


#----- Seek burst window

def test_initial_window_seek_burst():
    bs, _stubs = load_buffered_streamer(sb)
    sb.refresh_policy(None)  # balanced, adaptive
    policy = sb.get_policy()
    initial, maximum, burst = bs._initial_window(policy, 0)
    assert burst is False and initial < maximum
    initial, maximum, burst = bs._initial_window(policy, 5 * 1024 * 1024)
    assert burst is True and initial == maximum
    # The settings toggle (buffer_seek_burst) disables the burst
    sb.refresh_policy({"buffer_seek_burst": False})
    policy = sb.get_policy()
    assert policy.seek_burst is False
    initial, maximum, burst = bs._initial_window(policy, 5 * 1024 * 1024)
    assert burst is False and initial < maximum
    sb.refresh_policy(None)


#----- Stream source classifier

def _fake_settings():
    return SimpleNamespace(
        auth_channels=["-100111"],
        global_search_channels=[],
        manual_channels=[],
        anime_channels=["-100222"],
        announcement_channel="",
        skip_channel="",
        dl_channel_id=-100333,
        channel_titles={"-100111": "Auth Movies", "-100333": "My DL Pool"},
    )


def test_classify_source_labels():
    ctx = src.build_source_context(_fake_settings())
    assert src.classify_source(-100333, ctx) == {"label": "My DL Pool", "kind": "dl"}
    assert src.classify_source(-100111, ctx) == {"label": "Auth Movies", "kind": "auth"}
    anime = src.classify_source(-100222, ctx)
    assert anime["kind"] == "anime" and "Anime" in anime["label"]
    assert src.classify_source(8800546676, ctx) == {"label": "DM · Global", "kind": "global"}
    assert src.classify_source(-100999, ctx) == {"label": "-100999", "kind": "unknown"}
    assert src.classify_source(None, ctx)["kind"] == "unknown"
    assert src.classify_source("junk", ctx)["kind"] == "unknown"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} wave-2 engine tests passed")
