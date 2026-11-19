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
        librarian_batch_size=60, librarian_gate_strict_year=False,
        librarian_specials_policy="guard",
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

    class _FakeUB:
        """Stands in for the app userbot: serves canned messages so the
        pre-edit identity check can be exercised both ways."""

        def __init__(self):
            self.messages = {}

        async def get_messages(self, chat_id, msg_id):
            return self.messages.get((int(chat_id), int(msg_id)))

    bot.fake_ub = _FakeUB()
    bot.Userbot = bot.fake_ub

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
                   "removed": removed, "bot": bot}
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

    async def delete_one(self, filt):
        key = filt.get("_id")
        if key in self.docs:
            del self.docs[key]
            return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    async def replace_one(self, filt, doc, upsert=False):
        self.docs[filt["_id"]] = dict(doc)

    async def count_documents(self, query):
        return sum(1 for d in self.docs.values() if _matches(d, query))

    def aggregate(self, pipeline):
        docs = [dict(d) for d in self.docs.values()]
        for stage in pipeline or []:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                field = str(spec.get("_id") or "").lstrip("$")
                groups = {}
                for d in docs:
                    key = d.get(field)
                    row = groups.setdefault(key, {"_id": key, "n": 0})
                    row["n"] += 1
                docs = list(groups.values())
            elif "$sort" in stage:
                for field, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda d: d.get(field) or 0,
                              reverse=(direction < 0))
        return _FakeCursor(docs)

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


def _wire(lib, ledger=None, locator=None, proposals=None, audit=None,
          alias=None, packets=None):
    saved = {}
    for name, fake in (("_ledger_coll", ledger), ("_locator_coll", locator),
                       ("_proposals_coll", proposals), ("_audit_coll", audit),
                       ("_alias_coll", alias), ("_packets_coll", packets)):
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


def test_record_scan_entry_new_and_existing():
    lib = load_lib()
    fake = _FakeColl()
    post = {"chat_id": -1004395609351, "msg_id": 42, "caption": "Show E01",
            "file_name": "Show E01.mkv", "size": 100, "height": 1080}
    match = {"title": "Show", "media_type": "tv", "season": 1,
             "episode": 1, "quality": "1080p"}
    # A brand-new indexed file enters as indexed — never as waiting.
    run(lib._record_scan_entry(fake, "s1", post, "indexed", match))
    doc = fake.docs["s1"]
    assert doc["status"] == "indexed" and doc["scan_status"] == "indexed"
    assert doc["current_match"]["title"] == "Show"
    # A reject keeps its workflow status when a scan refreshes it.
    fake.docs["s2"] = {"_id": "s2", "status": "open", "caption": "old",
                       "file_name": "", "history": []}
    run(lib._record_scan_entry(fake, "s2", post, "unindexed", None))
    doc = fake.docs["s2"]
    assert doc["status"] == "open"
    assert doc["scan_status"] == "unindexed"
    assert doc["scan_msg_id"] == 42
    # A fixed file stays fixed.
    fake.docs["s3"] = {"_id": "s3", "status": "fixed", "history": []}
    run(lib._record_scan_entry(fake, "s3", post, "indexed", match))
    assert fake.docs["s3"]["status"] == "fixed"


def test_packet_scope_selects_scanned_files():
    lib = load_lib()
    ledger, locator, packets = _FakeColl(), _FakeColl(), _FakeColl()
    base = {"chat_id": -1004395609351, "caption": "Show E01",
            "file_name": "Show E01.mkv"}
    ledger.docs["w1"] = {"_id": "w1", "status": "open", "first_seen": 1.0,
                         **base}
    ledger.docs["i1"] = {"_id": "i1", "status": "indexed", "first_seen": 2.0,
                         "scan_status": "indexed",
                         "current_match": {"title": "Show",
                                            "media_type": "tv"}, **base}
    ledger.docs["u1"] = {"_id": "u1", "status": "unindexed",
                         "first_seen": 3.0, "scan_status": "unindexed",
                         **base}
    saved = _wire(lib, ledger=ledger, locator=locator, packets=packets)
    try:
        chan = -1004395609351
        pkt = run(lib.get_packet(chan, 10))
        assert [i["key"] for i in pkt["items"]] == ["w1"]
        assert pkt["scope"] == "waiting"
        pkt = run(lib.get_packet(chan, 10, "indexed"))
        assert [i["key"] for i in pkt["items"]] == ["i1"]
        assert pkt["items"][0]["current_match"]["title"] == "Show"
        pkt = run(lib.get_packet(chan, 10, "all"))
        assert sorted(i["key"] for i in pkt["items"]) == ["i1", "u1", "w1"]
        pkt = run(lib.get_packet(chan, 10, "bogus"))
        assert [i["key"] for i in pkt["items"]] == ["w1"]
        assert pkt["scope"] == "waiting"
    finally:
        _unwire(lib, saved)


def test_enabled_modules_merge_defaults_and_stored():
    lib = load_lib()
    mods = lib.enabled_modules(None)
    assert mods["status"] is True and mods["channels"] is True
    assert mods["proposals"] is True and mods["activity"] is True
    assert mods["flagged"] is False and mods["aliases"] is False
    merged = lib.enabled_modules({"flagged": True, "proposals": False,
                                  "bogus": True})
    assert merged["flagged"] is True
    assert merged["proposals"] is False
    assert "bogus" not in merged
    assert lib.enabled_modules("junk") == mods
    assert lib.enabled_modules({}) == mods


def test_panel_counts_cached_until_invalidated():
    lib = load_lib()
    ledger, locator = _FakeColl(), _FakeColl()
    proposals, audit = _FakeColl(), _FakeColl()
    ledger.docs["a"] = {"_id": "a", "status": "open", "chat_id": -1001,
                         "reason_code": "no-match"}
    saved = _wire(lib, ledger=ledger, locator=locator,
                  proposals=proposals, audit=audit)
    try:
        lib.invalidate_counts()
        first = run(lib.panel_counts())
        assert first["waiting"] == 1
        ledger.docs["b"] = {"_id": "b", "status": "open", "chat_id": -1001,
                             "reason_code": "no-match"}
        second = run(lib.panel_counts())
        assert second["waiting"] == 1  # served from cache
        lib.invalidate_counts()
        third = run(lib.panel_counts())
        assert third["waiting"] == 2
    finally:
        _unwire(lib, saved)
        lib.invalidate_counts()


def test_request_stop_flags_running_action():
    lib = load_lib()
    st = lib.ACTION_STATE
    old = dict(st)
    try:
        st.update({"running": False, "kind": ""})
        assert lib.request_stop()["status"] == "error"
        st.update({"running": True, "kind": "scan", "stop_requested": False})
        assert lib.request_stop()["status"] == "success"
        assert st["stop_requested"] is True
    finally:
        st.update(old)


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
        # A gate-failed plan releases its items back to the pool.
        assert ledger.docs["k1"].get("proposal_id") is None
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
    # The pre-edit identity check must see the right file at the target.
    lib._stubs["bot"].fake_ub.messages[(-100111, 5)] = types.SimpleNamespace(
        caption="Some Show S02E05", empty=False, document=None,
        video=types.SimpleNamespace(file_unique_id="k1"))
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


# ---------------------------------------------------------------------------
# v4.1 — pairing guard, specials gate, strict year, identity check
# ---------------------------------------------------------------------------

def test_packet_numbers_runs_hint_and_special_flag():
    lib = load_lib()
    ledger, locator, packets = _FakeColl(), _FakeColl(), _FakeColl()
    for i, cap in enumerate([
            "Alpha Show - EP01 1080p", "Alpha Show - EP02 1080p",
            "Beta Movie (2020) 1080p", "BTTH 3YA - SP3 E01 [E-SUB] 4K"]):
        key = f"pk{i}"
        ledger.docs[key] = {"_id": key, "status": "open", "caption": cap,
                            "chat_id": -100111, "msg_id": 10 + i,
                            "first_seen": float(i)}
        locator.docs[key] = {"_id": key, "chat_id": -100111,
                             "msg_id": 10 + i, "caption": cap, "file_name": ""}
    saved = _wire(lib, ledger=ledger, locator=locator, packets=packets)
    try:
        res = run(lib.get_packet(-100111, 10))
    finally:
        _unwire(lib, saved)
    assert res["ok"] is True
    assert [it["n"] for it in res["items"]] == [1, 2, 3, 4]
    assert res["packet_id"].startswith("pkt")
    assert len(packets.docs) == 1
    runs = {r["stem"]: r for r in res["runs_hint"]}
    assert runs["alpha show"]["count"] == 2
    assert runs["alpha show"]["numbers"] == [1, 2]
    sp = [it for it in res["items"] if it["key"] == "pk3"][0]
    assert sp["special"] is True
    assert [it for it in res["items"] if it["key"] == "pk0"][0]["special"] is False


def test_group_stem_drops_trailing_number_tokens():
    lib = load_lib()
    assert lib._group_stem("code geass turn 19 02") == "code geass turn"
    assert lib._group_stem("alpha show") == "alpha show"
    assert lib._group_stem("some movie 2") == "some movie"
    assert lib._group_stem("1899") == "1899"  # all-numeric stem survives


def test_submit_numbered_echo_guard():
    lib = load_lib()
    ledger, proposals, packets = _FakeColl(), _FakeColl(), _FakeColl()
    ledger.docs["k1"] = {"_id": "k1", "status": "open",
                         "caption": "Some Show S02E05",
                         "chat_id": -100111, "msg_id": 5, "first_seen": 1.0}
    saved = _wire(lib, ledger=ledger, proposals=proposals, packets=packets)
    entry = lib._stubs["entry"]

    async def fake_metadata(name, channel, msg_id, override_id=None,
                            season_hint=None):
        return {"tmdb_id": 42, "imdb_id": "tt1234567", "title": "Some Show",
                "season_number": 2, "episode_number": 5}

    entry.metadata = fake_metadata
    try:
        packet = run(lib.get_packet(-100111, 10))
        pid = packet["packet_id"]
        base = {"channel": -100111, "label": "Some Show S2",
                "packet_id": pid,
                "items": [{"n": 1, "new_caption": _plan()["items"][0]["new_caption"],
                           "media_type": "tv", "tmdb_id": 42,
                           "imdb_id": "tt1234567", "season": 2, "episode": 5,
                           "seen_caption": "A COMPLETELY DIFFERENT CAPTION"}]}
        res = run(lib.submit_proposal(base))
        assert res["ok"] is False and "does not match" in res["message"], res

        base["items"][0]["seen_caption"] = "Some Show S02E05"
        res = run(lib.submit_proposal(base))
        assert res["ok"] is True and res["status"] == "pending", res

        # Packet references REQUIRE the echo.
        base["items"][0].pop("seen_caption")
        res = run(lib.submit_proposal(base))
        assert res["ok"] is False and "seen_caption is required" in res["message"]
    finally:
        entry.metadata = None
        _unwire(lib, saved)


def test_specials_gate_and_strict_year():
    import re as _re
    lib = load_lib()
    ledger, proposals = _FakeColl(), _FakeColl()
    ledger.docs["ksp"] = {"_id": "ksp", "status": "open",
                          "caption": "BTTH 3YA - SP3 E01 [E-SUB] 4K",
                          "file_name": "BTTH 3YA - SP3 E01.mkv",
                          "first_seen": 1.0}
    saved = _wire(lib, ledger=ledger, proposals=proposals)
    entry = lib._stubs["entry"]
    settings = lib._stubs["settings"]

    async def fake_metadata(name, channel, msg_id, override_id=None,
                            season_hint=None):
        m = _re.search(r"S(\d+)E(\d+)", name)
        return {"tmdb_id": 777, "imdb_id": "tt7777777",
                "title": "Battle Through the Heavens",
                "season_number": int(m.group(1)),
                "episode_number": int(m.group(2)), "release_year": 2017}

    entry.metadata = fake_metadata

    def btth_plan(season, **kw):
        cap = (f"Battle Through the Heavens S{season:02d}E01 4K\n"
               "<a href=\"https://www.imdb.com/title/tt7777777/\">IMDB</a>")
        item = {"key": "ksp", "new_caption": cap, "media_type": "tv",
                "tmdb_id": 777, "imdb_id": "tt7777777",
                "season": season, "episode": 1}
        item.update(kw)
        return {"channel": -100444, "label": "BTTH SP3", "items": [item]}

    try:
        # SP-marked original mapped to a regular season: gate must refuse.
        res = run(lib.submit_proposal(btth_plan(3)))
        assert res["ok"] is True and res["status"] == "gate_failed", res
        reason = res["gate"]["items"]["ksp"]["reason"]
        assert "special" in reason, reason

        # The same file mapped to the specials season passes.
        res = run(lib.submit_proposal(btth_plan(0)))
        assert res["ok"] is True and res["status"] == "pending", res

        # Strict year: claiming 2021 for a 2017 resolution fails the gate.
        settings.librarian_gate_strict_year = True
        res = run(lib.submit_proposal(btth_plan(0, year=2021)))
        assert res["ok"] is True and res["status"] == "gate_failed", res
        assert "year" in res["gate"]["items"]["ksp"]["reason"]
    finally:
        settings.librarian_gate_strict_year = False
        entry.metadata = None
        _unwire(lib, saved)


def test_identity_check_blocks_wrong_file():
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

    async def fake_bot_api(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 5}}

    orig = lib._bot_api
    lib._bot_api = fake_bot_api
    settings = lib._stubs["settings"]
    # The post at the target is a DIFFERENT file than the plan expects.
    lib._stubs["bot"].fake_ub.messages[(-100111, 5)] = types.SimpleNamespace(
        caption="Some Show S02E05", empty=False, document=None,
        video=types.SimpleNamespace(file_unique_id="ANOTHER_FILE"))
    try:
        settings.librarian_dry_run = False
        res = run(lib.apply_proposal("p1"))
        assert res["ok"] is True and res["status"] == "failed", res
        assert res["applied"] == 0
        assert calls == []  # nothing was edited
        assert ledger.docs["k1"]["status"] == "open"
        assert len(audit.docs) == 1 and audit.docs[
            list(audit.docs)[0]]["ok"] is False
    finally:
        lib._bot_api = orig
        settings.librarian_dry_run = True
        _unwire(lib, saved)


# ---------------------------------------------------------------------------
# v5 Phase 2 — control room: aliases, dismiss, flagged queue
# ---------------------------------------------------------------------------

def test_alias_list_and_delete():
    lib = load_lib()
    alias = _FakeColl()
    alias.docs["-1001:btth"] = {"_id": "-1001:btth", "channel": -1001,
                                "stem": "btth", "title": "Battle Through The Heavens",
                                "tmdb_id": 123, "imdb_id": None,
                                "media_type": "tv", "taught_at": 2.0}
    alias.docs["-1001:pw"] = {"_id": "-1001:pw", "channel": -1001,
                              "stem": "pw", "title": "Perfect World",
                              "tmdb_id": 456, "imdb_id": "tt999",
                              "media_type": "tv", "taught_at": 1.0}
    saved = _wire(lib, alias=alias)
    try:
        rows = run(lib.list_aliases())
        assert [r["stem"] for r in rows] == ["btth", "pw"]  # newest first
        res = run(lib.delete_alias("-1001:pw"))
        assert res["ok"] is True and res["deleted"] == 1
        assert "-1001:pw" not in alias.docs
        res = run(lib.delete_alias("-1001:pw"))
        assert res["ok"] is False
    finally:
        _unwire(lib, saved)


def test_dismiss_item_leaves_packets():
    lib = load_lib()
    ledger, locator = _FakeColl(), _FakeColl()
    ledger.docs["d1"] = {"_id": "d1", "status": "open", "chat_id": -100111,
                         "caption": "Broken File", "first_seen": 1.0,
                         "reason_code": "no-match"}
    ledger.docs["d2"] = {"_id": "d2", "status": "fixed", "chat_id": -100111,
                         "caption": "Done", "first_seen": 2.0}
    saved = _wire(lib, ledger=ledger, locator=locator)
    try:
        res = run(lib.dismiss_item("d1"))
        assert res["ok"] is True
        assert ledger.docs["d1"]["status"] == "dismissed"
        # A dismissed item is no longer offered; a fixed one cannot be dismissed.
        res = run(lib.dismiss_item("d1"))
        assert res["ok"] is False
        res = run(lib.dismiss_item("d2"))
        assert res["ok"] is False
        res = run(lib.dismiss_item("nope"))
        assert res["ok"] is False
    finally:
        _unwire(lib, saved)


def test_list_flagged_groups_by_reason():
    lib = load_lib()
    ledger, locator = _FakeColl(), _FakeColl()
    ledger.docs["f1"] = {"_id": "f1", "status": "open", "caption": "A E01",
                         "reason_code": "no-match", "reason": "no match",
                         "updated_at": 3.0}
    ledger.docs["f2"] = {"_id": "f2", "status": "unresolved", "caption": "B E02",
                         "reason_code": "no-match", "reason": "no match",
                         "updated_at": 2.0}
    ledger.docs["f3"] = {"_id": "f3", "status": "open", "caption": "C",
                         "reason_code": "no-quality", "reason": "no quality token",
                         "updated_at": 1.0}
    ledger.docs["f4"] = {"_id": "f4", "status": "dismissed", "caption": "D",
                         "reason_code": "no-match", "updated_at": 4.0}
    saved = _wire(lib, ledger=ledger, locator=locator)
    try:
        groups = run(lib.list_flagged())
        assert [g["reason_code"] for g in groups] == ["no-match", "no-quality"]
        assert groups[0]["count"] == 2 and groups[1]["count"] == 1
        assert groups[0]["sample"][0]["caption"] == "A E01"
    finally:
        _unwire(lib, saved)


# ---------------------------------------------------------------------------
# v5 Phase 3 — automation ladder (server-side enforcement)
# ---------------------------------------------------------------------------

def test_auto_apply_eligible_ladder():
    lib = load_lib()
    items = [{"key": "k1"}]
    plain = {"k1": {"caption": "Some Show S02E05", "file_name": ""}}
    special = {"k1": {"caption": "Some Show SP1 E01", "file_name": ""}}
    assert lib._auto_apply_eligible("off", items, plain) is False
    assert lib._auto_apply_eligible("safe", items, plain) is True
    assert lib._auto_apply_eligible("safe", items, special) is False
    assert lib._auto_apply_eligible("all", items, special) is True
    assert lib._auto_apply_eligible("all", [], plain) is False
    assert lib._auto_apply_eligible("bogus", items, plain) is False


def test_submit_schedules_auto_apply_only_when_permitted():
    lib = load_lib()
    ledger, proposals, saved = _seed_proposal_env(lib)
    entry = lib._stubs["entry"]
    settings = lib._stubs["settings"]

    async def fake_metadata(name, channel, msg_id, override_id=None, season_hint=None):
        return {"tmdb_id": 42, "imdb_id": "tt1234567", "title": "Some Show",
                "season_number": 2, "episode_number": 5}

    entry.metadata = fake_metadata
    calls = []
    orig_runner = lib._run_auto_apply

    async def recorder(pid, level):
        calls.append((pid, level))

    lib._run_auto_apply = recorder
    try:
        # off (stub has no librarian_auto_apply -> default off): no schedule.
        res = run(lib.submit_proposal(_plan()))
        assert res["ok"] is True and res["status"] == "pending", res
        assert res["auto_apply"] is None and calls == []

        # safe + ordinary episode: scheduled.
        settings.librarian_auto_apply = "safe"
        ledger.docs["k1"].pop("proposal_id", None)

        async def submit_and_drain():
            out = await lib.submit_proposal(_plan())
            await asyncio.sleep(0.1)
            return out

        res = run(submit_and_drain())
        assert res["auto_apply"] == "safe", res
        assert len(calls) == 1 and calls[0][1] == "safe"
        doc = proposals.docs[res["proposal_id"]]
        assert doc.get("auto_apply_state") == "scheduled"

        # safe + SP-marked original: NOT scheduled.
        calls.clear()
        ledger.docs["k1"]["caption"] = "Some Show SP1 E05"
        ledger.docs["k1"].pop("proposal_id", None)
        res = run(submit_and_drain())
        assert res["ok"] is True and res["auto_apply"] is None, res
        assert calls == []
    finally:
        lib._run_auto_apply = orig_runner
        if hasattr(settings, "librarian_auto_apply"):
            del settings.librarian_auto_apply
        entry.metadata = None
        _unwire(lib, saved)


# ---------------------------------------------------------------------
# L4/L5/L6 — policies, bot state, watermark, pre-identify, proposals
# ---------------------------------------------------------------------
def test_resolve_channel_policy_defaults_and_overrides():
    lib = load_lib()
    p = lib.resolve_channel_policy(None, -1001)
    assert p == {"auto": "inherit",
                 "scopes": {"waiting": "allow", "indexed": "allow",
                            "unindexed": "allow"}}
    stored = {"-1001": {"auto": "safe",
                        "scopes": {"indexed": "deny", "waiting": "ask",
                                   "bogus": "deny"}}}
    p = lib.resolve_channel_policy(stored, -1001)
    assert p["auto"] == "safe"
    assert p["scopes"] == {"waiting": "ask", "indexed": "deny",
                           "unindexed": "allow"}
    p = lib.resolve_channel_policy(stored, -1002)
    assert p["auto"] == "inherit" and p["scopes"]["indexed"] == "allow"
    p = lib.resolve_channel_policy({"-1001": {"auto": "wild"}}, -1001)
    assert p["auto"] == "inherit"


def test_packet_excludes_denied_scope_and_ready_needs_candidate():
    lib = load_lib()
    ledger, locator, packets = _FakeColl(), _FakeColl(), _FakeColl()
    base = {"chat_id": -1004395609351, "caption": "Show E01",
            "file_name": "Show E01.mkv"}
    ledger.docs["w1"] = {"_id": "w1", "status": "open", "first_seen": 1.0,
                         **base}
    ledger.docs["w2"] = {"_id": "w2", "status": "open", "first_seen": 2.0,
                         "candidate": {"title": "Show", "confidence": "high",
                                       "source": "alias"}, **base}
    ledger.docs["i1"] = {"_id": "i1", "status": "indexed", "first_seen": 3.0,
                         "scan_status": "indexed", **base}
    saved = _wire(lib, ledger=ledger, locator=locator, packets=packets)
    settings = lib._stubs["settings"]
    chan = -1004395609351
    try:
        settings.librarian_channel_policies = {
            str(chan): {"auto": "inherit", "scopes": {
                "waiting": "allow", "indexed": "deny",
                "unindexed": "allow"}}}
        pkt = run(lib.get_packet(chan, 10, "all"))
        assert sorted(i["key"] for i in pkt["items"]) == ["w1", "w2"], pkt
        pkt = run(lib.get_packet(chan, 10, "ready"))
        assert [i["key"] for i in pkt["items"]] == ["w2"]
        assert pkt["items"][0]["candidate"]["title"] == "Show"
    finally:
        if hasattr(settings, "librarian_channel_policies"):
            del settings.librarian_channel_policies
        _unwire(lib, saved)


def test_bot_state_blocks_packet_and_submit():
    lib = load_lib()
    ledger, proposals, saved = _seed_proposal_env(lib)
    settings = lib._stubs["settings"]
    try:
        settings.librarian_bot_state = "killed"
        pkt = run(lib.get_packet(-100111, 10))
        assert pkt["ok"] is False and "killed" in pkt["message"]
        res = run(lib.submit_proposal(_plan()))
        assert res["ok"] is False and "killed" in res["message"]
        settings.librarian_bot_state = "paused"
        pkt = run(lib.get_packet(-100111, 10))
        assert pkt["ok"] is True and pkt["paused"] is True
        assert pkt["items"] == []
        res = run(lib.submit_proposal(_plan()))
        assert res["ok"] is False and "paused" in res["message"]
    finally:
        if hasattr(settings, "librarian_bot_state"):
            del settings.librarian_bot_state
        _unwire(lib, saved)


def test_submit_refuses_denied_scope():
    lib = load_lib()
    ledger, proposals, saved = _seed_proposal_env(lib)
    settings = lib._stubs["settings"]
    try:
        settings.librarian_channel_policies = {
            "-100111": {"auto": "inherit", "scopes": {
                "waiting": "deny", "indexed": "allow",
                "unindexed": "allow"}}}
        res = run(lib.submit_proposal(_plan()))
        assert res["ok"] is False and "denies the waiting scope" in res["message"], res
    finally:
        if hasattr(settings, "librarian_channel_policies"):
            del settings.librarian_channel_policies
        _unwire(lib, saved)


def test_auto_apply_effective_levels_per_channel():
    lib = load_lib()
    items = [{"key": "k1"}]
    plain = {"k1": {"caption": "Some Show S02E05", "file_name": "",
                    "chat_id": -100111}}
    special = {"k1": {"caption": "Some Show SP1 E01", "file_name": "",
                      "chat_id": -100111}}
    pol = {"-100111": lib.resolve_channel_policy(
        {"-100111": {"auto": "off", "scopes": {}}}, -100111)}
    assert lib._auto_apply_eligible("all", items, plain, pol) is False
    pol = {"-100111": lib.resolve_channel_policy(
        {"-100111": {"auto": "all", "scopes": {"waiting": "ask"}}}, -100111)}
    assert lib._auto_apply_eligible("off", items, plain, pol) is False
    pol = {"-100111": lib.resolve_channel_policy(
        {"-100111": {"auto": "all", "scopes": {}}}, -100111)}
    assert lib._auto_apply_eligible("off", items, special, pol) is True
    pol = {"-100111": lib.resolve_channel_policy(
        {"-100111": {"auto": "safe", "scopes": {}}}, -100111)}
    assert lib._auto_apply_eligible("all", items, special, pol) is False
    assert lib._auto_apply_eligible("all", items, plain, pol) is True


def test_scan_watermark_stops_at_seen_messages():
    import sys as _sys
    import types as _types
    lib = load_lib()
    ledger, locator = _FakeColl(), _FakeColl()
    scan_state = _FakeColl()
    saved = _wire(lib, ledger=ledger, locator=locator)
    orig_scan_coll = lib._scan_state_coll
    orig_index = lib._library_index_for_channel
    lib._scan_state_coll = lambda: scan_state

    async def _no_index(channel):
        return {}

    lib._library_index_for_channel = _no_index

    class _Media:
        def __init__(self, uid):
            self.file_unique_id = uid
            self.file_name = f"{uid}.mkv"
            self.file_size = 100
            self.height = 1080

    class _Msg:
        def __init__(self, mid, uid):
            self.id = mid
            self.caption = f"Show E{mid}"
            self.video = _Media(uid)
            self.document = None
            self.audio = None
            self.animation = None
            self.photo = None

    walked = []

    class _UB:
        async def get_chat_history(self, chat_id):
            for mid in (50, 49, 48, 47):
                walked.append(mid)
                yield _Msg(mid, f"u{mid}")

    botmod = _sys.modules["Backend.pyrofork.bot"]
    orig_ub = botmod.Userbot
    botmod.Userbot = _UB()
    try:
        res = run(lib.run_channel_scan(-1004395609351))
        assert res["ok"] is True and res["files"] == 4, res
        assert res["watermark"] == 50 and res["incremental"] is False
        assert scan_state.docs["-1004395609351"]["last_msg_id"] == 50
        walked.clear()
        res = run(lib.run_channel_scan(-1004395609351))
        assert res["ok"] is True and res["messages_read"] == 0, res
        assert res["incremental"] is True and walked == [50]
    finally:
        botmod.Userbot = orig_ub
        lib._scan_state_coll = orig_scan_coll
        lib._library_index_for_channel = orig_index
        _unwire(lib, saved)


def test_preidentify_alias_candidate_and_clear():
    lib = load_lib()
    ledger, alias = _FakeColl(), _FakeColl()
    ledger.docs["w1"] = {"_id": "w1", "status": "open", "first_seen": 1.0,
                         "chat_id": -1004395609351,
                         "caption": "Some Show S02E05 1080p",
                         "file_name": "Some Show S02E05.mkv"}
    alias.docs["-1004395609351:some show"] = {
        "_id": "-1004395609351:some show", "title": "Some Show",
        "tmdb_id": 42, "imdb_id": "tt1234567", "media_type": "tv"}
    saved = _wire(lib, ledger=ledger, alias=alias)
    try:
        res = run(lib.run_preidentify())
        assert res == {"ok": True, "checked": 1, "attached": 1}, res
        cand = ledger.docs["w1"]["candidate"]
        assert cand["confidence"] == "high" and cand["tmdb_id"] == 42
        assert cand["season"] == 2 and cand["episode"] == 5
        rows = run(lib.list_preidentified())
        assert len(rows) == 1 and rows[0]["key"] == "w1"
        out = run(lib.clear_candidate("w1"))
        assert out["ok"] is True and "candidate" not in ledger.docs["w1"]
        out = run(lib.clear_candidate("nope"))
        assert out["ok"] is False
    finally:
        _unwire(lib, saved)


def test_proposal_reject_and_lazy_expiry():
    import time as _time
    lib = load_lib()
    ledger, proposals = _FakeColl(), _FakeColl()
    ledger.docs["k1"] = {"_id": "k1", "status": "open",
                         "proposal_id": "p_old", "caption": "Show E01"}
    ledger.docs["k2"] = {"_id": "k2", "status": "open",
                         "proposal_id": "p_new", "caption": "Show E02"}
    proposals.docs["p_old"] = {
        "_id": "p_old", "status": "pending", "label": "old",
        "created_at": _time.time() - 49 * 3600,
        "items": [{"key": "k1", "new_caption": "X"}]}
    proposals.docs["p_new"] = {
        "_id": "p_new", "status": "pending", "label": "new",
        "created_at": _time.time(),
        "items": [{"key": "k2", "new_caption": "Y"}]}
    saved = _wire(lib, ledger=ledger, proposals=proposals)
    try:
        out = run(lib.list_proposals())
        by_id = {p["_id"]: p for p in out}
        assert by_id["p_old"]["status"] == "expired"
        assert proposals.docs["p_old"]["status"] == "expired"
        assert ledger.docs["k1"]["proposal_id"] is None
        assert by_id["p_new"]["status"] == "pending"
        res = run(lib.reject_proposal("p_new"))
        assert res["ok"] is True
        assert proposals.docs["p_new"]["status"] == "rejected"
        assert ledger.docs["k2"]["proposal_id"] is None
        res = run(lib.reject_proposal("p_new"))
        assert res["ok"] is False
    finally:
        _unwire(lib, saved)
