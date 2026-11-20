"""Librarian v2 Phase 0 smoke tests: the production-shaped harness itself
(SPEC §13) and the schema's load-bearing constraints (SPEC §3).

If these fail, every later v2 test built on the harness is suspect.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "librarian_v2"))
import harness  # noqa: E402


def _seed_series(store):
    c = store.conn
    now = harness.now()
    c.execute("INSERT INTO series (id, canonical_title, year, updated_at)"
              " VALUES (1, 'Perfect World', 2021, ?)", (now,))
    c.execute("INSERT INTO season (series_id, season_number) VALUES (1, 1)")
    c.execute("INSERT INTO episode (id, series_id, season_number,"
              " episode_number) VALUES (1, 1, 1, 1)")
    c.commit()


def _insert_file(store, fid, episode_id, is_primary):
    store.conn.execute(
        "INSERT INTO media_file (id, chat_id, msg_id, original_caption,"
        " current_caption, series_id, episode_id, is_primary, added_at,"
        " updated_at) VALUES (?, ?, ?, 'cap', 'cap', 1, ?, ?, ?, ?)",
        (fid, -1001, fid, episode_id, is_primary, harness.now(),
         harness.now()))


def test_store_schema_applies_and_wal_on():
    store = harness.StoreFixture()
    try:
        assert store.journal_mode() == "wal"
        tables = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("source_channel", "series", "series_alias", "season",
                     "episode", "media_file", "work_order", "plan",
                     "plan_item", "job", "caption_revision", "provider_cache",
                     "sync_state"):
            assert name in tables, name
    finally:
        store.close()


def test_unique_primary_slot_enforced():
    store = harness.StoreFixture()
    try:
        _seed_series(store)
        _insert_file(store, 101, 1, 1)
        store.conn.commit()
        try:
            _insert_file(store, 102, 1, 1)  # second primary for same slot
            store.conn.commit()
        except sqlite3.IntegrityError:
            store.conn.rollback()
        else:
            raise AssertionError("second primary file for one slot accepted")
        _insert_file(store, 103, 1, 0)      # a version is fine
        store.conn.commit()
    finally:
        store.close()


def test_unidentified_partial_index_exists():
    store = harness.StoreFixture()
    try:
        row = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
            " AND name='idx_media_unidentified'").fetchone()
        assert row is not None and "WHERE" in row[0].upper()
    finally:
        store.close()


def test_work_order_scope_check_constraint():
    store = harness.StoreFixture()
    try:
        _seed_series(store)
        now = harness.now()
        store.conn.execute(
            "INSERT INTO source_channel (id, telegram_id, title, created_at)"
            " VALUES (1, -1001, 'Ani', ?)", (now,))
        try:
            store.conn.execute(
                "INSERT INTO work_order (scope_kind, series_id,"
                " source_channel_id, created_at, updated_at)"
                " VALUES ('series', 1, 1, ?, ?)", (now, now))
            store.conn.commit()
        except sqlite3.IntegrityError:
            store.conn.rollback()
        else:
            raise AssertionError("order with two scopes accepted")
    finally:
        store.close()


def test_fake_mongo_handles_are_fresh():
    mongo = harness.FakeMongo()
    a = mongo.collection("reject_ledger")
    b = mongo.collection("reject_ledger")
    assert a is not b, "handles must never be is-identical (production shape)"
    a.insert_one({"_id": 1, "status": "open"})
    assert b.find_one({"_id": 1}) is not None  # data is shared


def test_fake_telegram_records_and_floods():
    tg = harness.FakeTelegram()
    tg.seed(-1001, 55, "old caption")
    assert tg.edit_caption(-1001, 55, "new caption")
    assert tg.edits == [(-1001, 55, "new caption")]
    assert tg.get_caption(-1001, 55) == "new caption"
    tg.flood_next(30)
    try:
        tg.edit_caption(-1001, 55, "x")
    except harness.FloodWait as fw:
        assert fw.retry_after == 30
    else:
        raise AssertionError("scripted flood wait did not raise")
    tg.drift(-1001, 55, "someone else edited")
    assert tg.get_caption(-1001, 55) == "someone else edited"


def test_provider_stub_outage_and_call_log():
    prov = harness.ProviderStub()
    prov.add_result("tmdb", "perfect world", [{"id": 1}])
    assert prov.search("tmdb", "perfect world") == [{"id": 1}]
    prov.set_outage("tmdb")
    try:
        prov.search("tmdb", "perfect world")
    except harness.ProviderDown:
        pass
    else:
        raise AssertionError("outage did not raise ProviderDown")
    assert prov.calls == [("tmdb", "perfect world")] * 2
