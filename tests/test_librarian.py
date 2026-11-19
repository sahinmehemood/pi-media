"""Librarian Phase 1 tests: pure logic (name bits, clustering, tiers,
reason codes, initials) plus funnel/gate behaviour against stubbed
provider modules, loaded through the suite's path loader."""

import asyncio
import difflib
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
    backend.db = types.SimpleNamespace(dbs={})
    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("librarian-tests")

    common = types.ModuleType("Backend.helper.metadata.common")
    _ID_RE = re.compile(r"\btt\d{7,10}\b|themoviedb\.org/(?:movie|tv)/\d+", re.I)

    def extract_default_id(text):
        m = _ID_RE.search(text or "")
        return m.group(0) if m else None

    def score_candidate(q_title, q_year, r_title, r_year, **kw):
        return difflib.SequenceMatcher(None, (q_title or "").lower(),
                                       (r_title or "").lower()).ratio()

    common.extract_default_id = extract_default_id
    common.score_candidate = score_candidate

    entry = types.ModuleType("Backend.helper.metadata.entry")
    entry.search_any_candidates = None  # set per-test
    entry.search_movie_candidates = None
    entry.search_tv_candidates = None

    tmdb = types.ModuleType("Backend.helper.metadata.providers.tmdb")
    tmdb.details = None
    tmdb.episode_details = None
    tmdb.external_imdb_id = None

    parse = types.ModuleType("Backend.helper.metadata.parse")
    parse.analyze_metadata_failure = lambda name: ""
    parse.parse_media_name = lambda name: {}

    settings_manager = types.ModuleType("Backend.helper.settings_manager")

    class _SM:
        @staticmethod
        def current():
            return types.SimpleNamespace(librarian_enabled=True)

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
        "Backend.helper.metadata.providers":
            types.ModuleType("Backend.helper.metadata.providers"),
        "Backend.helper.metadata.providers.tmdb": tmdb,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.encrypt": encrypt,
        "Backend.pyrofork": types.ModuleType("Backend.pyrofork"),
        "Backend.pyrofork.bot": bot,
    })
    _LIB = load_module("librarian_under_test", "Backend/helper/librarian.py")
    _LIB._stubs = {"common": common, "entry": entry, "tmdb": tmdb}
    return _LIB


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# name_bits / clustering
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


def test_cluster_groups_run_and_reports_missing():
    lib = load_lib()
    items = [
        {"key": "a", "chat_id": -1001, "caption": "BTTH 3YA - SP3 E01", "file_name": ""},
        {"key": "b", "chat_id": -1001, "caption": "BTTH 3YA - SP3 E03", "file_name": ""},
        {"key": "c", "chat_id": -1001, "caption": "Inception (2010) 1080p", "file_name": ""},
    ]
    for it in items:
        it["bits"] = lib.name_bits(it["caption"], it["file_name"])
    runs = lib.cluster_items(items)
    assert len(runs) == 2
    run = next(r for r in runs if r["stem"] == "btth 3ya sp3")
    assert run["episodes"] == [1, 3]
    assert run["missing_episodes"] == [2]
    assert sorted(run["item_keys"]) == ["a", "b"]


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


def test_assign_tier():
    lib = load_lib()
    full = {"passed": True, "checks": [{"name": "id_exists", "ok": True}]}
    skipped = {"passed": True, "checks": [{"name": "id_exists", "ok": True},
                                          {"name": "episode_exists", "ok": None}]}
    failed = {"passed": False, "checks": [{"name": "id_exists", "ok": False}]}
    assert lib.assign_tier("L0", full) == "A"
    assert lib.assign_tier("L1", full) == "A"
    assert lib.assign_tier("L2", full) == "B"
    assert lib.assign_tier("L0", skipped) == "B"
    assert lib.assign_tier("L0", failed) == "C"
    assert lib.assign_tier(None, full) == "C"


# ---------------------------------------------------------------------------
# Ledger store (fake collection)
# ---------------------------------------------------------------------------

class _FakeColl:
    def __init__(self):
        self.docs = {}

    async def update_one(self, filt, update, upsert=False):
        # Mirror MongoDB: one path may not be targeted by two operators.
        seen = {}
        for op, body in update.items():
            for path in (body or {}):
                if path in seen:
                    raise ValueError(f"Updating the path '{path}' would create a conflict")
                seen[path] = op
        key = filt["_id"]
        doc = self.docs.setdefault(key, {"_id": key, "history": [], "skip_msg_ids": []})
        for k, v in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(k, v)
        for k, v in (update.get("$set") or {}).items():
            doc[k] = v
        pushed = (update.get("$push") or {}).get("history")
        if isinstance(pushed, dict):
            doc["history"].append(pushed)
        add = (update.get("$addToSet") or {}).get("skip_msg_ids")
        if add is not None and add not in doc["skip_msg_ids"]:
            doc["skip_msg_ids"].append(add)


def test_record_reject_upserts_without_duplicating():
    lib = load_lib()
    fake = _FakeColl()
    original = lib._ledger_coll
    lib._ledger_coll = lambda: fake
    try:
        fields = {"key": "uid1", "file_unique_id": "uid1", "chat_id": -1001,
                  "msg_id": 5, "origin": "live-fail", "caption": "TDH E29",
                  "reason": "No video quality", "reason_code": "no-quality",
                  "skip_msg_id": 900}
        assert run(lib.record_reject(fields)) is True
        assert run(lib.record_reject(fields)) is True
    finally:
        lib._ledger_coll = original
    assert len(fake.docs) == 1
    assert fake.docs["uid1"]["skip_msg_ids"] == [900]


# ---------------------------------------------------------------------------
# Funnel + gate with stubbed providers
# ---------------------------------------------------------------------------

def _fake_run(caption="Some Show S02E05 tt1234567"):
    lib = load_lib()
    item = {"key": "k1", "chat_id": -1001, "caption": caption, "file_name": "",
            "bits": lib.name_bits(caption, "")}
    return {"run_id": "-1001|some show", "chat_id": -1001, "stem": "some show",
            "season": 2, "special": None, "year": None, "episodes": [5],
            "missing_episodes": [], "item_keys": ["k1"], "items": [item]}


def _install_provider_fakes(lib):
    entry, tmdb = lib._stubs["entry"], lib._stubs["tmdb"]

    async def fake_search(text, year=None, limit=8):
        return [{"source": "tmdb", "media_type": "tv", "title": "Some Show",
                 "year": "2020", "imdb_id": "tt1234567", "tmdb_id": 42}]

    async def fake_details(media_type, item_id):
        return {"id": item_id, "name": "Some Show"}

    async def fake_episode(tv_id, season, episode):
        return {"episode_number": episode} if episode <= 10 else None

    async def fake_external(media_type, tmdb_id):
        return "tt1234567"

    entry.search_any_candidates = fake_search
    tmdb.details = fake_details
    tmdb.episode_details = fake_episode
    tmdb.external_imdb_id = fake_external


def test_funnel_l0_verified_tier_a():
    lib = load_lib()
    _install_provider_fakes(lib)
    outcome = run(lib.resolve_run(_fake_run(), {}))
    assert outcome["proposal"]["layer"] == "L0"
    assert outcome["gate"]["passed"] is True
    assert outcome["tier"] == "A"


def test_gate_rejects_missing_episode():
    lib = load_lib()
    _install_provider_fakes(lib)
    proposal = {"layer": "L2", "provider": "tmdb", "media_type": "tv",
                "title": "Some Show", "year": 2020, "tmdb_id": 42,
                "imdb_id": "tt1234567", "tvdb_id": None, "kitsu_id": None}
    run_obj = _fake_run()
    run_obj["episodes"] = [99]
    gate = run(lib.verify_proposal(proposal, run_obj))
    assert gate["passed"] is False
    assert any(c["name"] == "episode_exists" and c["ok"] is False
               for c in gate["checks"])


def test_l2_coherence_unique_initials_only():
    lib = load_lib()
    run_obj = _fake_run("BTTH 3YA - SP3 E01")
    run_obj["stem"] = "btth 3ya sp3"
    run_obj["chat_id"] = -1004395609351
    index = {4395609351: {
        "battle through the heavens": {
            "title": "Battle Through The Heavens", "media_type": "tv",
            "tmdb_id": 7, "imdb_id": None, "tvdb_id": None, "kitsu_id": None,
            "year": 2018},
    }}
    proposal = run(lib._l2_coherence(run_obj, index))
    assert proposal is not None and proposal["title"] == "Battle Through The Heavens"
    index[4395609351]["breaking bad tales"] = {
        "title": "Breaking Bad Tales", "media_type": "tv", "tmdb_id": 8,
        "imdb_id": None, "tvdb_id": None, "kitsu_id": None, "year": 2020}
    # 'bbt' != 'btth', still unique -> proposes; add a true second match
    # (initials also spell 'btth') and the proposal must disappear:
    index[4395609351]["beyond the thunder hill"] = {
        "title": "Beyond The Thunder Hill", "media_type": "tv",
        "tmdb_id": 9, "imdb_id": None, "tvdb_id": None, "kitsu_id": None,
        "year": 2021}
    assert run(lib._l2_coherence(run_obj, index)) is None
