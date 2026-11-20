"""Librarian v2 Phase 3 tests: Work Orders end-to-end (SPEC §6) —
plan validation/tiers, scope enforcement, identity check, flood
snooze, budget pause, live progress, and full undo. Driven entirely
by the production-shaped harness fakes."""

import importlib
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "librarian_v2"))
import harness  # noqa: E402

_pkg = types.ModuleType("libv2pkg")
_pkg.__path__ = [str(ROOT / "Backend" / "librarian_v2")]
sys.modules["libv2pkg"] = _pkg
store_mod = importlib.import_module("libv2pkg.store")
jobs_mod = importlib.import_module("libv2pkg.jobs")
identify = importlib.import_module("libv2pkg.identify")
orders_mod = importlib.import_module("libv2pkg.orders")

T0 = 1_700_000_000
ANI_TG = -1004395609351


def _parse_caption(caption):
    m = re.search(r"S(\d+)E(\d+)", caption or "")
    if not m:
        return None
    return {"season": int(m.group(1)), "episode": int(m.group(2))}


class _Providers:
    """identify only needs the protocol; orders tests seed series directly."""
    def search(self, provider, query, kind, year):
        return []

    def lookup_by_id(self, provider, pid, kind):
        return None

    def episodes(self, provider, pid, kind):
        return []


def _setup(dry_run=False, budget=500):
    path = str(Path(tempfile.mkdtemp(prefix="libv2ord-")) / "t.sqlite3")
    store = store_mod.open_store(path)
    tg = harness.FakeTelegram()
    chan = store.upsert_channel(ANI_TG, "Ani", now=T0)
    sid = store.upsert_series("Steins;Gate", kind="tv", year=2011,
                              provider="tmdb", provider_id="7",
                              imdb_id="tt1910272", now=T0)
    store.replace_episode_list(sid, [
        {"season_number": 1, "episode_number": i, "absolute_number": i,
         "title": f"E{i}", "air_date": None} for i in (1, 2, 3)], T0)
    expected = {}

    def verify(file_row):
        return tg.get_caption(file_row["chat_id"], file_row["msg_id"]) == \
            expected.get((file_row["chat_id"], file_row["msg_id"]))

    ident = identify.Identifier(store, _Providers())
    svc = orders_mod.OrderService(
        store, ident, tg.edit_caption, tg.get_caption, verify,
        _parse_caption, dry_run=dry_run)
    return store, tg, svc, chan, sid, expected


def _add_file(store, tg, chan, msg, raw):
    fid = store.upsert_media_file(
        ANI_TG, msg, T0, original_caption=raw, current_caption=raw,
        source_channel_id=chan, location_status="located")
    tg.seed(ANI_TG, msg, raw)
    return fid


def _item(fid, sid, ep, score=0.95, caption=None, bits=None, special=None):
    b = bits if bits is not None else {"season": 1, "episode": ep}
    if special is not None:
        b = {**b, "special": special}
    return {"media_file_id": fid, "series_id": sid, "bits": b,
            "new_caption": caption or f"Steins;Gate - S01E{ep:02d}"
            " [tt1910272]",
            "base_score": score, "breakdown": {}}


def _drain(svc, store, now=T0):
    handlers = {"apply_caption": svc.make_apply_handler()}
    n = 0
    while jobs_mod.run_once(store, handlers, "w-test", now):
        n += 1
        if n > 50:
            raise AssertionError("job drain did not terminate")
    return n


def _open_series_order(svc, sid, budget=500):
    return svc.open_order("series", T0, series_id=sid,
                          budget_applies=budget)


def test_series_order_end_to_end():
    store, tg, svc, chan, sid, expected = _setup()
    fids = [_add_file(store, tg, chan, 100 + i, f"sg raw {i}")
            for i in (1, 2, 3)]
    oid = _open_series_order(svc, sid)
    items = [_item(f, sid, i) for i, f in enumerate(fids, start=1)]
    for it in items:
        expected[(ANI_TG, 100 + int(it["bits"]["episode"]))] = \
            it["new_caption"]
    pid, summary = svc.build_plan(oid, items, T0)
    assert summary == {"auto": 3, "approval": 0, "refused": 0}
    assert svc.apply_plan(oid, T0) == 3
    _drain(svc, store)
    for it in items:
        assert tg.get_caption(ANI_TG, 100 + it["bits"]["episode"]) == \
            it["new_caption"]
    row = store.get_series(sid)
    assert (row["file_count_cached"], row["missing_count_cached"]) == (3, 0)
    p = svc.progress(oid)
    assert p["planned"] == 3 and p["applied"] == 3 and p["percent"] == 100.0
    assert svc.maybe_finish_order(oid, T0)
    assert store.get_order(oid)["state"] == "done"


def test_approval_tier_blocks_until_approved():
    store, tg, svc, chan, sid, expected = _setup()
    fid = _add_file(store, tg, chan, 101, "sg raw 1")
    oid = _open_series_order(svc, sid)
    it = _item(fid, sid, 1, score=0.84)  # +0.03 fit = 0.87 -> approval tier
    expected[(ANI_TG, 101)] = it["new_caption"]
    pid, summary = svc.build_plan(oid, [it], T0)
    assert summary["approval"] == 1
    try:
        svc.apply_plan(oid, T0)
    except orders_mod.OrderError:
        pass
    else:
        raise AssertionError("unapproved approval-tier plan applied")
    svc.approve_plan(oid, pid, T0)
    assert svc.apply_plan(oid, T0) == 1
    _drain(svc, store)
    assert tg.get_caption(ANI_TG, 101) == it["new_caption"]


def test_plan_refusals():
    store, tg, svc, chan, sid, expected = _setup()
    f1 = _add_file(store, tg, chan, 101, "sg raw 1")
    f2 = _add_file(store, tg, chan, 102, "sg raw 2")
    f3 = _add_file(store, tg, chan, 103, "sg raw 3")
    store.replace_episode_list(sid, [
        {"season_number": 1, "episode_number": i, "absolute_number": i,
         "title": f"E{i}", "air_date": None} for i in (1, 2, 3)] + [
        {"season_number": 3, "episode_number": 1, "absolute_number": None,
         "title": "S3E1", "air_date": None}], T0)
    oid = _open_series_order(svc, sid)
    items = [
        _item(f1, sid, 1, caption="Steins;Gate - S01E09 [tt1910272]"),
        _item(f2, sid, 1, bits={"season": 3, "episode": 1}, special=3,
              caption="Steins;Gate - S03E01 [tt1910272]"),
        _item(f3, sid, 3, score=0.50),
    ]
    pid, summary = svc.build_plan(oid, items, T0)
    assert summary["refused"] == 3
    reasons = {i["media_file_id"]: i["refusal_reason"]
               for i in store.list_plan_items(pid)}
    assert reasons[f1] == "caption_parse_mismatch"
    assert reasons[f2] == "specials_guard"
    assert reasons[f3] == "below_approval_floor"


def test_channel_scope_enforced():
    store, tg, svc, chan, sid, expected = _setup()
    other = store.upsert_channel(-1003981572300, "Movie", now=T0)
    f_in = _add_file(store, tg, chan, 101, "sg raw 1")
    f_out = store.upsert_media_file(
        -1003981572300, 201, T0, original_caption="x", current_caption="x",
        source_channel_id=other, location_status="located")
    oid = svc.open_order("channel_unidentified", T0, channel_id=chan)
    items = [_item(f_in, sid, 1), _item(f_out, sid, 2)]
    pid, summary = svc.build_plan(oid, items, T0)
    reasons = {i["media_file_id"]: i["refusal_reason"]
               for i in store.list_plan_items(pid)}
    assert reasons[f_out] == "out_of_scope"
    assert reasons[f_in] is None


def test_identity_drift_blocks_edit():
    store, tg, svc, chan, sid, expected = _setup()
    fid = _add_file(store, tg, chan, 101, "sg raw 1")
    oid = _open_series_order(svc, sid)
    it = _item(fid, sid, 1)
    expected[(ANI_TG, 101)] = it["new_caption"]
    svc.build_plan(oid, [it], T0)
    svc.apply_plan(oid, T0)
    tg.drift(ANI_TG, 101, "someone else changed this")
    _drain(svc, store)
    assert tg.get_caption(ANI_TG, 101) == "someone else changed this"
    assert store.get_file(fid)["match_status"] == "conflicted"
    p = svc.progress(oid)
    assert p["failed"] == 1 and p["applied"] == 0


def test_flood_snoozes_then_completes():
    store, tg, svc, chan, sid, expected = _setup()
    fid = _add_file(store, tg, chan, 101, "sg raw 1")
    oid = _open_series_order(svc, sid)
    it = _item(fid, sid, 1)
    expected[(ANI_TG, 101)] = it["new_caption"]
    svc.build_plan(oid, [it], T0)
    svc.apply_plan(oid, T0)
    tg.flood_next(30)
    handlers = {"apply_caption": svc.make_apply_handler()}
    assert jobs_mod.run_once(store, handlers, "w-test", T0)
    job = store.conn.execute("SELECT * FROM job").fetchone()
    assert job["state"] == "snoozed" and job["attempts"] == 0
    assert jobs_mod.run_once(store, handlers, "w-test", T0 + 31)
    assert tg.get_caption(ANI_TG, 101) == it["new_caption"]


def test_budget_pauses_order():
    store, tg, svc, chan, sid, expected = _setup()
    fids = [_add_file(store, tg, chan, 100 + i, f"sg raw {i}")
            for i in (1, 2)]
    oid = _open_series_order(svc, sid, budget=1)
    items = [_item(f, sid, i) for i, f in enumerate(fids, start=1)]
    for it in items:
        expected[(ANI_TG, 100 + it["bits"]["episode"])] = it["new_caption"]
    svc.build_plan(oid, items, T0)
    svc.apply_plan(oid, T0)
    _drain(svc, store)
    assert store.get_order(oid)["state"] == "paused"
    p = svc.progress(oid)
    assert p["applied"] == 1 and p["failed"] == 1


def test_progress_mid_apply():
    store, tg, svc, chan, sid, expected = _setup()
    fids = [_add_file(store, tg, chan, 100 + i, f"sg raw {i}")
            for i in (1, 2, 3)]
    oid = _open_series_order(svc, sid)
    items = [_item(f, sid, i) for i, f in enumerate(fids, start=1)]
    for it in items:
        expected[(ANI_TG, 100 + it["bits"]["episode"])] = it["new_caption"]
    svc.build_plan(oid, items, T0)
    svc.apply_plan(oid, T0)
    handlers = {"apply_caption": svc.make_apply_handler()}
    assert jobs_mod.run_once(store, handlers, "w-test", T0)
    p = svc.progress(oid)
    assert p["applied"] == 1 and p["remaining"] == 2
    assert p["percent"] == 33.3 and p["budget_used"] == 1


def test_undo_order_restores_everything():
    store, tg, svc, chan, sid, expected = _setup()
    raws = {101: "sg raw 1", 102: "sg raw 2"}
    fids = [_add_file(store, tg, chan, m, r) for m, r in raws.items()]
    oid = _open_series_order(svc, sid)
    items = [_item(f, sid, i) for i, f in enumerate(fids, start=1)]
    for it in items:
        expected[(ANI_TG, 100 + it["bits"]["episode"])] = it["new_caption"]
    svc.build_plan(oid, items, T0)
    svc.apply_plan(oid, T0)
    _drain(svc, store)
    assert svc.undo_order(oid, T0) == 2
    for msg, raw in raws.items():
        assert tg.get_caption(ANI_TG, msg) == raw  # byte-identical restore
    row = store.get_series(sid)
    assert (row["file_count_cached"], row["missing_count_cached"]) == (0, 3)
    assert svc.progress(oid)["applied"] == 0


def test_dry_run_refuses_apply():
    store, tg, svc, chan, sid, expected = _setup(dry_run=True)
    fid = _add_file(store, tg, chan, 101, "sg raw 1")
    oid = _open_series_order(svc, sid)
    svc.build_plan(oid, [_item(fid, sid, 1)], T0)
    try:
        svc.apply_plan(oid, T0)
    except orders_mod.OrderError:
        pass
    else:
        raise AssertionError("dry_run order applied")
