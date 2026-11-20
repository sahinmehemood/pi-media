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


def test_T5b_test_mode_off_writes_gate_then_real_fix():
    # Phase 4's part (c) asserted a refusal here ("no writer exists
    # in this build"); Phase 5 A-2 removes that premise — with the
    # writer landed, test mode off + writes on is the REAL path.
    # Part (b) is unchanged: writes off still refuses at the gate.
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
    assert ub.calls == [] and bt.calls == []  # gates precede clients (T-2)
    # (c) test mode off, writes on: the real fix runs (A-2 doubles).
    _setup_real(msg=msg)
    resp = _fix_via_route(_payload(request_id="req-0002"))
    assert resp.status_code == 200, resp.content
    assert resp.content["state"] == "fixed"
    assert resp.content["dry_run"] is False
    assert resp.content["budget_counted"] is True


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


# ------------------------------------------------------------------
# A-2 — the real path: the Bot API writer + §8 steps 6–9
# (Phase 5 plan §2, DISP-1/DISP-2). The writer is doubled at the
# transport seam (engine._bot_api_edit_caption) with scripted REAL
# Bot API envelope shapes; the REAL transport (URL, JSON body,
# token hygiene, no-chain) is exercised against a local HTTP
# server in test_librarian_writer.py. The re-index seam is doubled
# through the REAL bridge module (DISP-3 contract: fresh result
# dicts per call, the (imdb, tmdb, media_type) triple, filed in
# the form it was given).
# ------------------------------------------------------------------

bridge = load_module("Backend.helper.librarian_reindex",
                     "Backend/helper/librarian_reindex.py")
sys.modules["Backend.helper"].librarian_reindex = bridge

NEW_CAPTION_TEXT = engine.canonical_from_html(NEW_CAPTION)
IMDB_URL = "https://www.imdb.com/title/tt1234567"
OK_ENVELOPE = (200, {"ok": True, "result": {"message_id": 1}})


class FakeWriter:
    """Scripted transport double: records every call; each outcome
    is a (status, body) tuple or an exception to raise. An
    unscripted call defaults to the success envelope — tests assert
    exact call counts, so a hidden extra call still fails them."""

    def __init__(self, outcomes=(), on_call=None):
        self.outcomes = list(outcomes)
        self.calls = []
        self.on_call = on_call

    async def __call__(self, chat_id, msg_id, caption_html):
        self.calls.append((chat_id, msg_id, caption_html))
        if self.on_call is not None:
            self.on_call(len(self.calls), chat_id, msg_id, caption_html)
        outcome = self.outcomes.pop(0) if self.outcomes else OK_ENVELOPE
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSeam:
    """DISP-3 bridge double: fresh result dict per call."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    async def __call__(self, client, message, *, placement=None,
                       expected_ids=None, series_ids=None,
                       future_timeout=120):
        self.calls.append({"client": client, "message": message,
                           "placement": placement,
                           "expected_ids": expected_ids,
                           "series_ids": series_ids,
                           "future_timeout": future_timeout})
        if self.results:
            return copy.deepcopy(self.results.pop(0))
        return {"state": "indexed", "ids": ("tt1234567", None, "tv"),
                "filed": {"season": 1, "episode": 1}, "error": None}


class SequencedClient(ExplodingClient):
    """get_messages answers from a per-locator sequence (the last
    answer repeats) — for re-probe tests whose second read must
    differ from the first."""

    def __init__(self, sequences=None):
        super().__init__()
        self._seq = {k: list(v) for k, v in (sequences or {}).items()}

    async def get_messages(self, chat_id, msg_id):
        self.calls.append(("get_messages", chat_id, msg_id))
        seq = self._seq.get((chat_id, msg_id)) or self._seq.get("default")
        if not seq:
            return None
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _real_settings(**kw):
    base = {"librarian_test_mode": False, "librarian_writes_enabled": True,
            "librarian_writer_token": WRITER_SENTINEL,
            "librarian_edit_budget_per_day": 10}
    base.update(kw)
    return _settings(**base)


def _setup_real(msg=None, writer_outcomes=(), seam_results=(),
                on_writer_call=None, seed=True, **settings_kw):
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings(**settings_kw)
    if seed:
        _seed_ledger()
    msg = msg if msg is not None else _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    writer = FakeWriter(writer_outcomes, on_call=on_writer_call)
    engine._bot_api_edit_caption = writer
    seam = FakeSeam(seam_results)
    bridge.register(seam)
    return msg, writer, seam


def _set_live_caption(msg, caption_text, url=None):
    """Mutate a fake message so caption_with_links() yields the
    given canonical text (plain text + the entity URL line)."""
    if url:
        msg.caption = caption_text[: -len(url)].rstrip("\n")
        msg.caption_entities = [types.SimpleNamespace(url=url)]
    else:
        msg.caption = caption_text
        msg.caption_entities = []


def _apply_then_restore(msg):
    """Writer on_call: call 1 lands the new caption, call 2 (the
    restore) lands the old one — the live re-reads then confirm."""
    def on_call(n, chat_id, msg_id, caption_html):
        if n == 1:
            _set_live_caption(msg, NEW_CAPTION_TEXT, IMDB_URL)
        elif n == 2:
            _set_live_caption(msg, CAPTION)
    return on_call


def _budget_used():
    return _run(store.count_budget_used_today())


# --- A-ACC-2: the full success walk ---------------------------------

def test_A2_real_fix_walks_prepared_editing_reindexing_fixed():
    msg, writer, seam = _setup_real()
    resp = _fix_via_route(_payload())
    assert resp.status_code == 200, resp.content
    body = resp.content
    assert body["state"] == "fixed"
    assert body["index_confirmed"] is True
    assert body["dry_run"] is False and body["budget_counted"] is True
    assert body["error"] is None
    row = _fixes_docs()[0]
    assert row["state"] == "fixed"
    assert row["budget_counted"] is True and row["dry_run"] is False
    for stamp in ("prepared_at", "editing_at", "reindexing_at", "fixed_at"):
        assert row[stamp] is not None, stamp
    # Exactly one edit, carrying the payload's HTML caption verbatim.
    assert writer.calls == [(CHAT, 1, NEW_CAPTION)]
    # The seam was called once, through the bridge, with the bot
    # client's post-edit message object and the contract kwargs.
    assert len(seam.calls) == 1
    call = seam.calls[0]
    assert call["client"] is api.STUBS["bot"].StreamBot
    assert call["message"] is msg
    assert call["placement"] is None
    assert call["expected_ids"] == {"imdb": "tt1234567"}
    assert call["series_ids"] is None
    # Ledger moved waiting -> fixed only on confirmed indexing.
    assert _ledger_docs()[0]["status"] == "fixed"
    assert _budget_used() == 1


def test_A2_real_fix_series_ids_come_from_the_record():
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()

    async def seed_record():
        return await store.upsert_record({
            "canonical_key": "fixture-show", "ids": {"tmdb": 999, "tvdb": 555},
            "titles": ["Fixture Show"], "media_type": "tv",
            "numbering": {"source": "tmdb", "order": "aired",
                          "offset": None, "note": ""},
            "status": "verified", "related_entries": []})

    record = _run(seed_record())
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    writer = FakeWriter()
    engine._bot_api_edit_caption = writer
    seam = FakeSeam()
    bridge.register(seam)
    resp = _fix_via_route(_payload(
        series_record_id=str(record["_id"]),
        series_record_updated_at=record["updated_at"].isoformat()))
    assert resp.content["state"] == "fixed", resp.content
    assert seam.calls[0]["series_ids"] == {"tmdb": 999, "tvdb": 555}


# --- Step 6 classifier branches (§2.2 item 2) ------------------------

def test_A2_flood_wait_terminates_with_seconds_and_standing_reservation():
    _msg, writer, seam = _setup_real(writer_outcomes=[(
        429, {"ok": False, "error_code": 429,
              "description": "Too Many Requests: retry after 30",
              "parameters": {"retry_after": 30}})])
    result = _run(engine.fix_one(_payload()))
    assert result["state"] == "flood_wait"
    assert result["retry_after"] == 30
    assert "30-second" in result["error"]
    row = _fixes_docs()[0]
    assert row["state"] == "flood_wait"
    assert row["budget_counted"] is True  # reservation stands (§8 step 9)
    assert _ledger_docs()[0]["status"] == "waiting"
    assert len(writer.calls) == 1 and seam.calls == []  # no retry, no seam


def test_A2_flood_wait_without_retry_after_invents_no_seconds():
    _msg, _writer, _seam = _setup_real(writer_outcomes=[(
        429, {"ok": False, "error_code": 429,
              "description": "Too Many Requests"})])
    result = _run(engine.fix_one(_payload()))
    assert result["state"] == "flood_wait"
    assert "retry_after" not in result
    assert "second" not in result["error"]


def test_A2_flood_wait_terminates_the_batch_with_aborted_rows():
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    for msg_id in (1, 2, 3):
        _seed_ledger(msg_id=msg_id)
    msg = _message()
    script = {(CHAT, i): msg for i in (1, 2, 3)}
    ub, bt = _clients(script, script)
    writer = FakeWriter([(429, {"ok": False, "error_code": 429,
                                 "description": "Too Many Requests: retry after 30",
                                 "parameters": {"retry_after": 30}})])
    engine._bot_api_edit_caption = writer
    bridge.register(FakeSeam())
    items = [_payload(request_id=f"fw-{i}", msg_id=i) for i in (1, 2, 3)]
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "fw-batch",
                                           "items": items}))
    assert resp.status_code == 200, resp.content
    states = [r["state"] for r in resp.content["results"]]
    assert states == ["flood_wait", "aborted", "aborted"], states
    rows = {r["request_id"]: r for r in _fixes_docs()}
    assert len(rows) == 3
    for rid in ("fw-2", "fw-3"):
        row = rows[rid]
        assert row["state"] == "aborted"
        assert row["budget_counted"] is False
        assert row["dry_run"] is False
        assert row["index_confirmed"] is False
        assert "flood" in row["error"].lower()
        assert row["aborted_at"] is not None
    # The remaining items were never attempted: one edit, and the
    # only probes are the first item's.
    assert len(writer.calls) == 1
    assert ub.calls == [("get_messages", CHAT, 1)]
    assert bt.calls == [("get_messages", CHAT, 1)]
    assert all(r["status"] == "waiting" for r in _ledger_docs())


def test_A2_not_editable_is_edit_failed_repost_deferred():
    _msg, writer, _seam = _setup_real(writer_outcomes=[(
        400, {"ok": False, "error_code": 400,
              "description": "Bad Request: message can't be edited"})])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed"
    assert "cannot be edited" in resp.content["error"]
    assert "Phase 6" in resp.content["error"]
    row = _fixes_docs()[0]
    assert row["budget_counted"] is True  # a real write attempt stands
    assert _ledger_docs()[0]["status"] == "waiting"
    assert len(writer.calls) == 1  # no second client, no repost attempt


def test_A2_not_modified_proceeds_to_confirmation():
    _msg, _writer, _seam = _setup_real(writer_outcomes=[(
        400, {"ok": False, "error_code": 400,
              "description": "Bad Request: message is not modified"})])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "fixed", resp.content


def test_A2_generic_edit_error_is_edit_failed_with_scrubbed_description():
    _msg, _writer, _seam = _setup_real(writer_outcomes=[(
        400, {"ok": False, "error_code": 400,
              "description": "Bad Request: chat not found"})])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed"
    assert "chat not found" in resp.content["error"]


def test_A2_not_found_reprobe_both_missing_is_refused_missing_post():
    # Obligation 3 / S5-2: this branch NEVER releases the
    # reservation — the row is post-write-attempt (it entered
    # `editing` and the writer was called), so §2.3a is never
    # invoked on it (pinned by c43a07b at the store level).
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()
    msg = _message()
    ub = SequencedClient({(CHAT, 1): [msg, None]})
    bt = SequencedClient({(CHAT, 1): [msg, None]})
    api.STUBS["bot"].Userbot = ub
    api.STUBS["bot"].StreamBot = bt
    writer = FakeWriter([(400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: message to edit not found"})])
    engine._bot_api_edit_caption = writer
    bridge.register(FakeSeam())
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_missing_post", resp.content
    assert "re-probe" in resp.content["error"]
    row = _fixes_docs()[0]
    assert row["budget_counted"] is True  # stands — never released
    assert _ledger_docs()[0]["status"] == "waiting"
    assert len(writer.calls) == 1  # the edit is never re-sent


def test_A2_not_found_reprobe_still_found_is_edit_failed():
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()
    msg = _message()
    ub = SequencedClient({(CHAT, 1): [msg, msg]})
    bt = SequencedClient({(CHAT, 1): [msg, msg]})
    api.STUBS["bot"].Userbot = ub
    api.STUBS["bot"].StreamBot = bt
    writer = FakeWriter([(400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: message to edit not found"})])
    engine._bot_api_edit_caption = writer
    bridge.register(FakeSeam())
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed", resp.content
    assert "re-probe" in resp.content["error"]
    assert len(writer.calls) == 1


def test_A2_ambiguous_timeout_caption_landed_proceeds_one_edit_only():
    msg = _message()

    def land(n, chat_id, msg_id, caption_html):
        _set_live_caption(msg, NEW_CAPTION_TEXT, IMDB_URL)

    _setup_real(msg=msg, writer_outcomes=[
        engine.BotApiTransportError("TimeoutError: timed out")],
        on_writer_call=land)
    writer = engine._bot_api_edit_caption
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "fixed", resp.content
    assert len(writer.calls) == 1  # never re-sent (§8 ambiguous rule)


def test_A2_ambiguous_timeout_caption_not_landed_is_edit_failed():
    _msg, writer, _seam = _setup_real(writer_outcomes=[
        engine.BotApiTransportError("TimeoutError: timed out")])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed", resp.content
    assert "ambiguous" in resp.content["error"]
    assert len(writer.calls) == 1
    assert _ledger_docs()[0]["status"] == "waiting"


# --- Step 8 confirmation (§2.2 item 3) -------------------------------

def test_A2_seam_error_is_edit_failed_with_seam_message():
    _msg, _writer, _seam = _setup_real(seam_results=[
        {"state": "error", "ids": None, "filed": None,
         "error": "seam exploded"}])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed"
    assert "seam exploded" in resp.content["error"]
    assert _ledger_docs()[0]["status"] == "waiting"


def test_A2_duplicate_with_matching_ids_and_filed_is_fixed():
    _msg, _writer, _seam = _setup_real(seam_results=[
        {"state": "duplicate", "ids": ("tt1234567", None, "tv"),
         "filed": {"season": 1, "episode": 1}, "error": None}])
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "fixed", resp.content
    assert _ledger_docs()[0]["status"] == "fixed"


def test_A2_indexed_filed_mismatch_restores_to_edited_unindexed():
    msg = _message()
    _setup_real(msg=msg, seam_results=[
        {"state": "indexed", "ids": ("tt1234567", None, "tv"),
         "filed": {"season": 1, "episode": 2}, "error": None}],
        on_writer_call=_apply_then_restore(msg))
    writer = engine._bot_api_edit_caption
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edited_unindexed", resp.content
    assert "previous caption restored" in resp.content["error"]
    assert len(writer.calls) == 2
    assert writer.calls[1] == (CHAT, 1, CAPTION)  # the old caption, restored
    assert _ledger_docs()[0]["status"] == "waiting"
    assert _fixes_docs()[0]["index_confirmed"] is False


def test_A2_missing_intended_cannot_confirm_tv_filing():
    # C-2 item (ii) / Senior B-2: the seam reports a TV filing and
    # the request carried no `intended` — confirmation cannot
    # complete, judged on the seam's report, not the channel.
    # (Engine-level: the route's record-keyed requirement is A-4.)
    msg = _message()
    _setup_real(msg=msg, on_writer_call=_apply_then_restore(msg))
    body = _payload()
    del body["intended"]
    result = _run(engine.fix_one(body))
    assert result["state"] == "edited_unindexed", result
    assert "intended" in result["error"]
    assert "previous caption restored" in result["error"]
    assert _ledger_docs()[0]["status"] == "waiting"


def test_A2_seam_timeout_is_edited_unindexed_with_restore():
    msg = _message()

    async def slow_seam(client, message, **kw):
        await asyncio.sleep(30)
        return {"state": "indexed", "ids": ("tt1234567", None, "tv"),
                "filed": {"season": 1, "episode": 1}, "error": None}

    _setup_real(msg=msg, on_writer_call=_apply_then_restore(msg))
    bridge.register(slow_seam)
    engine.REINDEX_TIMEOUT = 0.2
    try:
        resp = _fix_via_route(_payload())
    finally:
        engine.REINDEX_TIMEOUT = 120
    assert resp.content["state"] == "edited_unindexed", resp.content
    assert "previous caption restored" in resp.content["error"]


def test_A2_ledger_refusal_after_confirmation_escalates_not_fixed():
    _setup_real()
    orig = store.mark_ledger_fixed

    async def refusing(chat_id, msg_id):
        row = await store.get_ledger_row(chat_id, msg_id)
        return row, "parked"

    store.mark_ledger_fixed = refusing
    try:
        resp = _fix_via_route(_payload())
    finally:
        store.mark_ledger_fixed = orig
    assert resp.content["state"] == "escalated", resp.content
    row = _fixes_docs()[0]
    assert row["index_confirmed"] is True  # indexing WAS confirmed
    assert "ledger" in row["error"]


# --- edited_unindexed auto-restore (§2.2 item 4) ---------------------

def test_A2_metadata_failed_restores_and_reports_edited_unindexed():
    msg = _message()
    _setup_real(msg=msg, seam_results=[
        {"state": "metadata_failed", "ids": None, "filed": None,
         "error": None}], on_writer_call=_apply_then_restore(msg))
    writer = engine._bot_api_edit_caption
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edited_unindexed", resp.content
    assert "previous caption restored" in resp.content["error"]
    assert len(writer.calls) == 2
    assert writer.calls[1][2] == CAPTION
    assert _ledger_docs()[0]["status"] == "waiting"  # never left waiting


def test_A2_restore_flood_wait_terminates_flood_wait():
    msg = _message()
    _setup_real(msg=msg, seam_results=[
        {"state": "metadata_failed", "ids": None, "filed": None,
         "error": None}],
        writer_outcomes=[OK_ENVELOPE,
                         (429, {"ok": False, "error_code": 429,
                                "description": "Too Many Requests: retry after 7",
                                "parameters": {"retry_after": 7}})],
        on_writer_call=_apply_then_restore(msg))
    result = _run(engine.fix_one(_payload()))
    assert result["state"] == "flood_wait", result
    assert "restore" in result["error"]
    assert "7-second" in result["error"]
    assert result["retry_after"] == 7


def test_A2_restore_failure_escalates_with_both_captions():
    msg = _message()
    _setup_real(msg=msg, seam_results=[
        {"state": "metadata_failed", "ids": None, "filed": None,
         "error": None}],
        writer_outcomes=[OK_ENVELOPE,
                         (400, {"ok": False, "error_code": 400,
                                "description": "Bad Request: chat not found"})],
        on_writer_call=_apply_then_restore(msg))
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "escalated", resp.content
    error = resp.content["error"]
    assert CAPTION in error and "Fixture Show" in error
    assert "-100111" in error and "chat not found" in error
    assert _ledger_docs()[0]["status"] == "escalated"


def test_A2_restore_landing_wrong_caption_escalates():
    msg = _message()

    def wrong_restore(n, chat_id, msg_id, caption_html):
        if n == 1:
            _set_live_caption(msg, NEW_CAPTION_TEXT, IMDB_URL)
        elif n == 2:
            _set_live_caption(msg, "Some Third Caption tt1234567")

    _setup_real(msg=msg, seam_results=[
        {"state": "metadata_failed", "ids": None, "filed": None,
         "error": None}], on_writer_call=wrong_restore)
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "escalated", resp.content
    assert _ledger_docs()[0]["status"] == "escalated"


# --- Step 3 real branches: skip copy (DISP-2) + ghost ----------------

def _setup_skip_copy(skip_msg, **kw):
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()
    _ledger_docs()[0]["skip_locator"] = {"chat_id": -100777, "msg_id": 9}
    ub = ExplodingClient({})
    bt = ExplodingClient({(-100777, 9): skip_msg, "default": None})
    api.STUBS["bot"].Userbot = ub
    api.STUBS["bot"].StreamBot = bt
    writer = FakeWriter()
    engine._bot_api_edit_caption = writer
    bridge.register(FakeSeam())
    return writer


def test_A2_skip_copy_readable_edit_failed_releases_reservation():
    # DISP-2's pinned terminal: no write attempted, reservation
    # RELEASED via §2.3a (its sole call site — this branch), ledger
    # stays waiting, drift waiver recorded in `error` (§8 step 7).
    writer = _setup_skip_copy(_message())
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "edit_failed", resp.content
    error = resp.content["error"]
    assert "skip copy readable" in error
    assert "Phase 6" in error
    assert "drift check is waived" in error
    row = _fixes_docs()[0]
    assert row["budget_counted"] is False  # released
    assert _budget_used() == 0
    assert writer.calls == []  # no write of any kind
    assert _ledger_docs()[0]["status"] == "waiting"


def test_A2_skip_copy_subtitle_exclusion_recheck():
    # Obligation 1 (Security Note 2, E-1 review): §8 step 7's
    # exclusion re-check runs against the SKIP COPY in the real
    # branch — a subtitle skip copy is refused_subtitle, never
    # classified toward a repost.
    writer = _setup_skip_copy(_message(
        file_name="Fixture Show S01E01.srt", as_document=True))
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_subtitle", resp.content
    assert writer.calls == []
    assert _fixes_docs()[0]["budget_counted"] is False
    assert _ledger_docs()[0]["status"] == "waiting"


def test_A2_skip_copy_personal_exclusion_recheck():
    writer = _setup_skip_copy(_message())
    MEDIA_IDS[(777, 9)] = ("tt1234567", -5, "movie")  # stripped skip chat
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_personal", resp.content
    assert writer.calls == []
    assert _fixes_docs()[0]["budget_counted"] is False


def test_A2_skip_copy_caption_id_check_still_runs():
    writer = _setup_skip_copy(_message())
    resp = _fix_via_route(_payload(
        new_caption='Fixture Show\n<a href="https://www.imdb.com/title/tt9999999">IMDb</a>'))
    assert resp.content["state"] == "refused_caption_id_mismatch", resp.content
    assert writer.calls == []


def test_A2_real_ghost_escalates_ledger_and_keeps_reservation():
    # §8 step 3, real form: the ghost's ledger row is escalated
    # with the probe summary as evidence (test mode never does
    # this — §9 invariant, asserted by the Phase 4 tests). The
    # reservation is NOT released: §2.3a's sole call site is the
    # readable-skip-copy branch (see the report's flagged tension
    # with §8 step 3's "no budget is consumed").
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()
    _ledger_docs()[0]["skip_locator"] = {"chat_id": -100777, "msg_id": 9}
    _clients({}, {})
    writer = FakeWriter()
    engine._bot_api_edit_caption = writer
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_missing_post", resp.content
    assert resp.content["error"] == "userbot: missing; bot: missing; skip: missing"
    ledger = _ledger_docs()[0]
    assert ledger["status"] == "escalated"
    assert ledger["escalation_reason"] == resp.content["error"]
    assert _fixes_docs()[0]["budget_counted"] is True
    assert writer.calls == []


# --- Budget (§2.2 item 1: check + reserve atomic under the lock) -----

def test_A2_budget_exhausted_single_item_refuses_without_row():
    _setup_real(librarian_edit_budget_per_day=0)
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_precondition", resp.content
    assert "budget" in resp.content["error"]
    assert _fixes_docs() == []


def test_A2_budget_exhaustion_mid_batch_aborts_from_exhaustion_point():
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings(librarian_edit_budget_per_day=1)
    _seed_ledger()
    _seed_ledger(msg_id=2)
    msg = _message()
    _clients({(CHAT, 1): msg, (CHAT, 2): msg},
             {(CHAT, 1): msg, (CHAT, 2): msg})
    engine._bot_api_edit_caption = FakeWriter()
    bridge.register(FakeSeam())
    items = [_payload(request_id="bud-1", msg_id=1),
             _payload(request_id="bud-2", msg_id=2)]
    resp = api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": "bud-batch",
                                           "items": items}))
    states = [r["state"] for r in resp.content["results"]]
    assert states == ["fixed", "aborted"], states
    rows = {r["request_id"]: r for r in _fixes_docs()}
    assert rows["bud-1"]["budget_counted"] is True
    assert rows["bud-2"]["budget_counted"] is False
    assert "budget" in rows["bud-2"]["error"]
    assert _budget_used() == 1


def test_A2_budget_boundary_race_exactly_one_reservation():
    # Security S5-3: concurrent items, ONE remaining slot — the
    # check + persist is one unit inside _WRITE_LOCK, so exactly
    # one item reserves; the rest are step-1-class refusals.
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings(librarian_edit_budget_per_day=1)
    n = 5
    for msg_id in range(1, n + 1):
        _seed_ledger(msg_id=msg_id)
    msg = _message()
    script = {(CHAT, i): msg for i in range(1, n + 1)}
    _clients(script, script)
    engine._bot_api_edit_caption = FakeWriter()
    bridge.register(FakeSeam())

    async def go():
        return await asyncio.gather(*[
            engine.fix_one(_payload(request_id=f"race-{i}", msg_id=i))
            for i in range(1, n + 1)])

    results = _run(go())
    states = sorted(r["state"] for r in results)
    assert states == ["fixed"] + ["refused_precondition"] * 4, states
    rows = _fixes_docs()
    assert len(rows) == 1 and rows[0]["budget_counted"] is True
    assert _budget_used() == 1


# --- A-ACC-7: inherited refusals hold on the real path ---------------

def test_A2_real_path_skip_channel_never_a_write_target():
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings(skip_channel="-100111")
    _seed_ledger()
    msg = _message()
    _clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    writer = FakeWriter()
    engine._bot_api_edit_caption = writer
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_precondition"
    assert "never fix targets" in resp.content["error"]
    assert _fixes_docs() == [] and writer.calls == []


def test_A2_real_path_drift_and_exclusions_still_refuse():
    # The shared steps 1–5 segment (E-1's reorder included) guards
    # the real path identically; pre-write refusals never reach
    # the writer.
    msg, writer, _seam = _setup_real()
    drifted = _message(caption="Changed Caption tt1234567")
    _clients({(CHAT, 1): drifted}, {(CHAT, 1): drifted})
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_drift"
    assert writer.calls == []
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    _real_settings()
    _seed_ledger()
    sub = _message(caption="Changed Caption tt1234567",
                   file_name="Fixture Show S01E01.srt", as_document=True)
    _clients({(CHAT, 1): sub}, {(CHAT, 1): sub})
    writer2 = FakeWriter()
    engine._bot_api_edit_caption = writer2
    resp = _fix_via_route(_payload())
    assert resp.content["state"] == "refused_subtitle"  # E-1 order, real path
    assert writer2.calls == []

# ------------------------------------------------------------------
# A-3 — batch execution: bounded concurrency + per-channel pacing
# (Phase 5 plan §2.4, A-ACC-5). The writer double below tracks
# in-flight calls and monotonic call timestamps — the fake-server
# evidence §10.2 pins — with scripted REAL Bot API envelopes.
# ------------------------------------------------------------------

import time as _time


class TrackingWriter:
    """Writer double with an artificial hold: records every call's
    (chat_id, msg_id, monotonic timestamp), the peak number of
    simultaneous in-flight calls, and per-locator scripted
    outcomes / delays (defaults: success envelope, ``delay``)."""

    def __init__(self, delay=0.05, outcomes=None, delays=None):
        self.delay = delay
        self.outcomes = outcomes or {}
        self.delays = delays or {}
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, chat_id, msg_id, caption_html):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls.append((chat_id, msg_id, _time.monotonic()))
        try:
            await asyncio.sleep(self.delays.get((chat_id, msg_id),
                                                self.delay))
            outcome = self.outcomes.get((chat_id, msg_id), OK_ENVELOPE)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.in_flight -= 1


def _setup_batch(channels, items_spec, writer, **settings_kw):
    """Real-mode batch fixture: one ledger row + one readable post
    per (chat_id, msg_id) in items_spec, across ``channels``."""
    _reset()
    bridge.register(None)
    api._enable(librarian_channels=[str(c) for c in channels],
                librarian_channel_types={})
    base = {"librarian_channels": [str(c) for c in channels]}
    base.update(settings_kw)
    _real_settings(**base)
    msg = _message()
    script = {}
    for chat_id, msg_id in items_spec:
        _seed_ledger(chat_id=chat_id, msg_id=msg_id)
        script[(chat_id, msg_id)] = msg
    _clients(script, script)
    engine._bot_api_edit_caption = writer
    bridge.register(FakeSeam())
    return msg


def _batch_via_route(batch_id, items, serial):
    return api._call("POST", "/api/librarian/fix-batch",
                     api._authed_req(body={"batch_id": batch_id,
                                           "serial": serial,
                                           "items": items}))


def test_A3_concurrent_batch_never_exceeds_four_in_flight():
    channels = [-100111 - i for i in range(8)]
    spec = [(c, 1) for c in channels]
    writer = TrackingWriter(delay=0.1)
    _setup_batch(channels, spec, writer)
    items = [_payload(request_id=f"cc-{i}", chat_id=c, msg_id=1)
             for i, c in enumerate(channels)]
    resp = _batch_via_route("cc-batch", items, serial=False)
    assert resp.status_code == 200, resp.content
    results = resp.content["results"]
    # Results come back in request order, every item attempted.
    assert [r["request_id"] for r in results] == [
        f"cc-{i}" for i in range(8)]
    assert [r["state"] for r in results] == ["fixed"] * 8
    assert len(writer.calls) == 8
    # The ceiling holds AND the batch really ran concurrently
    # (a serial executor would peak at 1).
    assert writer.max_in_flight <= 4, writer.max_in_flight
    assert writer.max_in_flight >= 2, writer.max_in_flight


def test_A3_serial_batch_never_overlaps():
    channels = [-100211 - i for i in range(4)]
    spec = [(c, 1) for c in channels]
    writer = TrackingWriter(delay=0.05)
    _setup_batch(channels, spec, writer)
    items = [_payload(request_id=f"sb-{i}", chat_id=c, msg_id=1)
             for i, c in enumerate(channels)]
    resp = _batch_via_route("sb-batch", items, serial=True)
    assert [r["state"] for r in resp.content["results"]] == ["fixed"] * 4
    assert writer.max_in_flight == 1, writer.max_in_flight


def test_A3_pacing_same_channel_spaces_writes_one_second():
    spec = [(CHAT, 1), (CHAT, 2), (CHAT, 3)]
    writer = TrackingWriter(delay=0)
    _setup_batch([CHAT], spec, writer)
    items = [_payload(request_id=f"pc-{i}", msg_id=m)
             for i, m in enumerate((1, 2, 3), start=1)]
    resp = _batch_via_route("pc-batch", items, serial=False)
    assert [r["state"] for r in resp.content["results"]] == ["fixed"] * 3
    stamps = [t for _c, _m, t in sorted(writer.calls,
                                        key=lambda call: call[2])]
    assert len(stamps) == 3
    for earlier, later in zip(stamps, stamps[1:]):
        assert later - earlier >= 0.99, (earlier, later)


def test_A3_pacing_does_not_apply_across_channels():
    channels = [-100311, -100312]
    spec = [(c, 1) for c in channels]
    writer = TrackingWriter(delay=0)
    _setup_batch(channels, spec, writer)
    items = [_payload(request_id=f"px-{i}", chat_id=c, msg_id=1)
             for i, c in enumerate(channels)]
    resp = _batch_via_route("px-batch", items, serial=False)
    assert [r["state"] for r in resp.content["results"]] == ["fixed"] * 2
    stamps = sorted(t for _c, _m, t in writer.calls)
    assert stamps[1] - stamps[0] < 0.5, stamps


def test_A3_single_fixes_are_not_paced():
    # Pacing is batch machinery (§2.4): two standalone fix_one
    # calls to the same channel carry no pacing delay.
    _msg, writer, _seam = _setup_real()
    _seed_ledger(msg_id=2)
    msg = _message()
    _clients({(CHAT, 1): msg, (CHAT, 2): msg},
             {(CHAT, 1): msg, (CHAT, 2): msg})
    start = _time.monotonic()
    r1 = _run(engine.fix_one(_payload(request_id="np-1", msg_id=1)))
    r2 = _run(engine.fix_one(_payload(request_id="np-2", msg_id=2)))
    elapsed = _time.monotonic() - start
    assert r1["state"] == "fixed" and r2["state"] == "fixed"
    assert len(writer.calls) == 2
    assert elapsed < 0.9, elapsed


def test_A3_flood_wait_under_concurrency_inflight_finish_rest_aborted():
    channels = [-100411 - i for i in range(7)]
    spec = [(c, 1) for c in channels]
    flood = (429, {"ok": False, "error_code": 429,
                   "description": "Too Many Requests: retry after 30",
                   "parameters": {"retry_after": 30}})
    # The flooding item's write returns at once; the other
    # in-flight writes hold for 0.3 s, so items 5-7 are still
    # unattempted (semaphore waiters) when the flood lands.
    writer = TrackingWriter(
        delay=0.3, outcomes={(channels[0], 1): flood},
        delays={(channels[0], 1): 0})
    _setup_batch(channels, spec, writer)
    items = [_payload(request_id=f"cf-{i}", chat_id=c, msg_id=1)
             for i, c in enumerate(channels)]
    resp = _batch_via_route("cf-batch", items, serial=False)
    assert resp.status_code == 200, resp.content
    results = resp.content["results"]
    assert [r["request_id"] for r in results] == [
        f"cf-{i}" for i in range(7)]
    states = [r["state"] for r in results]
    # In-flight items finish honestly; unattempted items are
    # recorded aborted — never attempted, never silently dropped.
    assert states == ["flood_wait", "fixed", "fixed", "fixed",
                      "aborted", "aborted", "aborted"], states
    assert len(writer.calls) == 4, writer.calls
    rows = {r["request_id"]: r for r in _fixes_docs()}
    assert len(rows) == 7
    for rid in ("cf-4", "cf-5", "cf-6"):
        row = rows[rid]
        assert row["state"] == "aborted"
        assert row["budget_counted"] is False
        assert row["aborted_at"] is not None
        assert "flood" in row["error"].lower()



# --- html_from_canonical (§4.2's reconstruction, landed by A-2) ------

def test_A2_html_from_canonical_shapes():
    h = engine.html_from_canonical
    # ID link: the bare label line is dropped, the URL line becomes
    # the labelled anchor (caption_with_id's construction).
    assert h("Show Name\nIMDB\nhttps://www.imdb.com/title/tt1234567") == (
        'Show Name\n<a href="https://www.imdb.com/title/tt1234567">IMDB</a>')
    assert h("Show Name\nTMDB\nhttps://www.themoviedb.org/tv/999") == (
        'Show Name\n<a href="https://www.themoviedb.org/tv/999">TMDB</a>')
    # A non-ID URL line becomes an anchor over its own text.
    assert h("Show Name\nhttps://example.com/page") == (
        'Show Name\n<a href="https://example.com/page">https://example.com/page</a>')
    # Plain text is HTML-escaped; no URL -> no anchor.
    assert h("A <B> & C") == "A &lt;B&gt; &amp; C"
    assert h("") == ""
    # Round-trip through the canonical form for the app's own
    # caption shape (uppercase labels, as caption_with_id emits).
    canonical = "Show Name\nIMDB\nhttps://www.imdb.com/title/tt1234567"
    assert engine.canonical_from_html(h(canonical)) == canonical
