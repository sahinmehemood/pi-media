"""Phase 1 tests — Librarian rejection capture (DESIGN §5/§6).

Loads the real ``librarian_store`` and the real ``skip_channel``
funnel by file path (the repo harness pattern) against stub modules
whose shapes mirror the real tree — every stubbed name was verified
against the real module it stands in for:

- ``Backend.helper.pyro.get_readable_file_size`` — algorithm copied
  from pyro.py (the suite is stdlib-only, so pyrogram cannot be
  imported here); ``clean_filename`` is the same pipeline reduced to
  its ASCII-observable behaviour (the store only uses it on the
  mechanical-title fallback path).
- ``Backend.helper.metadata.parse.parse_media_name`` — returns the
  real key set (title/year/season/episode/quality/excess) with PTN's
  observable behaviour on the test corpus: title with numbering,
  year, and quality/codec tokens removed; quality = resolution.
  The real PTN/guessit parser is exercised by the on-device gate.
- ``Backend.helper.metadata.common.caption_with_links`` — copied
  verbatim from common.py.
- ``Backend.helper.split_files.strip_part_suffix`` — the real
  trailing-numeric pattern and splice.
- ``Backend.helper.subtitles.is_subtitle_file`` — copied verbatim.
- ``Backend.helper.settings_manager.SettingsManager`` — classmethod
  ``current()`` returning a settings object with the same property
  names the code under test reads.
- ``pymongo`` — only ``ASCENDING`` and ``errors.DuplicateKeyError``
  are used by the store; both are provided with real semantics.
- The fake tracking DB implements the motor surface the store uses
  (``create_index``, ``update_one`` with ``$set``/``$setOnInsert``/
  upsert, ``find_one``) with real Mongo semantics for those
  operations, including unique-index enforcement raising
  ``DuplicateKeyError`` and filter-equality materialisation on
  upsert-insert.
"""

import copy
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run  # noqa: E402


# ------------------------------------------------------------------
# Faithful stubs
# ------------------------------------------------------------------

class DuplicateKeyError(Exception):
    pass


def _get_readable_file_size(size_in_bytes):
    # Copied from Backend/helper/pyro.py.
    size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
    if not size_in_bytes:
        return "0B"
    index, SIZE_UNITS = 0, ["B", "KB", "MB", "GB", "TB", "PB"]
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1
    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}" if index > 0 else f"{size_in_bytes:.0f}B"


def _remove_urls(text):
    if not text:
        return ""
    url_pattern = r"\b(?:(?:https?|ftp)://|www\.)[^\s/$.?#].[^\s]*"
    text = re.sub(r"\n\s*(?:IMDB|TMDB)\s*$", "", text, flags=re.I)
    text_without_urls = re.sub(url_pattern, "", text)
    return re.sub(r"\s+", " ", text_without_urls).strip()


def _clean_filename(filename):
    # pyro.py pipeline, ASCII-observable subset (emoji/decoration/
    # channel-tag/codec patterns only affect non-ASCII or tag text).
    if not filename:
        return "unknown_file"
    filename = _remove_urls(filename)
    filename = re.sub(r"[^\x20-\x7E]", " ", filename)
    filename = re.sub(r"\s+", " ", filename).strip().replace(" .", ".")
    return filename if filename else "unknown_file"


def _caption_with_links(message):
    # Copied verbatim from Backend/helper/metadata/common.py.
    urls = [e.url for e in (getattr(message, "caption_entities", None) or []) if getattr(e, "url", None)]
    return "\n".join([getattr(message, "caption", None) or "", *urls])


_TRAILING_NUMERIC = re.compile(
    r"(?i)\.(mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg|zip)\.(\d{2,3})(?=$|\D)"
)


def _strip_part_suffix(filename):
    # Backend/helper/split_files.py strip_part_suffix, via its pattern.
    if not filename:
        return filename
    name = filename.strip()
    m = _TRAILING_NUMERIC.search(name)
    if not m:
        return filename
    return (name[: m.start()] + "." + m.group(1)) if m.group(1) else (name[: m.start()] + name[m.end():])


_SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")


def _is_subtitle_file(name):
    return bool(name) and name.lower().strip().endswith(_SUBTITLE_EXTS)


_RES_RE = re.compile(r"(?i)\b(2160|1440|1080|720|576|480|360|240)p\b")
_SE_RE = re.compile(r"(?i)\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_CODEC_RE = re.compile(
    r"(?i)\b(x264|x265|h\.?264|h\.?265|hevc|avc|web-dl|webrip|bluray|bdrip|hdrip|aac|ac3|dts|flac|mp3|10bit|hdr|dv)\b"
)


def _parse_media_name(name):
    """PTN's observable output shape on the test corpus."""
    base = name or ""
    quality = None
    m = _RES_RE.search(base)
    if m:
        quality = m.group(0).lower()
    title = re.sub(r"\[[^\]]*\]", " ", base)
    title = _SE_RE.sub(" ", title)
    title = _RES_RE.sub(" ", title)
    title = _CODEC_RE.sub(" ", title)
    title = _YEAR_RE.sub(" ", title)
    title = re.sub(r"[._\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return {
        "title": title or None,
        "year": None,
        "season": None,
        "episode": None,
        "quality": quality,
        "excess": None,
    }


class FakeSettings:
    def __init__(self):
        self.librarian_capture_enabled = True
        self.skip_channel = "-1004399192319"
        self.delete_on_metadata_fail = False


class FakeSettingsManager:
    _current = FakeSettings()

    @classmethod
    def current(cls):
        return cls._current


# ------------------------------------------------------------------
# Fake Mongo (motor surface, real semantics for the used operations)
# ------------------------------------------------------------------

def _dotted_get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _dotted_set(doc, path, value):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self.indexes = []  # (keys, unique, sparse, expireAfterSeconds)
        self.raise_duplicate_on_next_upsert = False

    async def create_index(self, keys, unique=False, sparse=False, expireAfterSeconds=None):
        self.indexes.append((list(keys), unique, sparse, expireAfterSeconds))
        return "idx"

    def _matches(self, doc, filt):
        return all(_dotted_get(doc, k)[0] == v for k, v in filt.items())

    def _check_unique(self, candidate, ignore=None):
        for keys, unique, _sparse, _ttl in self.indexes:
            if not unique:
                continue
            paths = [k for k, _ in keys]
            vals = tuple(_dotted_get(candidate, p)[0] for p in paths)
            if any(v is None for v in vals):
                continue
            for doc in self.docs:
                if doc is ignore:
                    continue
                if tuple(_dotted_get(doc, p)[0] for p in paths) == vals:
                    raise DuplicateKeyError(f"E11000 duplicate key on {paths}")

    async def update_one(self, filt, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, filt):
                for k, v in update.get("$set", {}).items():
                    _dotted_set(doc, k, copy.deepcopy(v))
                return types.SimpleNamespace(matched_count=1, upserted_id=None)
        if not upsert:
            return types.SimpleNamespace(matched_count=0, upserted_id=None)
        new_doc = {}
        for k, v in filt.items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$setOnInsert", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$set", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        if self.raise_duplicate_on_next_upsert:
            # A concurrent insert won the race: its row lands, ours raises.
            self.raise_duplicate_on_next_upsert = False
            self._check_unique(new_doc)  # winner may itself be the conflict
            self.docs.append(new_doc)
            raise DuplicateKeyError("E11000 simulated race")
        self._check_unique(new_doc)
        self.docs.append(new_doc)
        return types.SimpleNamespace(matched_count=0, upserted_id=id(new_doc))

    async def find_one(self, filt):
        for doc in self.docs:
            if self._matches(doc, filt):
                return copy.deepcopy(doc)
        return None


class FakeTracking:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


class FakeDatabase:
    def __init__(self):
        self.dbs = {"tracking": FakeTracking()}
        self.index_ids = {}  # (stripped_chat, msg_id) -> (imdb, tmdb, type)

    async def get_media_ids_by_part(self, channel, msg_id):
        return self.index_ids.get((channel, msg_id))


# ------------------------------------------------------------------
# Module wiring
# ------------------------------------------------------------------

fake_db = FakeDatabase()
_delete_events = []


def _install_stubs():
    import logging

    pymongo = types.ModuleType("pymongo")
    pymongo.ASCENDING = 1
    errors = types.ModuleType("pymongo.errors")
    errors.DuplicateKeyError = DuplicateKeyError
    pymongo.errors = errors

    backend = types.ModuleType("Backend")
    backend.db = fake_db
    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("librarian-tests")

    helper = types.ModuleType("Backend.helper")
    metadata_pkg = types.ModuleType("Backend.helper.metadata")

    common = types.ModuleType("Backend.helper.metadata.common")
    common.caption_with_links = _caption_with_links
    parse = types.ModuleType("Backend.helper.metadata.parse")
    parse.parse_media_name = _parse_media_name
    pyro = types.ModuleType("Backend.helper.pyro")
    pyro.clean_filename = _clean_filename
    pyro.get_readable_file_size = _get_readable_file_size
    split_files = types.ModuleType("Backend.helper.split_files")
    split_files.strip_part_suffix = _strip_part_suffix
    subtitles = types.ModuleType("Backend.helper.subtitles")
    subtitles.is_subtitle_file = _is_subtitle_file
    settings_manager = types.ModuleType("Backend.helper.settings_manager")
    settings_manager.SettingsManager = FakeSettingsManager
    task_manager = types.ModuleType("Backend.helper.task_manager")

    async def delete_message(chat_id, msg_id):
        _delete_events.append((chat_id, msg_id))

    task_manager.delete_message = delete_message

    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = object
    pg_errors = types.ModuleType("pyrogram.errors")

    class FloodWait(Exception):
        def __init__(self, value=0):
            super().__init__(f"FloodWait {value}")
            self.value = value

    pg_errors.FloodWait = FloodWait
    pg_types = types.ModuleType("pyrogram.types")
    pg_types.Message = object
    pyrogram.errors = pg_errors
    pyrogram.types = pg_types

    mods = {
        "pymongo": pymongo,
        "pymongo.errors": errors,
        "Backend": backend,
        "Backend.logger": logger_mod,
        "Backend.helper": helper,
        "Backend.helper.metadata": metadata_pkg,
        "Backend.helper.metadata.common": common,
        "Backend.helper.metadata.parse": parse,
        "Backend.helper.pyro": pyro,
        "Backend.helper.split_files": split_files,
        "Backend.helper.subtitles": subtitles,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.task_manager": task_manager,
        "pyrogram": pyrogram,
        "pyrogram.errors": pg_errors,
        "pyrogram.types": pg_types,
    }
    sys.modules.update(mods)
    helper.metadata = metadata_pkg
    metadata_pkg.common = common
    metadata_pkg.parse = parse


_install_stubs()
store = load_module("Backend.helper.librarian_store", "Backend/helper/librarian_store.py")
funnel = load_module("Backend.helper.skip_channel", "Backend/helper/skip_channel.py")


def _reset():
    fake_db.dbs["tracking"] = FakeTracking()
    fake_db.index_ids = {}
    FakeSettingsManager._current = FakeSettings()
    store._indexes_ready = False
    _delete_events.clear()


def _ledger():
    return fake_db.dbs["tracking"]["reject_ledger"]


class FakeMedia:
    def __init__(self, file_name, file_size=1_500_000_000, file_unique_id="uniq1"):
        self.file_name = file_name
        self.file_size = file_size
        self.file_unique_id = file_unique_id


class FakeMessage:
    def __init__(self, chat_id=-1003966209940, msg_id=101, caption=None, media=None,
                 entities=None, copy_error=None, copy_errors=None, events=None):
        self.chat = types.SimpleNamespace(id=chat_id)
        self.id = msg_id
        self.caption = caption
        self.caption_entities = entities or []
        self.video = None
        self.document = media
        if copy_errors is not None:
            self._copy_errors = list(copy_errors)
        elif copy_error is not None:
            self._copy_errors = [copy_error]
        else:
            self._copy_errors = []
        self._events = events

    async def copy(self, chat_id):
        if self._events is not None:
            self._events.append(("copy", len(_ledger().docs)))
        if self._copy_errors:
            raise self._copy_errors.pop(0)
        return types.SimpleNamespace(id=9000 + self.id, chat=types.SimpleNamespace(id=-1004399192319))


def _msg(**kw):
    kw.setdefault("media", FakeMedia("Some Show S01E01 1080p WEB-DL x264.mkv"))
    kw.setdefault("caption", "Some Show S01E01 1080p WEB-DL x264.mkv")
    return FakeMessage(**kw)


# ------------------------------------------------------------------
# Capture tests
# ------------------------------------------------------------------

def test_capture_writes_complete_row():
    _reset()

    async def go():
        result = await store.capture_rejection(_msg(), "live-fail")
        assert result == {"chat_id": -1003966209940, "msg_id": 101}
        assert len(_ledger().docs) == 1
        row = _ledger().docs[0]
        assert row["source_locator"] == {"chat_id": -1003966209940, "msg_id": 101}
        assert row["current_locator"] == row["source_locator"]
        assert row["skip_locator"] is None
        assert row["chat_id"] == -1003966209940
        assert row["status"] == "waiting"
        assert row["origin"] == "live-fail"
        assert row["file_unique_id"] == "uniq1"
        assert row["size"] == 1_500_000_000
        assert row["stem"] == "some show"
        assert row["special_flag"] is False
        assert row["identity"] == {
            "quality": "1080p",
            "norm_name": "some show",
            "size": _get_readable_file_size(1_500_000_000),
        }
        assert row["created_at"] is not None and row["updated_at"] is not None

    run(go())


def test_capture_disabled_writes_nothing():
    _reset()
    FakeSettingsManager._current.librarian_capture_enabled = False

    async def go():
        assert await store.capture_rejection(_msg(), "live-fail") is None
        assert _ledger().docs == []

    run(go())


def test_capture_subtitle_excluded():
    _reset()

    async def go():
        m = _msg(media=FakeMedia("Some Show S01E01.srt"), caption="Some Show S01E01.srt")
        assert await store.capture_rejection(m, "scan-fail") is None
        assert _ledger().docs == []

    run(go())


def test_capture_negative_tmdb_excluded():
    _reset()
    fake_db.index_ids[(3966209940, 101)] = ("tt1234567", -42, "movie")

    async def go():
        assert await store.capture_rejection(_msg(), "live-fail") is None
        assert _ledger().docs == []

    run(go())


def test_capture_positive_index_entry_still_captured():
    _reset()
    fake_db.index_ids[(3966209940, 101)] = ("tt1234567", 12345, "movie")

    async def go():
        assert await store.capture_rejection(_msg(), "live-fail") is not None
        assert len(_ledger().docs) == 1

    run(go())


def test_repeat_capture_no_duplicates():
    _reset()

    async def go():
        for _ in range(3):
            await store.capture_rejection(_msg(), "scan-fail")
        assert len(_ledger().docs) == 1
        identities = {
            (r["identity"]["quality"], r["identity"]["norm_name"], r["identity"]["size"])
            for r in _ledger().docs
        }
        assert len(identities) == 1

    run(go())


def test_refresh_preserves_parked_escalated_fixed():
    _reset()

    async def go():
        await store.capture_rejection(_msg(), "live-fail")
        row = _ledger().docs[0]
        # Park it, then re-capture: status + reason survive, descriptive
        # fields refresh (§5 refresh contract).
        row["status"] = "parked"
        row["park_reason"] = "multipart release"
        m2 = _msg(media=FakeMedia("Some Show S01E01 720p.mkv"), caption="Some Show S01E01 720p.mkv")
        await store.capture_rejection(m2, "scan-fail")
        assert len(_ledger().docs) == 1
        row = _ledger().docs[0]
        assert row["status"] == "parked"
        assert row["park_reason"] == "multipart release"
        assert row["identity"]["quality"] == "720p"
        # Escalated: same protection.
        row["status"] = "escalated"
        row["escalation_reason"] = "ghost"
        await store.capture_rejection(_msg(), "scan-fail")
        assert _ledger().docs[0]["status"] == "escalated"
        assert _ledger().docs[0]["escalation_reason"] == "ghost"
        # Fixed: a rescan refreshes descriptively, never re-queues.
        row["status"] = "fixed"
        await store.capture_rejection(_msg(), "scan-fail")
        assert _ledger().docs[0]["status"] == "fixed"
        assert len(_ledger().docs) == 1

    run(go())


def test_refresh_never_overwrites_rejection_caption():
    # Regression (Phase 1 review B1): the stored caption is the caption
    # AT REJECTION — a re-capture after the post was edited must not
    # replace it, nor the file_unique_id (§5 refresh list).
    _reset()

    async def go():
        await store.capture_rejection(_msg(), "live-fail")
        edited = _msg(
            media=FakeMedia("Some Show S01E01 1080p WEB-DL x264.mkv", file_unique_id="uniq2"),
            caption="Some Show S01E01 1080p FIXED.mkv",
        )
        await store.capture_rejection(edited, "scan-fail")
        row = _ledger().docs[0]
        assert row["caption"] == "Some Show S01E01 1080p WEB-DL x264.mkv"
        assert row["file_unique_id"] == "uniq1"
        assert len(_ledger().docs) == 1

    run(go())


def test_skip_locator_set_and_never_cleared():
    _reset()

    async def go():
        await store.capture_rejection(_msg(), "live-fail")
        await store.set_skip_locator(-1003966209940, 101, -1004399192319, 9101)
        assert _ledger().docs[0]["skip_locator"] == {"chat_id": -1004399192319, "msg_id": 9101}
        # A later capture that made no copy must not clear it.
        await store.capture_rejection(_msg(), "scan-fail")
        assert _ledger().docs[0]["skip_locator"] == {"chat_id": -1004399192319, "msg_id": 9101}
        # No row -> no-op, no exception.
        await store.set_skip_locator(-1003966209940, 999, -1004399192319, 1)

    run(go())


def test_concurrent_insert_race_retried_as_refresh():
    _reset()

    async def go():
        await store.ensure_indexes()
        _ledger().raise_duplicate_on_next_upsert = True
        result = await store.capture_rejection(_msg(), "live-fail")
        assert result is not None
        assert len(_ledger().docs) == 1
        assert _ledger().docs[0]["status"] == "waiting"

    run(go())


def test_split_parts_share_stem_and_identity():
    _reset()

    async def go():
        m1 = _msg(msg_id=201, media=FakeMedia("Some Show S01E01 1080p.mkv.001"),
                  caption="Some Show S01E01 1080p.mkv.001")
        m2 = _msg(msg_id=202, media=FakeMedia("Some Show S01E01 1080p.mkv.002"),
                  caption="Some Show S01E01 1080p.mkv.002")
        await store.capture_rejection(m1, "scan-fail")
        await store.capture_rejection(m2, "scan-fail")
        assert len(_ledger().docs) == 2  # one row per source post
        stems = {r["stem"] for r in _ledger().docs}
        assert stems == {"some show"}
        identities = {
            (r["identity"]["quality"], r["identity"]["norm_name"], r["identity"]["size"])
            for r in _ledger().docs
        }
        assert len(identities) == 1

    run(go())


def test_special_flag_detection():
    assert store.detect_special("BTTH 3YA - SP3 E02.mkv") is True
    assert store.detect_special("Some Show OVA 01.mkv") is True
    assert store.detect_special("Some Show Special.mkv") is True
    assert store.detect_special("Some Show S01E02 1080p.mkv") is False
    assert store.detect_special("Novak Djokovic Story.mkv") is False
    assert store.detect_special("") is False


def test_caption_stored_in_canonical_form():
    _reset()
    entity = types.SimpleNamespace(url="https://www.imdb.com/title/tt1234567/")

    async def go():
        m = _msg(caption="Some Show S01E01 1080p.mkv\nIMDB", entities=[entity])
        await store.capture_rejection(m, "live-fail")
        stored = _ledger().docs[0]["caption"]
        assert stored == "Some Show S01E01 1080p.mkv\nIMDB\nhttps://www.imdb.com/title/tt1234567/"

    run(go())


def test_ensure_indexes_creates_full_set():
    _reset()

    def keysets(coll):
        return {tuple(k for k, _ in ix[0]): ix for ix in coll.indexes}

    async def go():
        await store.ensure_indexes()
        tracking = fake_db.dbs["tracking"]

        ledger = keysets(tracking["reject_ledger"])
        assert ledger[("source_locator.chat_id", "source_locator.msg_id")][1] is True
        assert ("status", "chat_id") in ledger
        assert ("chat_id", "stem") in ledger

        records = keysets(tracking["series_records"])
        for field in ("ids.tmdb", "ids.tvdb", "ids.anilist", "ids.mal", "ids.kitsu", "ids.imdb"):
            assert (field,) in records, f"missing sparse index {field}"
            assert records[(field,)][2] is True, f"{field} index must be sparse"
        assert records[("canonical_key",)][1] is True

        alias = keysets(tracking["alias_map"])
        assert alias[("channel_id", "stem")][1] is True

        fixes = keysets(tracking["librarian_fixes"])
        assert fixes[("request_id",)][1] is True
        assert ("batch_id",) in fixes
        assert ("state", "created_at") in fixes
        assert ("stem", "created_at") in fixes

        cache = keysets(tracking["provider_cache"])
        assert cache[("key",)][1] is True
        assert cache[("expires_at",)][3] == 0, "provider_cache needs the expires_at TTL index"

    run(go())


# ------------------------------------------------------------------
# Funnel integration tests (real skip_channel.py)
# ------------------------------------------------------------------

def test_funnel_captures_before_copy_and_fills_skip_locator():
    _reset()
    events = []

    async def go():
        m = _msg(events=events)
        await funnel.route_to_skip_channel(object(), m, origin="live-fail")
        # Capture ran before the copy: the row already existed when
        # copy() was invoked (it observed 1 ledger doc).
        assert events == [("copy", 1)]
        assert _ledger().docs[0]["skip_locator"] == {"chat_id": -1004399192319, "msg_id": 9101}

    run(go())


def test_funnel_floodwait_retry_fills_skip_locator():
    _reset()
    FloodWait = sys.modules["pyrogram.errors"].FloodWait

    async def go():
        m = _msg(copy_errors=[FloodWait(0)])
        await funnel.route_to_skip_channel(object(), m)
        assert len(_ledger().docs) == 1
        assert _ledger().docs[0]["skip_locator"] == {"chat_id": -1004399192319, "msg_id": 9101}

    run(go())


def test_funnel_copy_failure_still_captures():
    _reset()

    async def go():
        m = _msg(copy_error=RuntimeError("copy boom"))
        await funnel.route_to_skip_channel(object(), m)
        assert len(_ledger().docs) == 1
        assert _ledger().docs[0]["skip_locator"] is None

    run(go())


def test_funnel_no_skip_channel_still_captures():
    _reset()
    FakeSettingsManager._current.skip_channel = ""
    events = []

    async def go():
        m = _msg(events=events)
        await funnel.route_to_skip_channel(object(), m)
        assert len(_ledger().docs) == 1
        assert events == []  # copy never attempted

    run(go())


def test_funnel_capture_failure_isolated():
    _reset()
    original = store.capture_rejection

    async def boom(message, origin):
        raise RuntimeError("db down")

    store.capture_rejection = boom
    try:
        events = []

        async def go():
            m = _msg(events=events)
            await funnel.route_to_skip_channel(object(), m)
            assert events == [("copy", 0)]  # funnel proceeded despite capture failing

        run(go())
    finally:
        store.capture_rejection = original


def test_funnel_delete_never_precedes_capture():
    _reset()
    FakeSettingsManager._current.delete_on_metadata_fail = True
    events = []

    async def go():
        m = _msg(events=events)
        await funnel.route_to_skip_channel(object(), m)
        assert events == [("copy", 1)]  # row existed before copy; delete happens after
        assert _delete_events == [(-1003966209940, 101)]

    run(go())


# ------------------------------------------------------------------
# A-1: engine-only ledger transitions (§2.3) + budget-reservation
# release (§2.3a)
# ------------------------------------------------------------------

def _seed_ledger(status, chat_id=-100111, msg_id=7):
    _ledger().docs.append({
        "source_locator": {"chat_id": chat_id, "msg_id": msg_id},
        "current_locator": {"chat_id": chat_id, "msg_id": msg_id},
        "skip_locator": None, "chat_id": chat_id, "status": status,
        "origin": "live-fail", "park_reason": None,
        "escalation_reason": None,
    })


def _fixes():
    return fake_db.dbs["tracking"]["librarian_fixes"]


def _seed_fix(request_id="rq-a1", state="prepared", budget_counted=True):
    _fixes().docs.append({"request_id": request_id, "state": state,
                          "budget_counted": budget_counted})


def test_mark_ledger_fixed_happy():
    _reset()
    _seed_ledger("waiting")

    async def go():
        row, current = await store.mark_ledger_fixed(-100111, 7)
        assert current == "waiting"
        assert row["status"] == "fixed"
        assert _ledger().docs[0]["status"] == "fixed"

    run(go())


def test_mark_ledger_fixed_wrong_status_no_write():
    for status in ("parked", "escalated", "fixed"):
        _reset()
        _seed_ledger(status)

        async def go(status=status):
            row, current = await store.mark_ledger_fixed(-100111, 7)
            assert current == status
            assert row["status"] == status
            assert _ledger().docs[0]["status"] == status  # no write

        run(go())


def test_mark_ledger_fixed_missing_row():
    _reset()

    async def go():
        assert await store.mark_ledger_fixed(-100111, 7) == (None, None)

    run(go())


def test_return_ledger_to_waiting_happy():
    _reset()
    _seed_ledger("fixed")

    async def go():
        row, current = await store.return_ledger_to_waiting(-100111, 7)
        assert current == "fixed"
        assert row["status"] == "waiting"
        assert _ledger().docs[0]["status"] == "waiting"

    run(go())


def test_return_ledger_to_waiting_wrong_status_no_write():
    for status in ("waiting", "parked", "escalated"):
        _reset()
        _seed_ledger(status)

        async def go(status=status):
            row, current = await store.return_ledger_to_waiting(-100111, 7)
            assert current == status
            assert row["status"] == status
            assert _ledger().docs[0]["status"] == status  # no write

        run(go())


def test_return_ledger_to_waiting_missing_row():
    _reset()

    async def go():
        assert await store.return_ledger_to_waiting(-100111, 7) == (None, None)

    run(go())


def test_ledger_transitions_key_set_is_command_surface_only():
    # DISP-1a's guard (Senior criterion): the command table must
    # never gain a fix/un-fix transition — the engine's transitions
    # are the two store functions above, not table entries.
    assert set(store._LEDGER_TRANSITIONS) == {"park", "unpark", "escalate"}


def test_release_budget_reservation_releases_prepared_counted():
    _reset()
    _seed_fix()

    async def go():
        assert await store.release_budget_reservation("rq-a1") is True
        assert _fixes().docs[0]["budget_counted"] is False
        # A second release finds nothing counted: no write, False.
        assert await store.release_budget_reservation("rq-a1") is False

    run(go())


def test_release_budget_reservation_no_write_other_states():
    # Any post-write state (editing or beyond) with a counted
    # reservation: the reservation stands — no write, False.
    for state in ("editing", "confirming", "done", "edit_failed",
                  "refused_precondition"):
        _reset()
        _seed_fix(state=state, budget_counted=True)

        async def go(state=state):
            assert await store.release_budget_reservation("rq-a1") is False
            assert _fixes().docs[0]["budget_counted"] is True  # no write
            assert _fixes().docs[0]["state"] == state

        run(go())


def test_release_budget_reservation_prepared_uncounted_and_missing():
    _reset()
    _seed_fix(state="prepared", budget_counted=False)

    async def go():
        assert await store.release_budget_reservation("rq-a1") is False
        assert _fixes().docs[0]["budget_counted"] is False
        assert await store.release_budget_reservation("rq-absent") is False

    run(go())


def test_release_budget_reservation_has_no_call_site_yet():
    # §2.3a's call-site restriction, A-1 stage: the function is
    # defined exactly once (in the store) and NOTHING calls it
    # yet — its sole call site lands with WS-A's DISP-2 skip-copy
    # branch, which replaces this pin with its own.
    store_src = Path("Backend/helper/librarian_store.py").read_text()
    assert store_src.count("release_budget_reservation") == 1
    for path in Path("Backend").rglob("*.py"):
        if path.name == "librarian_store.py":
            continue
        assert "release_budget_reservation" not in path.read_text(), path
