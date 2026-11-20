"""Phase 4 Workstream C2–C4 tests — fix store functions, the
test-mode engine, and the fix / fix-batch routes (phase4-plan §4.2,
§4.3, §4.5, §9.5; security T-4…T-8, A-2…A-4, W-5 point 4).

Reuses the Phase 3 harness (``test_librarian_api``) and its
production-shaped fakes, extended here with:

- ``extract_default_id`` on the metadata.common stub — a faithful
  reduction of the real function (metadata/common.py:290: bare
  ``tt…``, IMDb URL, TMDB URL, in that order), which the Phase 3
  stub did not need and the engine does.
- ``db.get_media_ids_by_part`` on the fake database — scriptable
  per (stripped_chat, msg_id), the real method's exact contract
  (database.py:1247: the (imdb, tmdb, media_type) triple or None).
- Exploding fake Telegram clients (T-4): they serve ``get_messages``
  from production-shaped messages and raise AssertionError on ANY
  other attribute access, recording every call — a fake that
  silently absorbs a mutation call would be a failed test double.
"""

import asyncio
import copy
import json
import logging
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_librarian_api as api  # noqa: E402  (installs the harness)

from _loader import load_module  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


# --- harness extensions (see module docstring) ---------------------

def _extract_default_id(text):
    if not text:
        return None
    bare_imdb = re.search(r"\b(tt\d{7,10})\b", text)
    if bare_imdb:
        return bare_imdb.group(1)
    imdb_url = re.search(r"/title/(tt\d+)", text)
    if imdb_url:
        return imdb_url.group(1)
    tmdb_url = re.search(r"/(?:movie|tv)/(\d+)", text)
    if tmdb_url:
        return tmdb_url.group(1)
    return None


sys.modules["Backend.helper.metadata.common"].extract_default_id = (
    _extract_default_id)

MEDIA_IDS = {}


async def _get_media_ids_by_part(channel, msg_id):
    return MEDIA_IDS.get((channel, msg_id))


api.fake_db.get_media_ids_by_part = _get_media_ids_by_part

store = api.store
routes = api.routes
ROUTE_MAP = api.ROUTE_MAP
FakeSettingsManager = api.FakeSettingsManager

engine = load_module("Backend.helper.librarian_engine",
                     "Backend/helper/librarian_engine.py")

WRITER_SENTINEL = "123456789:AAEwriter-sentinel_token-material-0123456789abcd"
CHAT = -100111
CAPTION = "Fixture Show S01E01 1080p"
NEW_CAPTION = ('Fixture Show S01E01 1080p\n'
               '<a href="https://www.imdb.com/title/tt1234567">IMDb</a>')


# --- fakes ----------------------------------------------------------

class ExplodingClient:
    """T-4 fake: get_messages only; anything else explodes."""

    def __init__(self, script=None):
        self._script = script or {}
        self.calls = []

    async def get_messages(self, chat_id, msg_id):
        self.calls.append(("get_messages", chat_id, msg_id))
        outcome = self._script.get((chat_id, msg_id),
                                   self._script.get("default"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __getattr__(self, name):
        raise AssertionError(
            f"exploding fake: unexpected attribute access {name!r} — "
            "the engine touched a non-read surface")


def _media(file_name="Fixture Show S01E01 1080p.mkv", file_id="fid-1"):
    return types.SimpleNamespace(
        file_id=file_id, file_name=file_name, file_size=2048,
        mime_type="video/x-matroska", file_unique_id="uniq-1")


def _message(caption=CAPTION, file_name="Fixture Show S01E01 1080p.mkv",
             entities=None, as_document=False):
    media = _media(file_name)
    return types.SimpleNamespace(
        caption=caption, caption_entities=entities or [],
        video=None if as_document else media,
        document=media if as_document else None)


def _reset():
    api._reset_api()
    engine._engine_ready = False
    MEDIA_IDS.clear()
    api.STUBS["bot"].Userbot = None
    api.STUBS["bot"].StreamBot = None


def _settings(**kw):
    base = {"librarian_enabled": True, "librarian_test_mode": True,
            "librarian_writes_enabled": False, "librarian_mode": "ask",
            "librarian_channels": ["-100111"],
            "librarian_channel_types": {"-100111": "tv"}}
    base.update(kw)
    return api._set_settings(**base)


def _seed_ledger(**kw):
    kw.setdefault("_id", api.ObjectId())
    row = api._ledger_row(caption=CAPTION, **kw)
    api._seed_ledger([row])
    return row


def _clients(userbot_script=None, bot_script=None):
    ub = ExplodingClient(userbot_script)
    bt = ExplodingClient(bot_script)
    api.STUBS["bot"].Userbot = ub
    api.STUBS["bot"].StreamBot = bt
    return ub, bt


def _payload(**kw):
    body = {"request_id": "req-0001", "chat_id": CHAT, "msg_id": 1,
            "expected_caption": CAPTION, "new_caption": NEW_CAPTION,
            "expected_ids": {"imdb": "tt1234567"},
            "intended": {"season": 1, "episode": 1}}
    body.update(kw)
    return body


def _fix_via_route(body):
    return api._call("POST", "/api/librarian/fix",
                     api._authed_req(body=body))


def _fixes_docs():
    return api.fake_db.dbs["tracking"]["librarian_fixes"].docs


def _ledger_docs():
    return api.fake_db.dbs["tracking"]["reject_ledger"].docs


# ------------------------------------------------------------------
# C2 — store functions
# ------------------------------------------------------------------

def _fix_store_row(**kw):
    row = {"request_id": "store-1", "batch_id": None,
           "ledger_id": api.ObjectId(),
           "source_locator": {"chat_id": CHAT, "msg_id": 1},
           "current_locator": {"chat_id": CHAT, "msg_id": 1},
           "stem": "fixture show", "chat_id": CHAT,
           "old_caption": "old cap", "old_caption_text": "old cap",
           "new_caption": "new cap", "new_caption_text": "new cap",
           "expected_ids": {"imdb": "tt1234567"}, "series_record_id": None,
           "action": "edit", "state": "prepared", "index_confirmed": False,
           "error": None, "budget_counted": False, "dry_run": True}
    row.update(kw)
    return row


def test_store_fix_insert_get_and_duplicate_backstop():
    _reset()

    async def go():
        # Production always ensures indexes before writes (router
        # startup / capture hook); the unique request_id index is
        # what makes the duplicate backstop real.
        await store.ensure_indexes()
        coll = api.fake_db.dbs["tracking"]["librarian_fixes"]
        assert any(unique and [k for k, _ in keys] == ["request_id"]
                   for keys, unique, _s, _t in coll.indexes)
        stored = await store.insert_fix(_fix_store_row())
        assert stored["request_id"] == "store-1"
        assert stored["created_at"] is not None
        assert stored["updated_at"] is not None
        # Duplicate request_id (a retry racing the first insert):
        # motor raises DuplicateKeyError and insert_fix re-reads and
        # returns the existing row — no second document. The raise
        # uses the class the store module itself imported: two
        # suites stub pymongo.errors with distinct classes and the
        # store binds the first-loaded one (a harness artefact —
        # production has exactly one pymongo), so raising through
        # the fake collection's own class would test the stub
        # mismatch, not the backstop.
        orig_insert = coll.insert_one

        async def racing_insert(doc):
            raise store.DuplicateKeyError(
                "E11000 duplicate key on ['request_id']")

        coll.insert_one = racing_insert
        try:
            again = await store.insert_fix(_fix_store_row(state="prepared"))
        finally:
            coll.insert_one = orig_insert
        assert again["_id"] == stored["_id"]
        assert len(_fixes_docs()) == 1
        got = await store.get_fix_by_request_id("store-1")
        assert got["state"] == "prepared"
        assert await store.get_fix_by_request_id("nope") is None

    _run(go())


def test_store_update_fix_state_fixed_key_set():
    _reset()

    async def go():
        await store.insert_fix(_fix_store_row())
        coll = api.fake_db.dbs["tracking"]["librarian_fixes"]
        # error left unchanged when the argument is not given.
        await store.update_fix_state("store-1", "editing")
        row = await store.get_fix_by_request_id("store-1")
        assert row["state"] == "editing" and row["error"] is None
        assert row["editing_at"] is not None
        # error set explicitly; then cleared by an explicit None.
        await store.update_fix_state("store-1", "edit_failed", error="boom")
        row = await store.get_fix_by_request_id("store-1")
        assert row["error"] == "boom"
        await store.update_fix_state("store-1", "dry_run_complete",
                                     error=None, index_confirmed=False)
        row = await store.get_fix_by_request_id("store-1")
        assert row["error"] is None and row["index_confirmed"] is False
        # A-2(i): every $set the function ever wrote touches only the
        # fixed key set — captions / ids / locators are unreachable.
        allowed = {"state", "updated_at", "error", "index_confirmed",
                   "editing_at", "edit_failed_at", "dry_run_complete_at"}
        for _filt, update, _upsert in coll.update_calls:
            assert set(update["$set"]) <= allowed, update
        assert row["old_caption"] == "old cap"
        assert row["new_caption"] == "new cap"
        assert row["expected_ids"] == {"imdb": "tt1234567"}

    _run(go())


def test_store_find_active_and_list_nonterminal():
    _reset()

    async def go():
        await store.insert_fix(_fix_store_row(request_id="a", state="prepared"))
        await store.insert_fix(_fix_store_row(
            request_id="b", state="dry_run_complete",
            current_locator={"chat_id": CHAT, "msg_id": 2}))
        await store.insert_fix(_fix_store_row(
            request_id="c", state="reindexing",
            current_locator={"chat_id": CHAT, "msg_id": 3}))
        active = await store.find_active_fix(CHAT, 1)
        assert active["request_id"] == "a"
        assert await store.find_active_fix(CHAT, 2) is None  # terminal
        assert await store.find_active_fix(CHAT, 99) is None
        states = {r["request_id"] for r in await store.list_nonterminal_fixes()}
        assert states == {"a", "c"}

    _run(go())


# ------------------------------------------------------------------
# Pure helpers: canonical form + classifier (DISP-4)
# ------------------------------------------------------------------

def test_canonical_from_html_fixture_pairs():
    # Mirrors caption_with_links on the message-space side: plain
    # text plus entity URLs, newline-joined, document order.
    html = ('Fixture Show\n'
            '<a href="https://www.imdb.com/title/tt1234567">IMDb</a> · '
            '<a href="https://www.themoviedb.org/tv/999">TMDB</a>')
    assert engine.canonical_from_html(html) == (
        "Fixture Show\nIMDb · TMDB\n"
        "https://www.imdb.com/title/tt1234567\n"
        "https://www.themoviedb.org/tv/999")
    assert engine.canonical_from_html("Just text") == "Just text"
    assert engine.canonical_from_html("") == ""
    msg = _message(caption="Fixture Show\nIMDb · TMDB", entities=[
        types.SimpleNamespace(url="https://www.imdb.com/title/tt1234567"),
        types.SimpleNamespace(url="https://www.themoviedb.org/tv/999")])
    from Backend.helper.metadata.common import caption_with_links
    assert engine.canonical_from_html(html) == caption_with_links(msg)


def test_classifier_mapping():
    classify = engine.classify_bot_api_error
    body429 = {"ok": False, "error_code": 429,
               "description": "Too Many Requests: retry after 30",
               "parameters": {"retry_after": 30}}
    assert classify(429, body429) == ("flood_wait", 30)
    assert classify(200, body429) == ("flood_wait", 30)  # error_code rules
    assert classify(429, {"ok": False, "error_code": 429}) == ("flood_wait", None)
    assert classify(400, {"description": "Bad Request: message can't be edited"}) == (
        "not_editable", None)
    assert classify(400, {"description": "Bad Request: message is not modified"}) == (
        "not_modified", None)
    assert classify(400, {"description": "Bad Request: message to edit not found"}) == (
        "not_found", None)
    assert classify(400, {"description": "Bad Request: MESSAGE_ID_INVALID"}) == (
        "not_found", None)
    assert classify(500, {"description": "Internal error"}) == ("edit_failed", None)
    assert classify(400, None) == ("edit_failed", None)
    assert classify(400, "garbage") == ("edit_failed", None)


def test_classifier_consumer_contract_batch_terminates():
    # §9.5's named test-double consumer: the contract the Phase 5
    # writer's batch driver will implement — a flood_wait
    # classification terminates the batch; later items never run.
    processed = []

    def driver(items, classify_outcomes):
        for item, outcome in zip(items, classify_outcomes):
            processed.append(item)
            if outcome[0] == "flood_wait":
                return {"terminated": "flood_wait",
                        "retry_after": outcome[1],
                        "remaining": items[len(processed):]}
        return {"terminated": None, "remaining": []}

    outcomes = [engine.classify_bot_api_error(200, {"error_code": 400}),
                engine.classify_bot_api_error(
                    429, {"error_code": 429,
                          "parameters": {"retry_after": 12}}),
                engine.classify_bot_api_error(200, {"error_code": 400})]
    result = driver(["i1", "i2", "i3"], outcomes)
    assert result == {"terminated": "flood_wait", "retry_after": 12,
                      "remaining": ["i3"]}
    assert processed == ["i1", "i2"]


# ------------------------------------------------------------------
# T-4 / T-6 — the dry run itself, against exploding fakes, asserted
# on persisted state
# ------------------------------------------------------------------

def test_T4_T6_dry_run_complete_exploding_fakes():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    ledger_before = copy.deepcopy(_seed_ledger())
    msg = _message()
    ub, bt = _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})

    resp = _fix_via_route(_payload())
    assert resp.status_code == 200, resp.content
    body = resp.content
    assert body["state"] == "dry_run_complete"
    assert body["would_be_action"] == "edit"
    assert body["dry_run"] is True and body["budget_counted"] is False
    # Reads only, both clients consulted (T-4): any other call would
    # have raised inside the fake and failed the run.
    assert ub.calls == [("get_messages", CHAT, 1)]
    assert bt.calls == [("get_messages", CHAT, 1)]
    # T-6: assertions on the persisted document, not the projection.
    docs = _fixes_docs()
    assert len(docs) == 1
    row = docs[0]
    assert row["state"] == "dry_run_complete"
    assert row["dry_run"] is True and row["budget_counted"] is False
    assert row["index_confirmed"] is False and row["error"] is None
    assert row["old_caption"] == CAPTION and row["old_caption_text"] == CAPTION
    assert row["new_caption"] == NEW_CAPTION
    assert row["new_caption_text"] == (
        "Fixture Show S01E01 1080p\nIMDb\n"
        "https://www.imdb.com/title/tt1234567")
    assert row["expected_ids"] == {"imdb": "tt1234567"}
    assert row["dry_run_complete_at"] is not None
    # The ledger row is byte-identical: a dry run never moves it.
    assert _ledger_docs() == [ledger_before]


def test_T5a_hostile_writes_on_auto_still_dry_run():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings(librarian_mode="auto", librarian_writes_enabled=True)

    async def seed_record():
        record = await store.upsert_record({
            "canonical_key": "fixture-show", "ids": {"tmdb": 999},
            "titles": ["Fixture Show"], "media_type": "tv",
            "numbering": {"source": "tmdb", "order": "aired",
                          "offset": None, "note": ""},
            "status": "verified", "related_entries": []})
        await store.set_alias(CHAT, "some show", record["_id"])
        return record

    record = _run(seed_record())
    _seed_ledger()
    msg = _message()
    ub, bt = _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    body = _payload(series_record_id=str(record["_id"]),
                    series_record_updated_at=record["updated_at"].isoformat())
    resp = _fix_via_route(body)
    assert resp.status_code == 200, resp.content
    assert resp.content["state"] == "dry_run_complete"
    row = _fixes_docs()[0]
    assert row["budget_counted"] is False and row["dry_run"] is True
    assert all(c[0] == "get_messages" for c in ub.calls + bt.calls)


def test_T5b_c_test_mode_off_refused_two_gates():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _seed_ledger()
    msg = _message()
    ub, bt = _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    # (b) test mode off, writes off: refused at the gate, no row.
    _settings(librarian_test_mode=False, librarian_writes_enabled=False)
    resp = _fix_via_route(_payload())
    assert resp.status_code == 200
    assert resp.content["state"] == "refused_precondition"
    assert "writes_enabled" in resp.content["error"]
    assert _fixes_docs() == []
    # (c) test mode off, writes on: still refused (no writer exists).
    _settings(librarian_test_mode=False, librarian_writes_enabled=True)
    resp = _fix_via_route(_payload(request_id="req-0002"))
    assert resp.content["state"] == "refused_precondition"
    assert "Phase 5" in resp.content["error"]
    assert _fixes_docs() == []
    assert ub.calls == [] and bt.calls == []  # gates precede clients (T-2)


# ------------------------------------------------------------------
# Step-1 refusals (no rows) + route 404
# ------------------------------------------------------------------

def test_step1_refusals_create_no_rows():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _clients({}, {})
    # Wrong status.
    _seed_ledger(status="parked")
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_wrong_status"
    assert "parked" in resp.content["error"]
    assert _fixes_docs() == []


def test_step1_channel_gates():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _clients({}, {})
    # Channel outside the allowlist.
    _settings(librarian_channels=["-100222"])
    _seed_ledger()
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_precondition"
    # Skip channel is never a target, even when allowlisted.
    _settings(librarian_channels=["-100111"], skip_channel="-100111")
    resp = _fix_via_route(_payload(request_id="req-0002"))
    assert resp.content["state"] == "refused_precondition"
    assert "never fix targets" in resp.content["error"]
    assert _fixes_docs() == []


def test_step1_auto_mode_requires_record_and_alias():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings(librarian_mode="auto")
    _seed_ledger()
    _clients({}, {})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_precondition"
    assert _fixes_docs() == []


def test_step1_record_conflicted_and_stale():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _clients({}, {})

    async def seed_record(status):
        return await store.upsert_record({
            "canonical_key": f"rec-{status}", "ids": {"tmdb": 5},
            "titles": ["Fixture Show"], "media_type": "tv",
            "numbering": {"source": "tmdb", "order": "aired",
                          "offset": None, "note": ""},
            "status": status, "related_entries": []})

    conflicted = _run(seed_record("conflicted"))
    resp = _fix_via_route(_payload(
        series_record_id=str(conflicted["_id"]),
        series_record_updated_at=conflicted["updated_at"].isoformat()))
    assert resp.content["state"] == "refused_record_conflicted"
    verified = _run(seed_record("verified"))
    resp = _fix_via_route(_payload(
        request_id="req-0002",
        series_record_id=str(verified["_id"]),
        series_record_updated_at="2020-01-01T00:00:00+00:00"))
    assert resp.content["state"] == "refused_record_conflicted"
    assert _fixes_docs() == []


def test_route_unknown_ledger_row_is_404():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _clients({}, {})
    resp = _fix_via_route(_payload())
    assert resp.status_code == 404
    assert _fixes_docs() == []


# ------------------------------------------------------------------
# Classification matrix (§4.3.5)
# ------------------------------------------------------------------

def _setup_found(msg=None, **settings_kw):
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings(**settings_kw)
    _seed_ledger()
    return msg


def test_classification_skip_copy_repost():
    _setup_found()
    _seed_skip = _ledger_docs()[0]
    _seed_skip["skip_locator"] = {"chat_id": -100777, "msg_id": 9}
    skip_msg = _message()
    _clients({}, {( -100777, 9): skip_msg, "default": None})
    # userbot misses everywhere; bot finds only the skip copy.
    api.STUBS["bot"].Userbot = ExplodingClient({})
    resp = _fix_via_route(_payload())
    assert resp.status_code == 200, resp.content
    assert resp.content["state"] == "dry_run_complete"
    assert resp.content["would_be_action"] == "repost"
    assert _ledger_docs()[0]["status"] == "waiting"


def test_classification_ghost_error_forms():
    _setup_found()
    _clients({}, {})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_missing_post"
    assert resp.content["error"] == "userbot: missing; bot: missing; skip: none"
    row = _fixes_docs()[0]
    assert row["state"] == "refused_missing_post" and row["dry_run"] is True
    assert row["budget_counted"] is False
    assert _ledger_docs()[0]["status"] == "waiting"  # never escalated in test mode


def test_classification_ghost_with_skip_missing():
    _setup_found()
    _ledger_docs()[0]["skip_locator"] = {"chat_id": -100777, "msg_id": 9}
    _clients({}, {})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_missing_post"
    assert resp.content["error"] == "userbot: missing; bot: missing; skip: missing"


def test_classification_error_is_edit_failed_never_ghost():
    _setup_found()
    flood = RuntimeError("FLOOD_WAIT_30: retry after 30")
    _clients({(CHAT, 1): flood}, {(CHAT, 1): RuntimeError("connection reset")})
    resp = _fix_via_route(_payload())
    # A probe-side flood is a probe error (DISP-4 point 4): edit_failed,
    # never the flood_wait state, and the engine does not sleep/retry.
    assert resp.content["state"] == "edit_failed"
    assert "FLOOD_WAIT_30" in resp.content["error"]
    assert _fixes_docs()[0]["state"] == "edit_failed"


def test_classification_one_found_one_error_proceeds():
    _setup_found()
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): RuntimeError("bot exploded")})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete"


def test_classification_two_found_disagreement_pin():
    _setup_found()
    ub_msg = _message(caption="Someone Elses Caption tt1234567")
    bot_msg = _message(caption=CAPTION)
    _clients({(CHAT, 1): ub_msg}, {(CHAT, 1): bot_msg})
    # Either read matching expected_caption proceeds (on that read).
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete"
    # Neither matching is drift.
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _clients({(CHAT, 1): ub_msg},
             {(CHAT, 1): _message(caption="Third Caption tt1234567")})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_drift"


# ------------------------------------------------------------------
# Exclusions, drift, caption-ID (§8 steps 4–5)
# ------------------------------------------------------------------

def test_exclusion_subtitle():
    _setup_found()
    sub = _message(file_name="Fixture Show S01E01.srt", as_document=True)
    _clients({(CHAT, 1): sub}, {(CHAT, 1): sub})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_subtitle"
    assert _fixes_docs()[0]["budget_counted"] is False


def test_exclusion_personal_negative_tmdb():
    _setup_found()
    MEDIA_IDS[(111, 1)] = ("tt1234567", -5, "movie")  # stripped chat 111
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_personal"


def test_E1_drifted_subtitle_refused_subtitle_not_drift():
    # DISP-9 / §6.1 regression: exclusion (step 4) precedes drift
    # (step 5). A subtitle whose caption has ALSO drifted is an
    # exclusion, not drift — on the pre-reorder code this returned
    # refused_drift.
    _setup_found()
    sub = _message(caption="Changed Caption tt1234567",
                   file_name="Fixture Show S01E01.srt", as_document=True)
    _clients({(CHAT, 1): sub}, {(CHAT, 1): sub})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_subtitle", resp.content
    assert _fixes_docs()[0]["state"] == "refused_subtitle"


def test_E1_drifted_personal_refused_personal_not_drift():
    # DISP-9 / §6.1 regression: a personal file (negative TMDB id
    # in the index) whose caption has ALSO drifted is an exclusion,
    # not drift — on the pre-reorder code this returned
    # refused_drift.
    _setup_found()
    MEDIA_IDS[(111, 1)] = ("tt1234567", -5, "movie")  # stripped chat 111
    drifted = _message(caption="Changed Caption tt1234567")
    _clients({(CHAT, 1): drifted}, {(CHAT, 1): drifted})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_personal", resp.content
    assert _fixes_docs()[0]["state"] == "refused_personal"


def test_E1_drifted_ordinary_still_refused_drift():
    # DISP-9 / §6.1 control: a drifted ordinary file is still drift
    # after the reorder — the reorder changes only exclusion
    # precedence, not the drift outcome for non-excluded files.
    _setup_found()
    drifted = _message(caption="Changed Caption tt1234567")
    _clients({(CHAT, 1): drifted}, {(CHAT, 1): drifted})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_drift", resp.content


def test_E1_exclusions_run_on_first_found_read():
    # DISP-9 first-found pin (Senior N-1): the file-name check runs
    # on the FIRST found read in probe order (userbot first), not
    # on the drift-selected read. Here the userbot read is a
    # drifted subtitle and the bot read is an ordinary file whose
    # caption matches — pre-reorder code selected the bot read and
    # completed; the reorder refuses on the first read's exclusion.
    _setup_found()
    sub = _message(caption="Changed Caption tt1234567",
                   file_name="Fixture Show S01E01.srt", as_document=True)
    ordinary = _message()
    _clients({(CHAT, 1): sub}, {(CHAT, 1): ordinary})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_subtitle", resp.content


def test_E1_second_read_subtitle_not_excluded_when_first_is_ordinary():
    # DISP-9 first-found pin, converse: exclusions do NOT run on
    # every found read. The userbot (first) read is an ordinary
    # drifted file; the bot read is a subtitle whose caption
    # matches. The exclusion check passes on the first read, drift
    # selection then proceeds on the surviving reads exactly as
    # Phase 4 pinned it and selects the matching bot read.
    _setup_found()
    ordinary_drifted = _message(caption="Changed Caption tt1234567")
    sub_matching = _message(file_name="Fixture Show S01E01.srt",
                            as_document=True)
    _clients({(CHAT, 1): ordinary_drifted}, {(CHAT, 1): sub_matching})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete", resp.content


def test_drift_and_caption_id_mismatch():
    _setup_found()
    drifted = _message(caption="Changed Caption tt1234567")
    _clients({(CHAT, 1): drifted}, {(CHAT, 1): drifted})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_drift"
    # Caption-ID: live matches, but new_caption carries the wrong ID.
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload(
        new_caption='Fixture Show\n<a href="https://www.imdb.com/title/tt9999999">IMDb</a>'))
    assert resp.content["state"] == "refused_caption_id_mismatch"
    # TMDB-URL form satisfies a tmdb-only expected_ids.
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload(
        expected_ids={"tmdb": 999},
        new_caption='Fixture Show\n<a href="https://www.themoviedb.org/tv/999">TMDB</a>'))
    assert resp.content["state"] == "dry_run_complete"


# ------------------------------------------------------------------
# T-7 — idempotency
# ------------------------------------------------------------------

def test_T7_idempotency_stored_result_wins():
    _setup_found()
    msg = _message()
    ub, bt = _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    first = _fix_via_route(_payload())
    assert first.content["state"] == "dry_run_complete"
    assert first.content["would_be_action"] == "edit"  # fresh classification
    calls_after_first = len(ub.calls) + len(bt.calls)
    # The stored result is the first body minus would_be_action:
    # the classification is not a §6 field, is not persisted, and
    # is therefore absent on replay — never synthesised from the
    # row's action.
    stored_body = {k: v for k, v in first.content.items()
                   if k != "would_be_action"}
    # Same request_id, same payload: stored result verbatim, no
    # re-execution, no synthesised would_be_action.
    second = _fix_via_route(_payload())
    assert "would_be_action" not in second.content
    assert second.content == stored_body
    # Same request_id, DIFFERENT payload: stored result still wins.
    third = _fix_via_route(_payload(new_caption="Totally Different tt7654321",
                                    expected_ids={"imdb": "tt7654321"}))
    assert third.content == stored_body
    assert len(_fixes_docs()) == 1
    assert len(ub.calls) + len(bt.calls) == calls_after_first


def test_T7_repost_classified_replay_does_not_fabricate_action():
    # A dry run classified as a REPOST (original post gone, skip
    # copy readable) persists a row whose §6 action honestly
    # records "edit" — the classification itself is not persisted.
    # A replay with the same request_id must return the stored
    # result verbatim: no second row, no re-execution, and NO
    # would_be_action at all — synthesising it from action would
    # report "edit", the one action that was impossible.
    _setup_found()
    _ledger_docs()[0]["skip_locator"] = {"chat_id": -100777, "msg_id": 9}
    skip_msg = _message()
    ub = ExplodingClient({})
    bt = ExplodingClient({(-100777, 9): skip_msg, "default": None})
    api.STUBS["bot"].Userbot = ub
    api.STUBS["bot"].StreamBot = bt
    first = _fix_via_route(_payload())
    assert first.content["state"] == "dry_run_complete"
    assert first.content["would_be_action"] == "repost"
    assert _fixes_docs()[0]["action"] == "edit"  # the §6 record
    calls_after_first = len(ub.calls) + len(bt.calls)
    assert calls_after_first > 0
    second = _fix_via_route(_payload())
    assert len(_fixes_docs()) == 1  # no second row
    assert len(ub.calls) + len(bt.calls) == calls_after_first  # no re-execution
    assert second.content["state"] == "dry_run_complete"
    assert second.content.get("would_be_action") != "edit"
    assert "would_be_action" not in second.content  # absent, not fabricated
    stored_body = {k: v for k, v in first.content.items()
                   if k != "would_be_action"}
    assert second.content == stored_body


def test_T7_refused_request_id_reevaluates():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger(status="parked")
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_wrong_status"
    assert _fixes_docs() == []  # a refusal leaves no phantom row
    _ledger_docs()[0]["status"] = "waiting"
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete"


# ------------------------------------------------------------------
# A-3 — route validation before any store access
# ------------------------------------------------------------------

def test_A3_validation_400_before_store_access():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _clients({}, {})
    calls = {"n": 0}
    orig_get_fix = store.get_fix_by_request_id
    orig_get_ledger = store.get_ledger_row

    async def counting_get_fix(request_id):
        calls["n"] += 1
        return await orig_get_fix(request_id)

    async def counting_get_ledger(chat_id, msg_id):
        calls["n"] += 1
        return await orig_get_ledger(chat_id, msg_id)

    store.get_fix_by_request_id = counting_get_fix
    store.get_ledger_row = counting_get_ledger
    try:
        for bad_id in ("", "x" * 65, "has space", "uniçode", "a/b", None, 42):
            resp = _fix_via_route(_payload(request_id=bad_id))
            assert resp.status_code == 400, (bad_id, resp.content)
        resp = api._call("POST", "/api/librarian/fix-batch",
                         api._authed_req(body={
                             "batch_id": "bad batch!", "items": [_payload()]}))
        assert resp.status_code == 400
        # Shape rules: paired record fields, intended shapes, TV rule.
        resp = _fix_via_route(_payload(series_record_id="a" * 24))
        assert resp.status_code == 400
        resp = _fix_via_route(_payload(intended={"season": 1}))
        assert resp.status_code == 400
        body = _payload()
        del body["intended"]
        resp = _fix_via_route(body)  # tv channel: intended required
        assert resp.status_code == 400
        resp = _fix_via_route(_payload(placement={"absolute": 3}))
        assert resp.status_code == 400  # placement without record pair
        resp = _fix_via_route(_payload(chat_id=True))
        assert resp.status_code == 400  # JSON bool is not an int
    finally:
        store.get_fix_by_request_id = orig_get_fix
        store.get_ledger_row = orig_get_ledger
    assert calls["n"] == 0, "validation must precede any store access"
    assert _fixes_docs() == []


# ------------------------------------------------------------------
# fix-batch: serial, per-item outcomes, atomic validation
# ------------------------------------------------------------------

def test_batch_serial_results_and_not_found_item():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _seed_ledger(msg_id=2)
    msg = _message()
    _clients({(CHAT, 1): msg, (CHAT, 2): msg},
             {(CHAT, 1): msg, (CHAT, 2): msg})
    items = [_payload(request_id="b-1", msg_id=1),
             _payload(request_id="b-2", msg_id=2),
             _payload(request_id="b-3", msg_id=99)]  # no ledger row
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "batch-1",
                                           "items": items}))
    assert resp.status_code == 200, resp.content
    body = resp.content
    assert body["batch_id"] == "batch-1" and body["serial"] is True
    states = [r["state"] for r in body["results"]]
    assert states == ["dry_run_complete", "dry_run_complete", "not_found"]
    assert body["results"][0]["request_id"] == "b-1"
    assert body["results"][0]["batch_id"] == "batch-1"
    assert body["results"][0]["would_be_action"] == "edit"
    assert len(_fixes_docs()) == 2
    assert all(r["batch_id"] == "batch-1" for r in _fixes_docs())
    # serial=false is echoed, execution is still serial (T-10).
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={
                         "batch_id": "batch-2", "serial": False,
                         "items": [_payload(request_id="b-4", msg_id=1)]}))
    assert resp.content["serial"] is False
    assert resp.content["results"][0]["state"] == "dry_run_complete"


def test_batch_validation_is_atomic():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _clients({}, {})
    items = [_payload(request_id="ok-1"),
             _payload(request_id="bad id!")]
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "batch-x",
                                           "items": items}))
    assert resp.status_code == 400
    assert _fixes_docs() == []  # the valid item was not persisted either
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "batch-x", "items": []}))
    assert resp.status_code == 400


# ------------------------------------------------------------------
# A-4 — budget zero-consumption proof (unit half)
# ------------------------------------------------------------------

def test_A4_budget_zero_consumption():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _seed_ledger(msg_id=2)
    _seed_ledger(msg_id=3, status="parked")
    msg = _message()
    drifted = _message(caption="Other Caption tt1234567")
    _clients({(CHAT, 1): msg, (CHAT, 2): drifted},
             {(CHAT, 1): msg, (CHAT, 2): drifted})

    async def budget():
        return await store.count_budget_used_today()

    before = _run(budget())
    rows_before = len(_fixes_docs())
    items = [_payload(request_id="a4-1", msg_id=1),          # completes
             _payload(request_id="a4-2", msg_id=2),          # drift refusal
             _payload(request_id="a4-3", msg_id=3),          # step-1 refusal
             _payload(request_id="a4-4", msg_id=99)]         # not found
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "a4-batch",
                                           "items": items}))
    assert resp.status_code == 200
    assert _run(budget()) == before
    created = _fixes_docs()[rows_before:]
    # Only the two post-persist outcomes created rows; step-1
    # refusals created zero.
    assert len(created) == 2
    assert all(r["budget_counted"] is False and r["dry_run"] is True
               for r in created)


# ------------------------------------------------------------------
# W-5 point 4 — the scrub: a token-bearing probe error reaches
# neither the response, the persisted record, nor the logs
# ------------------------------------------------------------------

def test_W5_4_scrub_secrets_unit():
    _reset()
    api._set_settings(librarian_writer_token=WRITER_SENTINEL)
    dirty = f"GET https://api.telegram.org/bot{WRITER_SENTINEL}/x failed"
    assert engine.scrub_secrets(dirty) == (
        "GET https://api.telegram.org/bot[writer-token]/x failed")
    assert engine.scrub_secrets(None) is None
    assert engine.scrub_secrets("clean") == "clean"


def test_W5_4_token_bearing_probe_error_is_scrubbed_everywhere():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings(librarian_writer_token=WRITER_SENTINEL)
    _seed_ledger()
    boom = RuntimeError(
        f"transport failed for https://api.telegram.org/bot{WRITER_SENTINEL}/getMe")
    _clients({(CHAT, 1): boom}, {(CHAT, 1): boom})
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    logging.getLogger("librarian").addHandler(handler)
    try:
        resp = _fix_via_route(_payload())
    finally:
        logging.getLogger("librarian").removeHandler(handler)
    assert resp.content["state"] == "edit_failed"
    assert WRITER_SENTINEL not in json.dumps(resp.content)
    assert "[writer-token]" in resp.content["error"]
    assert WRITER_SENTINEL not in json.dumps(_fixes_docs()[0], default=str)
    assert all(WRITER_SENTINEL not in line for line in records)


# ------------------------------------------------------------------
# Reconciliation (unit half of DISP-5) + the engine init guard
# ------------------------------------------------------------------

def _seed_fix_row(**kw):
    row = _fix_store_row(**kw)
    api.fake_db.dbs["tracking"]["librarian_fixes"].docs.append(
        copy.deepcopy(row))
    return row


def test_reconciliation_escalates_nonterminal():
    _reset()
    _settings()
    _seed_ledger()
    _seed_ledger(msg_id=2)
    _seed_ledger(msg_id=3)
    _seed_fix_row(request_id="r-prepared", state="prepared", dry_run=True)
    _seed_fix_row(request_id="r-editing", state="editing", dry_run=False,
                  budget_counted=True,
                  current_locator={"chat_id": CHAT, "msg_id": 2},
                  source_locator={"chat_id": CHAT, "msg_id": 2})
    _seed_fix_row(request_id="r-reindexing", state="reindexing", dry_run=False,
                  budget_counted=True,
                  current_locator={"chat_id": CHAT, "msg_id": 3},
                  source_locator={"chat_id": CHAT, "msg_id": 3})
    count = _run(engine.reconcile_on_startup())
    assert count == 3
    rows = {r["request_id"]: r for r in _fixes_docs()}
    for rid in ("r-prepared", "r-editing", "r-reindexing"):
        assert rows[rid]["state"] == "escalated"
        assert rows[rid]["error"] == engine.RESTART_ERROR
    # Dry-run row: flags survive; its ledger row is untouched (C-7).
    assert rows["r-prepared"]["dry_run"] is True
    assert rows["r-prepared"]["budget_counted"] is False
    ledger = {r["source_locator"]["msg_id"]: r for r in _ledger_docs()}
    assert ledger[1]["status"] == "waiting"
    # Real rows: ledger escalated with the same reason (§8).
    assert ledger[2]["status"] == "escalated"
    assert ledger[2]["escalation_reason"] == engine.RESTART_ERROR
    assert ledger[3]["status"] == "escalated"
    # Idempotent: a second pass reconciles nothing.
    assert _run(engine.reconcile_on_startup()) == 0


def test_engine_init_guard_reconciles_before_first_fix():
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    _seed_ledger()
    _seed_fix_row(request_id="stale-1", state="prepared", dry_run=True,
                  current_locator={"chat_id": CHAT, "msg_id": 55},
                  source_locator={"chat_id": CHAT, "msg_id": 55})
    engine._engine_ready = False
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete"
    rows = {r["request_id"]: r for r in _fixes_docs()}
    assert rows["stale-1"]["state"] == "escalated"
    assert rows["stale-1"]["error"] == engine.RESTART_ERROR


def test_E2_concurrent_first_fixes_reconcile_exactly_once():
    # DISP-10 / §6.2 regression: _ensure_engine_init must provide
    # single execution. N concurrent FIRST fix_one calls, with a
    # counting reconciliation double whose await yields (opening
    # the exact race window the unguarded check-then-await-then-set
    # had), must run reconciliation EXACTLY once. On the pre-fix
    # code every caller observes _engine_ready == False before any
    # of them sets it, so the count is N and this test fails.
    _reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _settings()
    n = 5
    for msg_id in range(1, n + 1):
        _seed_ledger(msg_id=msg_id)
    msg = _message()
    script = {(CHAT, i): msg for i in range(1, n + 1)}
    _clients(script, script)
    calls = {"n": 0}
    orig_reconcile = engine.reconcile_on_startup

    async def counting_reconcile():
        calls["n"] += 1
        await asyncio.sleep(0.05)  # yield: concurrent callers interleave here
        return 0

    engine.reconcile_on_startup = counting_reconcile
    engine._engine_ready = False
    try:
        async def go():
            return await asyncio.gather(*[
                engine.fix_one(_payload(request_id=f"e2-{i}", msg_id=i))
                for i in range(1, n + 1)])

        results = _run(go())
        assert calls["n"] == 1, calls
        assert all(r["state"] == "dry_run_complete" for r in results), results
        # Fast path after init: a later fix does not re-run it.
        later = _run(engine.fix_one(_payload(request_id="e2-after",
                                             msg_id=1)))
        assert later["state"] == "dry_run_complete"
        assert calls["n"] == 1, calls
    finally:
        engine.reconcile_on_startup = orig_reconcile


# ------------------------------------------------------------------
# Page context: fix_history in the C5 contract shape (§4.6)
# ------------------------------------------------------------------

def test_page_context_carries_fix_history():
    _setup_found()
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "dry_run_complete"
    ctx = api._page_ctx()
    history = ctx["fix_history"]
    assert len(history) == 1
    item = history[0]
    assert item["request_id"] == "req-0001"
    assert item["state"] == "dry_run_complete"
    assert item["dry_run"] is True
    assert item["time_ist"]  # IST-formatted string, not a raw datetime
    assert item["stem"] == "some show"
    assert item["old_caption_text"] == CAPTION
    assert item["new_caption_text"] == (
        "Fixture Show S01E01 1080p\nIMDb\n"
        "https://www.imdb.com/title/tt1234567")
