"""Wave 2 Phase B tests: Pre-Warm policy, warm plans, planner
budgets/dedupe, device gates, progress listener wiring, and the
adapter's pre-warm analytics skip."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import (FakeClient, FakeFileId, collect, load_buffered_streamer,
                     load_module, load_stream_buffer, run, settle)

sb = load_stream_buffer()
pw = load_module("prewarm_under_test", "Backend/helper/prewarm.py")
bs, stubs = load_buffered_streamer(sb)

MiB = pw.MiB
CHUNK = 1024
DATA = bytes((i * 7) % 256 for i in range(10 * CHUNK))
TODAY = date(2026, 11, 19)


def enabled(**over):
    data = {"prewarm_enabled": True}
    data.update(over)
    return pw.build_policy(data)


#----- Policy

def test_policy_defaults_are_off_and_smart():
    p = pw.build_policy(None)
    assert p.enabled is False
    assert p.profile == "smart"
    assert (p.head_mb, p.tail_mb) == (24, 8)
    assert p.trigger_pct == 70 and p.daily_budget_mb == 2048
    assert p.episodes_ahead == 1 and p.mb_per_file == 256
    assert p.only_charging is False and p.full_next is False


def test_policy_profiles_and_clamps():
    lite = enabled(prewarm_profile="lite")
    assert (lite.head_mb, lite.tail_mb, lite.ingest_head) == (8, 0, False)
    agg = enabled(prewarm_profile="aggressive")
    assert agg.full_next is True and agg.head_mb == 48
    bad = enabled(prewarm_profile="turbo")
    assert bad.profile == "smart"
    clamped = enabled(prewarm_trigger_pct=3, prewarm_episodes_ahead=9,
                      prewarm_mb_per_file=10 ** 9, prewarm_daily_budget_mb=-5)
    assert clamped.trigger_pct == 10
    assert clamped.episodes_ahead == 2
    assert clamped.mb_per_file == 4096
    assert clamped.daily_budget_mb == 0


#----- Warm plans

def test_warm_plan_head_and_tail():
    p = enabled(prewarm_profile="balanced")
    plan = pw.warm_plan(100 * MiB, p)
    assert plan == [(0, 24), (92 * MiB, 8)]
    assert pw.plan_mb(plan) == 32.0


def test_warm_plan_small_file_and_lite():
    p = enabled(prewarm_profile="balanced")
    # Smaller than head+tail: head only, no overlap games
    assert pw.warm_plan(30 * MiB, p) == [(0, 24)]
    # Tiny file: the whole file is the head
    assert pw.warm_plan(4 * MiB, p) == [(0, 4)]
    lite = enabled(prewarm_profile="lite")
    assert pw.warm_plan(100 * MiB, lite) == [(0, 8)]
    assert pw.warm_plan(0, p) == []


def test_warm_plan_aggressive_full_next_cap():
    p = enabled(prewarm_profile="aggressive", prewarm_mb_per_file=256)
    assert pw.warm_plan(500 * MiB, p) == [(0, 256)]
    assert pw.warm_plan(100 * MiB, p) == [(0, 100)]


#----- Planner: dedupe, budget, day roll, smart series cap

def _item(c, m, est=10.0, reason="pooled", series=""):
    return pw.WarmItem(c, m, 100 * MiB, est, reason=reason, series_key=series)


def test_planner_refuses_when_disabled_or_zero_budget():
    pl = pw.WarmPlanner()
    assert pl.offer(_item(1, 1), pw.build_policy(None), TODAY) is False
    assert pl.offer(_item(1, 1), enabled(prewarm_daily_budget_mb=0), TODAY) is False


def test_planner_dedupe_and_budget_hard_stop():
    pl = pw.WarmPlanner()
    p = enabled(prewarm_daily_budget_mb=100)
    assert pl.offer(_item(1, 1, est=60), p, TODAY) is True
    assert pl.offer(_item(1, 1, est=1), p, TODAY) is False  # same file again
    item = pl.next()
    pl.complete(item, 60)
    assert pl.offer(_item(1, 2, est=41), p, TODAY) is False  # 60+41 > 100
    assert pl.offer(_item(1, 2, est=40), p, TODAY) is True
    st = pl.stats(TODAY)
    assert st["used_mb_today"] == 60.0 and st["warmed_today"] == 1


def test_planner_day_roll_and_restore():
    pl = pw.WarmPlanner()
    p = enabled(prewarm_daily_budget_mb=100)
    assert pl.offer(_item(1, 1, est=90), p, TODAY) is True
    pl.complete(pl.next(), 90)
    tomorrow = TODAY + timedelta(days=1)
    assert pl.stats(tomorrow)["used_mb_today"] == 0.0
    assert pl.offer(_item(1, 1, est=90), p, tomorrow) is True  # new day: allowed again
    pl2 = pw.WarmPlanner()
    pl2.restore_usage(150.0, TODAY)
    assert pl2.offer(_item(2, 2, est=1), enabled(prewarm_daily_budget_mb=100), TODAY) is False


def test_planner_smart_series_cap():
    pl = pw.WarmPlanner()
    smart = enabled(prewarm_profile="smart")
    assert pl.offer(_item(1, 1, reason="next-episode", series="S1"), smart, TODAY) is True
    assert pl.offer(_item(1, 2, reason="next-episode", series="S1"), smart, TODAY) is False
    assert pl.offer(_item(1, 3, reason="next-episode", series="S2"), smart, TODAY) is True
    balanced = enabled(prewarm_profile="balanced")
    pl2 = pw.WarmPlanner()
    assert pl2.offer(_item(1, 1, reason="next-episode", series="S1"), balanced, TODAY) is True
    assert pl2.offer(_item(1, 2, reason="next-episode", series="S1"), balanced, TODAY) is True


#----- Device gates

def test_charging_state_reader():
    assert pw.charging_state(lambda path: "Charging\n") is True
    assert pw.charging_state(lambda path: "Full") is True
    assert pw.charging_state(lambda path: "Discharging") is False

    def boom(path):
        raise OSError("selinux")

    assert pw.charging_state(boom) is None


def test_mem_available_parser():
    text = "MemTotal:        8192000 kB\nMemAvailable:    2048000 kB\n"
    assert pw.mem_available_mb(lambda path: text) == 2000
    assert pw.mem_available_mb(lambda path: "nothing useful") is None


#----- Progress listener registry

def test_progress_listener_roundtrip_and_safety():
    seen = []
    sb.set_progress_listener(seen.append)
    try:
        sb.notify_progress({"ratio": 0.5})
        assert seen == [{"ratio": 0.5}]
        sb.set_progress_listener(lambda e: 1 / 0)
        sb.notify_progress({"ratio": 0.9})  # must not raise
    finally:
        sb.set_progress_listener(None)
    assert sb.get_progress_listener() is None


#----- Adapter integration: progress events + analytics skip

def _fresh():
    sb.refresh_policy(None)
    sb.get_cache().clear()
    # The adapter module is shared across test modules in one process;
    # assert against the FakeDB instance it actually imported.
    bs.db.logged.clear()


async def _stream(meta, stream_id, chunks=10):
    client = FakeClient(DATA)
    streamer = bs.BufferedByteStreamer(client, 0)
    gen = await streamer.prefetch_stream(
        FakeFileId(), 0, 0, 0, CHUNK, chunks, CHUNK,
        prefetch=2, parallelism=1, stream_id=stream_id,
        meta=meta, request=None, chat_id=-100999, message_id=77,
        extra_clients=[])
    out = await collect(gen)
    await settle()
    return out


def test_progress_events_fire_for_real_streams():
    async def main():
        _fresh()
        events = []
        sb.set_progress_listener(events.append)
        try:
            out = await _stream({"title": "Ep 1"}, "prog-stream")
        finally:
            sb.set_progress_listener(None)
        assert out == DATA
        assert len(events) >= 3
        ratios = [e["ratio"] for e in events]
        assert ratios == sorted(ratios) and ratios[0] >= 0.25
        assert all(e["chat_id"] == -100999 and e["msg_id"] == 77 for e in events)
        assert events[0]["title"] == "Ep 1"

    run(main())


def test_prewarm_streams_skip_analytics_and_progress():
    async def main():
        _fresh()
        events = []
        sb.set_progress_listener(events.append)
        try:
            await _stream({}, "real-stream")
            await _stream({"prewarm": True, "title": "pooled"}, "warm-stream")
        finally:
            sb.set_progress_listener(None)
        logged_ids = [e["stream_id"] for e in bs.db.logged]
        assert "real-stream" in logged_ids
        assert "warm-stream" not in logged_ids
        assert all(e["stream_id"] == "real-stream" for e in events)

    run(main())
