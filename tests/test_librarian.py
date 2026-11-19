"""Librarian v4 tests: pure helpers (name bits, reason codes, initials),
the ledger upsert, and the packet -> proposal -> gate -> apply -> rollback
flow against fake collections and stubbed app modules."""

import asyncio
import logging
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module

_LIB = None


def load_lib():
    """Install Backend stubs, then load librarian.py by path (once)."""
    global _LIB
    if _LIB is not None:
        return _LIB

    backend = types.ModuleType("Backend")
    removed = []

    async def _remove_media_part(chat, msg):
        removed.append((chat, msg))
        return True

    async def _get_media_ids(chat, msg):
        return None

    backend.db = types.SimpleNamespace(
        dbs={}, remove_media_part=_remove_media_part,
        get_media_ids_by_part=_get_media_ids)
    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("librarian-tests")

    common = types.ModuleType("Backend.helper.metadata.common")
    _ID_RE = re.compile(r"\btt\d{7,10}\b|themoviedb\.org/(?:movie|tv)/\d+", re.I)

    def extract_default_id(text):
        m = re.search(r"\b(tt\d{7,10})\b", text or "")
        if m:
            return m.group(1)
        m = re.search(r"/(?:movie|tv)/(\d+)", text or "")
        return m.group(1) if m else None

    common.extract_default_id = extract_default_id

    entry = types.ModuleType("Backend.helper.metadata.entry")
    entry.metadata = None  # set per-test
    entry.search_any_candidates = None
    entry.search_movie_candidates = None
    entry.search_tv_candidates = None

    parse = types.ModuleType("Backend.helper.metadata.parse")
    parse.analyze_metadata_failure = lambda name: ""
    parse.parse_media_name = lambda name: {}

    pyro = types.ModuleType("Backend.helper.pyro")
    pyro.clean_filename = lambda s: s

    settings_ns = types.SimpleNamespace(
        librarian_enabled=True, librarian_dry_run=True,
        librarian_edit_budget_per_day=50, hermes_bot_token="tok",
        skip_channel=-100999)
    settings_manager = types.ModuleType("Backend.helper.settings_manager")

    class _SM:
        @staticmethod
        def current():
            return settings_ns

    settings_manager.SettingsManager = _SM

    encrypt = types.ModuleType("Backend.helper.encrypt")

    async def _decode(s):
        return {}

    encrypt.decode_string = _decode

    bot = types.ModuleType("Backend.pyrofork.bot")
    bot.Userbot = None

    sys.modules.update({
        "Backend": backend,
        "Backend.logger": logger_mod,
        "Backend.helper": types.ModuleType("Backend.helper"),
        "Backend.helper.metadata": types.ModuleType("Backend.helper.metadata"),
        "Backend.helper.metadata.common": common,
        "Backend.helper.metadata.entry": entry,
        "Backend.helper.metadata.parse": parse,
        "Backend.helper.pyro": pyro,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.encrypt": encrypt,
        "Backend.pyrofork": types.ModuleType("Backend.pyrofork"),
        "Backend.pyrofork.bot": bot,
    })
    _LIB = load_module("librarian_under_test", "Backend/helper/librarian.py")
    _LIB._stubs = {"common": common, "entry": entry, "settings": settings_ns,
                   "removed": removed}
    return _LIB


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_name_bits_special_run():
    lib = load_lib()
    bits = lib.name_bits("BTTH 3YA - SP3 E29 [E-SUB] 4K", "")
    assert bits["special"] == 3
    assert bits["episode"] == 29
    assert bits["stem"] == "btth 3ya sp3"


def test_name_bits_season_episode_and_split():
    lib = load_lib()
    bits = lib.name_bits("", "Breaking Bad S01E01.part002.mkv")
    assert bits["season"] == 1 and bits["episode"] == 1
    assert bits["stem"] == "breaking bad"


def test_name_bits_bare_episode_and_absolute():
    lib = load_lib()
    assert lib.name_bits("TDH E29", "")["episode"] == 29
    bits = lib.name_bits("OP 1172", "")
    assert bits["episode"] == 1172 and bits["stem"] == "op"


def test_name_bits_movie_has_no_episode():
    lib = load_lib()
    bits = lib.name_bits("Inception (2010) 1080p", "")
    assert bits["episode"] is None and bits["season"] is None
    assert bits["year"] == 2010


def test_reason_codes():
    lib = load_lib()
    assert lib.reason_code_for("No video quality/resolution was found.") == "no-quality"
    assert lib.reason_code_for("Could not match this title on providers.") == "no-match"
    assert lib.reason_code_for("Indexed under more than one title: X, Y") == "suspect-multi-index"
    assert lib.reason_code_for("anything else") == "unknown"


def test_initials_matching():
    lib = load_lib()
    assert lib.initials_of("Battle Through The Heavens") == "btth"
    assert lib.stem_title_match("btth 3ya sp3", "Battle Through The Heavens")
    assert not lib.stem_title_match("btth", "Breaking Bad")


# ---------------------------------------------------------------------------
# Fake Mongo collection (mirrors the operator-conflict rule)
# ---------------------------------------------------------------------------

def _matches(doc, query):
    for k, cond in (query or {}).items():
        val = doc.get(k)
        if isinstance(cond, dict):
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                return False
        elif val != cond:
            return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: d.get(field) or 0,
                        reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield dict(d)
        return gen()


class _FakeColl:
    def __init__(self):
        self.docs = {}

    def find(self, query=None, projection=None):
        return _FakeCursor([d for d in self.docs.values() if _matches(d, query)])

    async def find_one(self, query):
        for d in self.docs.values():
            if _matches(d, query):
                return dict(d)
        return None

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", f"auto{len(self.docs)}")
        self.docs[doc["_id"]] = doc

    async def replace_one(self, filt, doc, upsert=False):
        self.docs[filt["_id"]] = dict(doc)

    async def count_documents(self, query):
        return sum(1 for d in self.docs.values() if _matches(d, query))

    async def update_one(self, filt, update, upsert=False):
        seen = {}
        for op, body in update.items():
            for path in (body or {}):
                if path in seen:
                    raise ValueError(f"Updating the path '{path}' would create a conflict")
                seen[path] = op
        key = filt.get("_id")
        doc = self.docs.get(key)
        if doc is None:
            if not upsert and "$setOnInsert" not in update:
                # find the doc by query match instead of _id
                doc = next((d for d in self.docs.values() if _matches(d, filt)), None)
                if doc is None:
                    return
            doc = self.docs.setdefault(key, {"_id": key, "history": [],
                                             "skip_msg_ids": []})
        for k, v in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(k, v)
        for k, v in (update.get("$set") or {}).items():
            doc[k] = v
        for k in (update.get("$unset") or {}):
            doc.pop(k, None)
        pushed = (update.get("$push") or {}).get("history")
        if isinstance(pushed, dict):
            doc.setdefault("history", []).append(pushed)
        add = (update.get("$addToSet") or {}).get("skip_msg_ids")
        if add is not None:
            ids = doc.setdefault("skip_msg_ids", [])
            vals = add.get("$each") if isinstance(add, dict) else [add]
            for v in vals or []:
                if v not in ids:
                    ids.append(v)


def _wire(lib, ledger=None, locator=None, proposals=None, audit=None, alias=None):
    saved = {}
    for name, fake in (("_ledger_coll", ledger), ("_locator_coll", locator),
                       ("_proposals_coll", proposals), ("_audit_coll", audit),
                       ("_alias_coll", alias)):
        saved[name] = getattr(lib, name)
        if fake is not None:
            setattr(lib, name, lambda f=fake: f)
    return saved


def _unwire(lib, saved):
    for name, fn in saved.items():
        setattr(lib, name, fn)


def test_record_reject_upserts_without_duplicating():
    lib = load_lib()
    fake = _FakeColl()
    saved = _wire(lib, ledger=fake)
    try:
        fields = {"key": "uid1", "file_unique_id": "uid1", "chat_id": -1001,
                  "msg_id": 5, "origin": "live-fail", "caption": "TDH E29",
                  "reason": "No video quality", "reason_code": "no-quality",
                  "skip_msg_id": 900}
        assert run(lib.record_reject(fields)) is True
        assert run(lib.record_reject(fields)) is True
    finally:
        _unwire(lib, saved)
    assert len(fake.docs) == 1
    assert fake.docs["uid1"]["skip_msg_ids"] == [900]


# ---------------------------------------------------------------------------
# Packet
# ---------------------------------------------------------------------------

def test_packet_returns_located_channel_items():
    lib = load_lib()
    ledger, locator = _FakeColl(), _FakeColl()
    ledger.docs["u1"] = {"_id": "u1", "status": "open", "caption": "",
                         "file_name": "Show E01.mkv", "first_seen": 1.0,
                         "skip_msg_ids": [50]}
    ledger.docs["u2"] = {"_id": "u2", "status": "open", "chat_id": -100111,
                         "msg_id": 7, "caption": "Show E02", "first_seen": 2.0}
    ledger.docs["u3"] = {"_id": "u3", "status": "open", "chat_id": -100222,
                         "msg_id": 9, "caption": "Other E01", "first_seen": 3.0}
    locator.docs["u1"] = {"_id": "u1", "chat_id": -100111, "msg_id": 5,
                          "caption": "Show E01", "file_name": "Show E01.mkv"}
    saved = _wire(lib, ledger=ledger, locator=locator, alias=_FakeColl())
    orig_index = lib._channel_title_index

    async def no_index():
        return {}

    lib._channel_title_index = no_index
    try:
        packet = run(lib.get_packet(-100111, limit=10))
    finally:
        lib._channel_title_index = orig_index
        _unwire(lib, saved)
    assert packet["ok"] is True
    keys = sorted(i["key"] for i in packet["items"])
    assert keys == ["u1", "u2"]
    u1 = next(i for i in packet["items"] if i["key"] == "u1")
    assert u1["located"] is True and u1["msg_id"] == 5


# ---------------------------------------------------------------------------
# Proposals + gate
# ---------------------------------------------------------------------------

def _seed_proposal_env(lib):
    ledger, proposals = _FakeColl(), _FakeColl()
    ledger.docs["k1"] = {"_id": "k1", "status": "open",
                         "caption": "Some Show S02E05", "first_seen": 1.0}
    saved = _wire(lib, ledger=ledger, proposals=proposals)
    return ledger, proposals, saved


def _plan(caption="Some Show S02E05 1080p\n<a href=\"https://www.imdb.com/title/tt1234567/\">IMDB</a>",
          episode=5):
    return {"channel": -100111, "label": "Some Show S2",
            "items": [{"key": "k1", "new_caption": caption, "media_type": "tv",
                       "tmdb_id": 42, "imdb_id": "tt1234567",
                       "season": 2, "episode": episode}]}


def test_submit_proposal_requires_id_stamp():
    lib = load_lib()
    ledger, proposals, saved = _seed_proposal_env(lib)
    try:
        plan = _plan(caption="Some Show S02E05 1080p")
        res = run(lib.submit_proposal(plan))
    finally:
        _unwire(lib, saved)
    assert res["ok"] is False
    assert "no ID link" in res["message"]


def test_gate_passes_and_fails_on_numbering():
    lib = load_lib()
    ledger, proposals, saved = _seed_proposal_env(lib)
    entry = lib._stubs["entry"]

    async def fake_metadata(name, channel, msg_id, override_id=None, season_hint=None):
        return {"tmdb_id": 42, "imdb_id": "tt1234567", "title": "Some Show",
                "season_number": 2, "episode_number": 5}

    entry.metadata = fake_metadata
    try:
        res = run(lib.submit_proposal(_plan()))
        assert res["ok"] is True and res["status"] == "pending", res
        pid = res["proposal_id"]
        assert ledger.docs["k1"].get("proposal_id") == pid

        ledger.docs["k1"].pop("proposal_id", None)
        res2 = run(lib.submit_proposal(_plan(episode=7)))
        assert res2["ok"] is True and res2["status"] == "gate_failed", res2
    finally:
        entry.metadata = None
        _unwire(lib, saved)


# ---------------------------------------------------------------------------
# Apply + rollback
# ---------------------------------------------------------------------------

def test_apply_edit_path_dry_run_then_live():
    lib = load_lib()
    ledger, proposals, audit, alias = (_FakeColl(), _FakeColl(),
                                       _FakeColl(), _FakeColl())
    locator = _FakeColl()
    ledger.docs["k1"] = {"_id": "k1", "status": "open",
                         "caption": "Some Show S02E05", "skip_msg_ids": [50],
                         "first_seen": 1.0}
    locator.docs["k1"] = {"_id": "k1", "chat_id": -100111, "msg_id": 5,
                          "caption": "Some Show S02E05", "file_name": ""}
    proposals.docs["p1"] = {
        "_id": "p1", "channel": -100111, "label": "Some Show S2",
        "status": "pending",
        "items": [{"key": "k1", "new_caption": "Some Show S02E05 1080p tt1234567",
                   "media_type": "tv", "tmdb_id": 42,
                   "imdb_id": "tt1234567", "season": 2, "episode": 5}]}
    saved = _wire(lib, ledger=ledger, locator=locator, proposals=proposals,
                  audit=audit, alias=alias)
    calls = []
    cleaned = []

    async def fake_bot_api(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 5}}

    async def fake_verify(chat, msg, tmdb, imdb, tries=10, delay=3.0):
        return True

    async def fake_clean(item):
        cleaned.append(item["_id"])

    orig = (lib._bot_api, lib._verify_indexed, lib._delete_skip_copies)
    lib._bot_api, lib._verify_indexed, lib._delete_skip_copies = (
        fake_bot_api, fake_verify, fake_clean)
    settings = lib._stubs["settings"]
    try:
        settings.librarian_dry_run = True
        res = run(lib.apply_proposal("p1"))
        assert res["ok"] is True and res.get("dry_run") is True
        assert calls == []  # dry run edits nothing

        settings.librarian_dry_run = False
        res = run(lib.apply_proposal("p1"))
        assert res["ok"] is True and res["status"] == "applied", res
        assert calls and calls[0][0] == "editMessageCaption"
        assert ledger.docs["k1"]["status"] == "fixed"
        assert cleaned == ["k1"]
        assert len(audit.docs) == 1
        assert any(d.get("stem") == "some show" for d in alias.docs.values())

        # Rollback restores + un-indexes.
        calls.clear()
        res = run(lib.rollback_proposal("p1"))
        assert res["ok"] is True and res["rolled_back"] == 1, res
        assert calls and calls[0][0] == "editMessageCaption"
        assert calls[0][1]["caption"] == "Some Show S02E05"
        assert lib._stubs["removed"] == [(111, 5)]
        assert ledger.docs["k1"]["status"] == "open"
    finally:
        lib._bot_api, lib._verify_indexed, lib._delete_skip_copies = orig
        settings.librarian_dry_run = True
        _unwire(lib, saved)
