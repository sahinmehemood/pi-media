"""Phase 5 Workstream B tests — the re-index seam (phase5-plan §10.3).

Loads the REAL ``librarian_reindex`` bridge and the REAL
``receiver.py`` seam/handler code by file path, on top of the
Phase 3 harness (``test_librarian_api``: fake Mongo, fake settings,
the real ``librarian_store`` and ``librarian_routes``), extended
with production-shaped doubles at the exact module boundaries
``receiver.py`` imports — every stubbed name verified against the
real module it stands in for:

- ``pyrogram.Client`` — a class whose ``on_message`` /
  ``on_edited_message`` / ``on_deleted_messages`` are identity
  decorators (the real Client registers the function and returns
  it); ``pyrogram.filters`` — filter objects supporting ``&``/``|``
  (receiver composes them at import).
- ``Backend.helper.metadata`` package attrs — ``caption_with_links``
  / ``extract_default_id`` / ``has_raw_ref`` are faithful reductions
  of metadata/common.py:280-303 (verified line-by-line);
  ``metadata`` is a scriptable async double with entry.py:63's
  signature.
- ``Backend.helper.metadata.resolvers.resolve_series`` — scriptable
  double, resolvers.py:77's signature; ``...providers.tvdb``'s
  ``episode_by_absolute`` — scriptable double, tvdb.py:446's
  signature returning TVDB-shaped rows (``seasonNumber``/``number``).
- ``Backend.db`` methods — ``get_media_ids_by_part`` /
  ``rename_media_part`` / ``remove_media_part`` / ``insert_media``
  doubles operating on the fake storage collections in the REAL
  stored shape (modal.py: single-file qualities carry
  ``id = encoded_string`` and no parts list; TV docs carry
  ``season_number`` / ``episode_number`` / ``absolute_episode``),
  mirroring database.py:1247/1277/1306/1375 contracts, including
  duplicate protection's ``status["duplicate_skipped"]``.
- The import-time ``create_task(process_file())`` is captured by a
  delegating ``asyncio.create_task`` patch installed only for the
  load: with no running loop it closes the coroutine (the import
  side effect §8 warns about); inside a loop it schedules for real,
  so receiver's runtime create_task calls behave as production's.
"""

import asyncio
import copy
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module  # noqa: E402

bridge = load_module("Backend.helper.librarian_reindex",
                     "Backend/helper/librarian_reindex.py")

import test_librarian_api as api  # noqa: E402  (installs the harness)

store = api.store
routes = api.routes
fake_db = api.fake_db


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Faithful reductions (metadata/common.py, verified against source)
# ------------------------------------------------------------------

_ID_REFS = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:imdb\.com/title/tt\d+"
    r"|themoviedb\.org/(?:movie|tv)/\d+)\S*|\btt\d{7,10}\b", re.I)


def _caption_with_links(message):
    urls = [e.url for e in (getattr(message, "caption_entities", None) or [])
            if getattr(e, "url", None)]
    return "\n".join([getattr(message, "caption", None) or "", *urls])


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


def _has_raw_ref(text):
    return bool(_ID_REFS.search(text or ""))


def _parse_media_name(name):
    """Reduction of metadata/parse.py:30 for fixture shapes: title,
    year, SxxExx season/episode, quality — the fields the seam's
    placement path consumes."""
    base = name or ""
    season = episode = None
    m = re.search(r"(?i)\bS(\d{1,2})E(\d{1,3})\b", base)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    quality = None
    qm = re.search(r"(?i)\b(2160|1440|1080|720|576|480|360|240)p\b", base)
    if qm:
        quality = qm.group(0).lower()
    year = None
    ym = re.search(r"\b(19|20)\d{2}\b", base)
    if ym:
        year = int(ym.group(0))
    title = re.sub(r"\[[^\]]*\]", " ", base)
    title = re.sub(r"(?i)\bS\d{1,2}E\d{1,3}\b", " ", title)
    title = re.sub(r"(?i)\b(?:2160|1440|1080|720|576|480|360|240)p\b", " ", title)
    title = re.sub(r"\b(19|20)\d{2}\b", " ", title)
    title = re.sub(r"[._\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return {"title": title or None, "year": year, "season": season,
            "episode": episode, "quality": quality, "excess": None}


def _finalize_media_name(title, is_split=False):
    """Reduction of pyro.py:284: strip URLs, cut at the media
    extension, ensure a video extension."""
    title = re.sub(r"https?://\S+", " ", title or "")
    title = re.sub(r"\s+", " ", title).strip().replace(" .", ".")
    m = re.search(r"\.(?:mkv|mp4)(?:\.\d{2,3})?(?=$|[^A-Za-z0-9])",
                  title, flags=re.IGNORECASE)
    if m:
        title = title[:m.end()]
    if not title.endswith((".mkv", ".mp4")):
        title += ".mkv"
    return title


def _parse_split_info(filename):
    """Reduction of split_files.py:35 for the trailing .NNN form."""
    if not filename:
        return None
    m = re.search(r"\.(mkv|mp4|avi|zip)\.(\d{2,3})$", filename.strip(),
                  flags=re.IGNORECASE)
    if not m:
        return None
    return (filename[:m.start()] + "." + m.group(1)), int(m.group(2))


# ------------------------------------------------------------------
# Scriptable doubles (module-boundary state)
# ------------------------------------------------------------------

META = {"result": None, "calls": []}
RESOLVE = {"result": None, "calls": []}
TVDB_ABS = {"result": None, "calls": []}
INSERT_CALLS = []
INSERT_RAISES = [None]
REMOVE_CALLS = []
RENAME_CALLS = []
STAMP_CALLS = []
ROUTE_SKIP_CALLS = []


async def _metadata_double(filename, channel, msg_id, override_id=None,
                           season_hint=None):
    META["calls"].append((filename, channel, msg_id, override_id))
    result = META["result"]
    return copy.deepcopy(result) if result is not None else None


async def _resolve_series_double(title, season, episode, encoded_string,
                                 year=None, quality=None, default_id=None):
    RESOLVE["calls"].append({"title": title, "season": season,
                             "episode": episode, "encoded": encoded_string,
                             "year": year, "quality": quality,
                             "default_id": default_id})
    result = RESOLVE["result"]
    return copy.deepcopy(result) if result is not None else None


async def _episode_by_absolute_double(tvdb_id, absolute):
    TVDB_ABS["calls"].append((tvdb_id, absolute))
    result = TVDB_ABS["result"]
    return copy.deepcopy(result) if result is not None else None


async def _encode_string(payload):
    return f"enc:{payload['chat_id']}:{payload['msg_id']}"


async def _stamp_double(message, metadata_info):
    STAMP_CALLS.append((message.id, dict(metadata_info)))


async def _route_skip_double(client, message, origin="live-fail"):
    ROUTE_SKIP_CALLS.append((message.id, origin))


# ------------------------------------------------------------------
# Install the receiver-boundary stubs, then load the real receiver
# ------------------------------------------------------------------

def _install_receiver_stubs():
    pyrogram = sys.modules["pyrogram"]

    class _Filter:
        def __and__(self, other):
            return self

        def __or__(self, other):
            return self

    filters_mod = types.ModuleType("pyrogram.filters")
    filters_mod.channel = _Filter()
    filters_mod.document = _Filter()
    filters_mod.video = _Filter()
    sys.modules["pyrogram.filters"] = filters_mod
    pyrogram.filters = filters_mod

    class _Client:
        @staticmethod
        def _identity_decorator(*args, **kwargs):
            def deco(fn):
                return fn
            return deco

        on_message = _identity_decorator
        on_edited_message = _identity_decorator
        on_deleted_messages = _identity_decorator

    pyrogram.Client = _Client

    parse_mode_mod = types.ModuleType("pyrogram.enums.parse_mode")
    parse_mode_mod.ParseMode = types.SimpleNamespace(MARKDOWN="markdown")
    sys.modules["pyrogram.enums.parse_mode"] = parse_mode_mod
    sys.modules["pyrogram.enums"].parse_mode = parse_mode_mod

    backend = sys.modules["Backend"]
    backend.MANUAL_SESSION = None
    helper_pkg = sys.modules["Backend.helper"]
    helper_pkg.librarian_reindex = bridge

    # metadata package + submodules
    md_pkg = sys.modules["Backend.helper.metadata"]
    md_pkg.caption_with_links = _caption_with_links
    md_pkg.extract_default_id = _extract_default_id
    md_pkg.has_raw_ref = _has_raw_ref
    md_pkg.metadata = _metadata_double
    md_common = sys.modules["Backend.helper.metadata.common"]
    md_common.caption_with_links = _caption_with_links
    md_common.extract_default_id = _extract_default_id
    md_common.has_raw_ref = _has_raw_ref
    md_parse = sys.modules["Backend.helper.metadata.parse"]
    md_parse.parse_media_name = _parse_media_name

    resolvers_mod = types.ModuleType("Backend.helper.metadata.resolvers")
    resolvers_mod.resolve_series = _resolve_series_double
    sys.modules["Backend.helper.metadata.resolvers"] = resolvers_mod
    md_pkg.resolvers = resolvers_mod

    providers_pkg = types.ModuleType("Backend.helper.metadata.providers")
    tvdb_mod = types.ModuleType("Backend.helper.metadata.providers.tvdb")
    tvdb_mod.episode_by_absolute = _episode_by_absolute_double
    providers_pkg.tvdb = tvdb_mod
    sys.modules["Backend.helper.metadata.providers"] = providers_pkg
    sys.modules["Backend.helper.metadata.providers.tvdb"] = tvdb_mod
    md_pkg.providers = providers_pkg

    pyro = sys.modules["Backend.helper.pyro"]
    pyro.finalize_media_name = _finalize_media_name

    async def _apply_thumb(metadata_info, message, encoded, client=None):
        metadata_info.setdefault("_thumb_applied", True)

    pyro.apply_video_thumb_to_metadata = _apply_thumb
    pyro.resolve_video_thumb_url = lambda client, message, encoded: _async_str()
    split_mod = sys.modules["Backend.helper.split_files"]
    split_mod.parse_split_info = _parse_split_info
    subs_mod = sys.modules["Backend.helper.subtitles"]

    async def _ingest_subtitle(name, channel, msg_id):
        return True

    async def _remove_subtitle(channel, msg_id):
        return False

    subs_mod.ingest_subtitle = _ingest_subtitle
    subs_mod.remove_subtitle = _remove_subtitle

    settings_mod = sys.modules["Backend.helper.settings_manager"]

    skip_mod = types.ModuleType("Backend.helper.skip_channel")

    def _is_skip_channel(message):
        skip = settings_mod.SettingsManager.current().skip_channel
        if not skip:
            return False
        ref = str(skip).strip()
        if ref.lstrip("@-").replace("-100", "").isdigit():
            return (ref.replace("-100", "").lstrip("@")
                    == str(message.chat.id).replace("-100", ""))
        username = (getattr(message.chat, "username", None) or "").lower()
        return bool(username) and ref.lstrip("@").lower() == username

    skip_mod.is_skip_channel = _is_skip_channel
    skip_mod.route_to_skip_channel = _route_skip_double
    sys.modules["Backend.helper.skip_channel"] = skip_mod
    helper_pkg.skip_channel = skip_mod

    task_mod = sys.modules.get("Backend.helper.task_manager")
    if task_mod is None or not hasattr(task_mod, "delete_message"):
        task_mod = types.ModuleType("Backend.helper.task_manager")

        async def _delete_message(chat_id, msg_id):
            return True

        task_mod.delete_message = _delete_message
        sys.modules["Backend.helper.task_manager"] = task_mod
    helper_pkg.task_manager = task_mod

    for name, attr, fn in (
            ("Backend.helper.announcer", "announce_new_media",
             lambda info: None),
            ("Backend.helper.auto_catalog", "start_single_media_catalog_sync",
             lambda db, tmdb_id=None, media_type=None: None),
            ("Backend.helper.encrypt", "encode_string", _encode_string),
            ("Backend.helper.manual_add", "stamp_caption_with_id", _stamp_double),
    ):
        mod = sys.modules[name]
        setattr(mod, attr, fn)
        setattr(helper_pkg, name.rsplit(".", 1)[1], mod)

    manual_add = sys.modules["Backend.helper.manual_add"]

    async def _resolve_tg_message(client, chat_id=None, msg_id=None):
        return {"chat_id": chat_id, "msg_id": msg_id, "name": "x",
                "quality": "HD", "size": "1.00GB", "raw_size": 1}

    manual_add.resolve_telegram_message = _resolve_tg_message
    requests_mod = sys.modules["Backend.helper.requests_manager"]

    async def _auto_fulfill(tmdb_id=None, imdb_id=None, media_type=None):
        return None

    requests_mod.auto_fulfill = _auto_fulfill


async def _async_str():
    return ""


_install_receiver_stubs()

_real_create_task = asyncio.create_task
_SCHEDULED = []


def _capturing_create_task(coro, **kwargs):
    _SCHEDULED.append(getattr(coro, "cr_code", None) and coro.cr_code.co_name)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    return _real_create_task(coro, **kwargs)


asyncio.create_task = _capturing_create_task
try:
    receiver = load_module("receiver_under_test",
                           "Backend/pyrofork/plugins/receiver.py")
finally:
    asyncio.create_task = _real_create_task

REAL_IMPL = receiver.reindex_one_post


# ------------------------------------------------------------------
# Fake-database doubles (database.py contracts, real stored shapes)
# ------------------------------------------------------------------

def _dup_key(quality):
    name = re.sub(r"\s+", " ", str(quality.get("name") or "").strip().lower())
    size = str(quality.get("size") or "").strip().lower()
    return (quality.get("quality"), name, size)


def _storage():
    return fake_db.dbs["storage_1"]


def _all_qualities(doc):
    if "telegram" in doc:
        return list(doc.get("telegram") or [])
    return [q for s in doc.get("seasons") or []
            for e in s.get("episodes") or [] for q in e.get("telegram") or []]


def _find_doc(coll, tmdb_id, imdb_id):
    for doc in _storage()[coll].docs:
        if tmdb_id is not None and doc.get("tmdb_id") == tmdb_id:
            return doc
        if imdb_id is not None and doc.get("imdb_id") == imdb_id:
            return doc
    return None


async def _get_media_ids_by_part(channel, msg_id):
    legacy = f"enc:{channel}:{msg_id}"
    for coll, media_type in (("movie", "movie"), ("tv", "tv")):
        for doc in _storage()[coll].docs:
            for q in _all_qualities(doc):
                if q.get("id") == legacy:
                    return doc.get("imdb_id"), doc.get("tmdb_id"), media_type
                if any(p.get("chat_id") == channel and p.get("msg_id") == msg_id
                       for p in q.get("parts") or []):
                    return doc.get("imdb_id"), doc.get("tmdb_id"), media_type
    return None


async def _rename_media_part(channel, msg_id, name):
    RENAME_CALLS.append((channel, msg_id, name))
    legacy = f"enc:{channel}:{msg_id}"
    for coll in ("movie", "tv"):
        for doc in _storage()[coll].docs:
            for q in _all_qualities(doc):
                first = (q.get("parts") or [{}])[0]
                if q.get("id") == legacy or (
                        first.get("chat_id") == channel
                        and first.get("msg_id") == msg_id):
                    if q.get("name") != name:
                        q["name"] = name
                        return True
                    return False
    return False


async def _remove_media_part(channel, msg_id):
    REMOVE_CALLS.append((channel, msg_id))
    legacy = f"enc:{channel}:{msg_id}"

    def _hit(q):
        return q.get("id") == legacy or any(
            p.get("chat_id") == channel and p.get("msg_id") == msg_id
            for p in q.get("parts") or [])

    for doc in list(_storage()["movie"].docs):
        kept = [q for q in doc.get("telegram") or [] if not _hit(q)]
        if len(kept) != len(doc.get("telegram") or []):
            if kept:
                doc["telegram"] = kept
            else:
                _storage()["movie"].docs.remove(doc)
            return True
    for doc in list(_storage()["tv"].docs):
        for season in doc.get("seasons") or []:
            for ep in season.get("episodes") or []:
                kept = [q for q in ep.get("telegram") or [] if not _hit(q)]
                if len(kept) != len(ep.get("telegram") or []):
                    ep["telegram"] = kept
                    season["episodes"] = [e for e in season["episodes"]
                                           if e.get("telegram")]
                    doc["seasons"] = [s for s in doc["seasons"]
                                      if s.get("episodes")]
                    if not doc["seasons"]:
                        _storage()["tv"].docs.remove(doc)
                    return True
    return False


async def _insert_media(metadata_info, channel=None, msg_id=None, size=None,
                        raw_size=0, name=None, status=None):
    INSERT_CALLS.append(copy.deepcopy(metadata_info))
    if INSERT_RAISES[0] is not None:
        raise INSERT_RAISES[0]
    settings = api.FakeSettingsManager._current._d
    dup_protection = bool(settings.get("duplicate_protection", False))
    replace_mode = bool(settings.get("replace_mode", False))
    entry = {"quality": metadata_info.get("quality"),
             "id": metadata_info.get("encoded_string"),
             "name": name, "size": size}
    if metadata_info.get("group_key"):
        entry["group_key"] = metadata_info["group_key"]
        entry["parts"] = [{"part_number": metadata_info.get("part_number") or 1,
                           "chat_id": channel, "msg_id": msg_id,
                           "size_bytes": raw_size}]
    if metadata_info["media_type"] == "movie":
        doc = _find_doc("movie", metadata_info.get("tmdb_id"),
                        metadata_info.get("imdb_id"))
        if doc is None:
            doc = {"tmdb_id": metadata_info.get("tmdb_id"),
                   "imdb_id": metadata_info.get("imdb_id"),
                   "title": metadata_info.get("title"), "telegram": []}
            _storage()["movie"].docs.append(doc)
        if (dup_protection and not replace_mode
                and any(_dup_key(q) == _dup_key(entry)
                        for q in doc["telegram"])):
            if status is not None:
                status["duplicate_skipped"] = True
            return api.ObjectId()
        doc["telegram"].append(entry)
        return api.ObjectId()
    doc = _find_doc("tv", metadata_info.get("tmdb_id"),
                    metadata_info.get("imdb_id"))
    if doc is None:
        doc = {"tmdb_id": metadata_info.get("tmdb_id"),
               "imdb_id": metadata_info.get("imdb_id"),
               "title": metadata_info.get("title"), "seasons": []}
        _storage()["tv"].docs.append(doc)
    season = next((s for s in doc["seasons"]
                   if s["season_number"] == metadata_info["season_number"]), None)
    if season is None:
        season = {"season_number": metadata_info["season_number"],
                  "episodes": []}
        doc["seasons"].append(season)
    episode = next((e for e in season["episodes"]
                    if e["episode_number"] == metadata_info["episode_number"]),
                   None)
    if episode is None:
        episode = {"episode_number": metadata_info["episode_number"],
                   "title": metadata_info.get("episode_title"),
                   "absolute_episode": metadata_info.get("absolute_episode"),
                   "telegram": []}
        season["episodes"].append(episode)
    if (dup_protection and not replace_mode
            and any(_dup_key(q) == _dup_key(entry)
                    for q in episode["telegram"])):
        if status is not None:
            status["duplicate_skipped"] = True
        return api.ObjectId()
    episode["telegram"].append(entry)
    return api.ObjectId()


fake_db.get_media_ids_by_part = _get_media_ids_by_part
fake_db.rename_media_part = _rename_media_part
fake_db.remove_media_part = _remove_media_part
fake_db.insert_media = _insert_media


# ------------------------------------------------------------------
# Fixtures / drivers
# ------------------------------------------------------------------

CHAT = -100111
STRIPPED = 111


def _reset():
    api._reset_api()
    for script in (META, RESOLVE, TVDB_ABS):
        script["result"] = None
        script["calls"] = []
    INSERT_CALLS.clear()
    REMOVE_CALLS.clear()
    RENAME_CALLS.clear()
    STAMP_CALLS.clear()
    ROUTE_SKIP_CALLS.clear()
    INSERT_RAISES[0] = None
    bridge.register(REAL_IMPL)


def _settings(**kw):
    base = {"auth_channels": ["-100111"],
            "channel_titles": {"-100111": "Donghua Dumping"},
            "librarian_channels": ["-100111"],
            "manual_channels": [], "skip_channel": ""}
    base.update(kw)
    return api._set_settings(**base)


def _media(file_name="Fixture Show S01E02 720p.mkv", file_size=2048):
    return types.SimpleNamespace(
        file_id="fid-1", file_name=file_name, file_size=file_size,
        mime_type="video/x-matroska", file_unique_id="uniq-1")


def _message(caption="Fixture Show S01E02 720p.mkv", msg_id=56,
             chat_id=CHAT, file_name="Fixture Show S01E02 720p.mkv",
             entities=None):
    media = _media(file_name)
    return types.SimpleNamespace(
        id=msg_id,
        chat=types.SimpleNamespace(id=chat_id, username=None),
        caption=caption, caption_entities=entities or [],
        video=media, document=None)


def _imdb_entity(imdb_id):
    return [types.SimpleNamespace(
        url=f"https://www.imdb.com/title/{imdb_id}/")]


def _tv_payload(tmdb=202, imdb="tt0000002", season=1, episode=2,
                title="Fixture Show", quality="720p", ep_title="Ep Title",
                channel=STRIPPED, msg=56):
    return {"tmdb_id": tmdb, "imdb_id": imdb, "title": title, "year": 2021,
            "rate": 8.0, "description": "d", "poster": "p", "backdrop": "b",
            "logo": "l", "genres": [], "cast": [], "runtime": "45",
            "media_type": "tv", "quality": quality,
            "encoded_string": f"enc:{channel}:{msg}",
            "season_number": season, "episode_number": episode,
            "episode_title": ep_title, "episode_backdrop": "eb",
            "episode_overview": "eo", "episode_released": "2021-01-01",
            "group_key": None, "part_number": None}


def _movie_payload(tmdb=555, imdb="tt0000555", title="Fixture Film",
                   channel=STRIPPED, msg=56):
    return {"tmdb_id": tmdb, "imdb_id": imdb, "title": title, "year": 2020,
            "rate": 7.0, "description": "d", "poster": "p", "backdrop": "b",
            "logo": "l", "genres": [], "cast": [], "runtime": "90",
            "media_type": "movie", "quality": "1080p",
            "encoded_string": f"enc:{channel}:{msg}",
            "group_key": None, "part_number": None}


def _seed_tv_entry(tmdb=202, imdb="tt0000002", season=1, episode=2,
                   channel=STRIPPED, msg=56, quality="720p",
                   name="Fixture Show S01E02 720p.mkv", size="700.00MB",
                   absolute=None):
    doc = {"tmdb_id": tmdb, "imdb_id": imdb, "title": "Fixture Show",
           "seasons": [{"season_number": season, "episodes": [
               {"episode_number": episode, "title": "Ep Title",
                "absolute_episode": absolute,
                "telegram": [{"quality": quality,
                              "id": f"enc:{channel}:{msg}",
                              "name": name, "size": size}]}]}]}
    _storage()["tv"].docs.append(doc)
    return doc


def _fresh_primitives():
    receiver.file_queue = asyncio.Queue()
    receiver.db_lock = asyncio.Lock()


async def _serve(coro):
    """Run a handler/seam coroutine with the real process_file
    consumer live (the seam awaits queue futures)."""
    _fresh_primitives()
    task = asyncio.create_task(receiver.process_file())
    try:
        return await coro
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _restore_registration():
    bridge.register(REAL_IMPL)


# ------------------------------------------------------------------
# Bridge (DISP-3 items 1–2; plan §3.1)
# ------------------------------------------------------------------

def test_bridge_unregistered_returns_pinned_error():
    bridge.register(None)
    try:
        async def go():
            result = await bridge.reindex_one_post(object(), object())
            assert result == {"state": "error", "ids": None, "filed": None,
                              "error": "re-index seam not registered"}

        _run(go())
    finally:
        _restore_registration()


def test_bridge_unregistered_results_are_fresh_dicts():
    bridge.register(None)
    try:
        async def go():
            first = await bridge.reindex_one_post(object(), object())
            second = await bridge.reindex_one_post(object(), object())
            assert first is not second
            first["state"] = "mutated"
            first["error"] = "mutated"
            assert second == {"state": "error", "ids": None, "filed": None,
                              "error": "re-index seam not registered"}

        _run(go())
    finally:
        _restore_registration()


def test_bridge_delegates_with_kwargs_intact():
    seen = {}

    async def impl(client, message, *, placement=None, expected_ids=None,
                   series_ids=None, future_timeout=120):
        seen.update(client=client, message=message, placement=placement,
                    expected_ids=expected_ids, series_ids=series_ids,
                    future_timeout=future_timeout)
        return {"state": "indexed", "ids": ("tt1", 42, "tv"),
                "filed": {"season": 0, "episode": 7}, "error": None}

    bridge.register(impl)
    try:
        async def go():
            client, message = object(), object()
            placement = {"season": 0, "episode": 7}
            expected = {"imdb": "tt1", "tmdb": 42}
            series = {"tmdb": 42, "tvdb": 99}
            result = await bridge.reindex_one_post(
                client, message, placement=placement,
                expected_ids=expected, series_ids=series)
            assert result == {"state": "indexed", "ids": ("tt1", 42, "tv"),
                              "filed": {"season": 0, "episode": 7},
                              "error": None}
            assert seen["client"] is client and seen["message"] is message
            assert seen["placement"] is placement
            assert seen["expected_ids"] is expected
            assert seen["series_ids"] is series
            assert seen["future_timeout"] == 120  # DISP-3 default
            await bridge.reindex_one_post(client, message, future_timeout=30)
            assert seen["future_timeout"] == 30

        _run(go())
    finally:
        _restore_registration()


def test_bridge_registration_is_the_receiver_impl():
    # receiver.py registers at import (DESIGN §4); the bridge must
    # be holding the real seam function after this module loads.
    _reset()
    assert bridge._registered is REAL_IMPL


# ------------------------------------------------------------------
# Handler preservation + seam results (plan §10.3; B-ACC-1)
# ------------------------------------------------------------------

def test_handler_rename_only_override_matches():
    _reset()
    _settings()
    _seed_tv_entry()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert RENAME_CALLS and RENAME_CALLS[0][0] == STRIPPED
        assert REMOVE_CALLS == [] and INSERT_CALLS == []
        assert receiver.file_queue.empty()
        result = await receiver.reindex_one_post(object(), msg)
        assert result["state"] == "duplicate"
        assert result["ids"] == ("tt0000002", 202, "tv")
        assert result["filed"] == {"season": 1, "episode": 2}
        assert result["error"] is None

    _run(go())


def test_handler_rename_only_no_override_stamps_bare_ref():
    _reset()
    _settings()
    _seed_tv_entry()
    # No override at all -> rename-only; a bare tt… ref in the
    # caption text is a raw ref, so the caption is stamped.
    msg = _message(caption="Fixture Show S01E02 720p tt0000002")

    async def go():
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert RENAME_CALLS and REMOVE_CALLS == [] and INSERT_CALLS == []
        assert STAMP_CALLS and STAMP_CALLS[0][1]["imdb_id"] == "tt0000002"
        result = await receiver.reindex_one_post(object(), msg)
        assert result["state"] == "duplicate"
        assert result["filed"] == {"season": 1, "episode": 2}

    _run(go())


def test_handler_no_override_no_entry_is_metadata_failed():
    _reset()
    _settings()
    msg = _message(caption="Fixture Show S01E02 720p.mkv")

    async def go():
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert RENAME_CALLS == [] and REMOVE_CALLS == []
        assert INSERT_CALLS == [] and receiver.file_queue.empty()
        result = await receiver.reindex_one_post(object(), msg)
        assert result == {"state": "metadata_failed", "ids": None,
                          "filed": None, "error": None}

    _run(go())


def test_handler_metadata_none_removes_nothing():
    _reset()
    _settings()
    META["result"] = None
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert META["calls"], "metadata() must be attempted"
        assert REMOVE_CALLS == [] and INSERT_CALLS == []
        assert receiver.file_queue.empty()
        result = await receiver.reindex_one_post(object(), msg)
        assert result["state"] == "metadata_failed"

    _run(go())


def test_seam_reindex_override_mismatch_files_indexed():
    _reset()
    _settings()
    _seed_tv_entry(tmdb=999, imdb="tt9999999")
    META["result"] = _tv_payload()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        result = await _serve(receiver.reindex_one_post(object(), msg))
        assert result["state"] == "indexed"
        assert result["ids"] == ("tt0000002", 202, "tv")
        assert result["filed"] == {"season": 1, "episode": 2}
        assert REMOVE_CALLS == [(STRIPPED, 56)]
        assert len(INSERT_CALLS) == 1

    _run(go())


def test_handler_reindex_queues_eight_tuple():
    _reset()
    _settings()
    _seed_tv_entry(tmdb=999, imdb="tt9999999")
    META["result"] = _tv_payload()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))
    seen_items = []

    async def go():
        _fresh_primitives()
        queue = receiver.file_queue
        real_put = queue.put

        async def spy_put(item):
            seen_items.append(item)
            return await real_put(item)

        queue.put = spy_put
        task = asyncio.create_task(receiver.process_file())
        try:
            await receiver.file_edited_handler(object(), msg)
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(seen_items) == 1
        item = seen_items[0]
        # The seam's own put carries its completion future.
        assert len(item) == 8 and item[7] is not None
        assert await fake_db.get_media_ids_by_part(STRIPPED, 56) == (
            "tt0000002", 202, "tv")

    _run(go())


def test_receive_handler_queues_eight_tuple_with_none_future():
    _reset()
    _settings()
    META["result"] = _tv_payload()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))
    seen_items = []

    async def go():
        _fresh_primitives()
        queue = receiver.file_queue
        real_put = queue.put

        async def spy_put(item):
            seen_items.append(item)
            return await real_put(item)

        queue.put = spy_put
        task = asyncio.create_task(receiver.process_file())
        try:
            await receiver.file_receive_handler(object(), msg)
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(seen_items) == 1
        item = seen_items[0]
        # Both pre-existing put sites pass None explicitly (plan
        # §3.2 item 5): the event path stays fire-and-forget.
        assert len(item) == 8 and item[7] is None
        assert len(INSERT_CALLS) == 1

    _run(go())


def test_seam_results_are_fresh_dicts():
    _reset()
    _settings()
    _seed_tv_entry()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        _fresh_primitives()
        first = await receiver.reindex_one_post(object(), msg)
        second = await receiver.reindex_one_post(object(), msg)
        assert first is not second
        first["state"] = "mutated"
        assert second["state"] == "duplicate"

    _run(go())


def test_seam_duplicate_skipped_maps_to_duplicate():
    _reset()
    _settings(duplicate_protection=True)
    # This post's old entry (another series) is removed first, per
    # the handler's order; a SECOND post of the same episode already
    # holds the same quality/name/size, so insert_media reports
    # duplicate_skipped and the seam maps it to `duplicate`. This
    # post filed nothing, so the read-back honestly finds no ids /
    # placement for it (the engine's mapping decides from there).
    _seed_tv_entry(tmdb=999, imdb="tt9999999")
    _seed_tv_entry(tmdb=202, imdb="tt0000002", msg=57, size="2.00KB")
    META["result"] = _tv_payload(tmdb=202, imdb="tt0000002")
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt7777777"))

    async def go():
        result = await _serve(receiver.reindex_one_post(object(), msg))
        assert result["state"] == "duplicate"
        assert result["ids"] is None and result["filed"] is None

    _run(go())


# ------------------------------------------------------------------
# process_file completion future (plan §10.3; B-ACC-5)
# ------------------------------------------------------------------

def test_process_file_future_set_on_duplicate_skipped():
    _reset()
    _settings(duplicate_protection=True)
    _seed_tv_entry()

    async def go():
        _fresh_primitives()
        future = asyncio.get_running_loop().create_future()
        payload = _tv_payload()
        await receiver.file_queue.put(
            (payload, STRIPPED, 56, "700.00MB", 2048,
             "Fixture Show S01E02 720p.mkv", None, future))
        task = asyncio.create_task(receiver.process_file())
        try:
            outcome = await asyncio.wait_for(future, timeout=5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert outcome["status"].get("duplicate_skipped") is True
        assert outcome["updated_id"]

    _run(go())


def test_process_file_future_carries_insert_exception():
    _reset()
    _settings()
    INSERT_RAISES[0] = RuntimeError("insert exploded")

    async def go():
        _fresh_primitives()
        future = asyncio.get_running_loop().create_future()
        await receiver.file_queue.put(
            (_tv_payload(), STRIPPED, 56, "700.00MB", 2048,
             "Fixture Show S01E02 720p.mkv", None, future))
        task = asyncio.create_task(receiver.process_file())
        try:
            try:
                await asyncio.wait_for(future, timeout=5)
                raise AssertionError("future must carry the exception")
            except RuntimeError as exc:
                assert "insert exploded" in str(exc)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _run(go())


def test_seam_insert_exception_becomes_error_result():
    _reset()
    _settings()
    META["result"] = _tv_payload()
    INSERT_RAISES[0] = RuntimeError("insert exploded")
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        result = await _serve(receiver.reindex_one_post(object(), msg))
        assert result["state"] == "error"
        assert result["ids"] is None and result["filed"] is None
        assert "insert exploded" in result["error"]

    _run(go())


# ------------------------------------------------------------------
# Coordination (plan §3.3/§10.3; B-ACC-4; DISP-3 item 4)
# ------------------------------------------------------------------

def _fix_row(state="prepared", chat_id=CHAT, msg_id=56):
    from datetime import datetime, timezone
    return {"request_id": f"coord-{state}-{chat_id}-{msg_id}",
            "batch_id": None, "ledger_id": api.ObjectId(),
            "source_locator": {"chat_id": chat_id, "msg_id": msg_id},
            "current_locator": {"chat_id": chat_id, "msg_id": msg_id},
            "stem": "fixture show", "chat_id": chat_id,
            "old_caption": "old", "old_caption_text": "old",
            "new_caption": "new", "new_caption_text": "new",
            "expected_ids": {"tmdb": 202}, "series_record_id": None,
            "action": "edit", "state": state, "index_confirmed": False,
            "error": None, "budget_counted": False, "dry_run": False,
            "prepared_at": datetime.now(timezone.utc)}


def test_coordination_blocks_both_handlers_full_form():
    _reset()
    _settings()
    _seed_tv_entry()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        await store.ensure_indexes()
        await store.insert_fix(_fix_row("prepared"))
        # The trap, documented as a test (DISP-3 item 4): the
        # stripped form can never match a full-form fix row.
        assert await store.find_active_fix(STRIPPED, 56) is None
        assert await store.find_active_fix(CHAT, 56) is not None
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert RENAME_CALLS == [] and REMOVE_CALLS == []
        assert INSERT_CALLS == [] and META["calls"] == []
        await receiver.file_receive_handler(object(), msg)
        assert META["calls"] == [] and ROUTE_SKIP_CALLS == []
        assert receiver.file_queue.empty()

    _run(go())


def test_coordination_terminal_row_lets_handlers_proceed():
    _reset()
    _settings()
    _seed_tv_entry()
    msg = _message(caption="Fixture Show S01E02 720p.mkv\nIMDb",
                   entities=_imdb_entity("tt0000002"))

    async def go():
        await store.ensure_indexes()
        await store.insert_fix(_fix_row("fixed"))
        assert await store.find_active_fix(CHAT, 56) is None
        _fresh_primitives()
        await receiver.file_edited_handler(object(), msg)
        assert RENAME_CALLS, "rename-only path must proceed"

    _run(go())


# ------------------------------------------------------------------
# Placement (DISP-3 item 5, DISP-3a/3b; B-ACC-2/3)
# ------------------------------------------------------------------

DONGHUA_TMDB = 79481
DONGHUA_IMDB = "tt7948100"


async def _seed_donghua_record(media_type="donghua"):
    await store.ensure_indexes()
    return await store.upsert_record({
        "canonical_key": f"tmdb:{DONGHUA_TMDB}",
        "ids": {"tmdb": DONGHUA_TMDB, "tvdb": 333354},
        "titles": ["Donghua Fixture"], "media_type": media_type,
        "numbering": {"source": "tmdb", "order": "specials",
                      "offset": 5, "note": "fixture"},
        "status": "verified", "related_entries": []})


def _donghua_message(msg_id=56):
    return _message(
        caption="Donghua Fixture SP 1080p.mkv\nIMDb",
        msg_id=msg_id, file_name="Donghua Fixture SP 1080p.mkv",
        entities=_imdb_entity(DONGHUA_IMDB))


def test_placement_spike_case_movie_namespace_mismatch():
    _reset()
    _settings()
    # Canonical spike: metadata() resolves the caption's ID in the
    # movie namespace (unrelated movie payload, mismatched IDs);
    # the seam must obtain the payload by TV resolution at S00E07.
    META["result"] = _movie_payload(tmdb=555, imdb="tt0000555")
    RESOLVE["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Placed Title")
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record()
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 7},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "indexed"
        assert result["ids"] == (DONGHUA_IMDB, DONGHUA_TMDB, "tv")
        assert result["filed"] == {"season": 0, "episode": 7}
        assert len(RESOLVE["calls"]) == 1
        call = RESOLVE["calls"][0]
        assert call["season"] == 0 and call["episode"] == 7
        assert call["default_id"] == DONGHUA_IMDB
        doc = _find_doc("tv", DONGHUA_TMDB, DONGHUA_IMDB)
        ep = doc["seasons"][0]["episodes"][0]
        assert doc["seasons"][0]["season_number"] == 0
        assert ep["episode_number"] == 7 and ep["title"] == "Placed Title"

    _run(go())


def test_placement_same_series_wrong_episode_reresolves():
    _reset()
    _settings()
    # metadata() found the right series at the WRONG episode: the
    # seam re-resolves at the placed S/E rather than relabelling —
    # the filed episode_title is the placed episode's, not the
    # parsed one's.
    META["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=3,
        title="Donghua Fixture", quality="1080p", ep_title="Wrong Title")
    RESOLVE["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Placed Title")
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record()
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 7},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "indexed"
        assert result["filed"] == {"season": 0, "episode": 7}
        assert len(RESOLVE["calls"]) == 1
        assert INSERT_CALLS[0]["episode_title"] == "Placed Title"

    _run(go())


def test_placement_same_series_same_episode_keeps_metadata_payload():
    _reset()
    _settings()
    META["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Already Placed")
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record()
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 7},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "indexed"
        assert RESOLVE["calls"] == []
        assert INSERT_CALLS[0]["episode_title"] == "Already Placed"

    _run(go())


def test_placement_tv_resolution_none_is_metadata_failed():
    _reset()
    _settings()
    META["result"] = None
    RESOLVE["result"] = None
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record()
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 7},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "metadata_failed"
        assert INSERT_CALLS == [] and REMOVE_CALLS == []

    _run(go())


def test_placement_non_donghua_falls_through_to_standard_path():
    _reset()
    _settings(channel_titles={"-100111": "Anime Dumping"})
    # The series record establishes anime (DISP-3a: the record
    # governs) — the S/E override must NOT fire even with placement
    # data present; the file files through metadata()'s standard
    # path (standard S00) and resolve_series is never called.
    META["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Standard S00")
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record(media_type="anime")
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 9},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert RESOLVE["calls"] == []
        assert result["state"] == "indexed"
        assert result["filed"] == {"season": 0, "episode": 7}

    _run(go())


def test_placement_conflicting_signals_fail_safe_no_override():
    _reset()
    _settings(channel_titles={"-100111": "Anime Dumping"})
    # DISP-3a conflict branch: the series record establishes
    # donghua, but the channel name signals NOT donghua. The
    # signals conflict, so the series is NOT donghua for placement
    # purposes (fail-safe) — the override must NOT fire even though
    # the record alone would pass the gate. The file files through
    # metadata()'s standard path and resolve_series is never
    # called for placement.
    META["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Standard S00")
    msg = _donghua_message()

    async def go():
        await _seed_donghua_record(media_type="donghua")
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 9},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert RESOLVE["calls"] == []
        assert META["calls"], "standard filing must proceed"
        assert result["state"] == "indexed"
        assert result["filed"] == {"season": 0, "episode": 7}

    _run(go())


def test_placement_channel_only_signal_fires():
    _reset()
    _settings()
    # DISP-3a channel-only branch: NO series record exists for any
    # of the candidate provider ids, so the record lookup finds
    # nothing and the channel name alone is the signal — the
    # default fixture channel is "Donghua Dumping", which signals
    # donghua, so the gate passes and the placement override fires
    # (the spike shape: metadata() resolves in the movie namespace,
    # the seam re-resolves by TV resolution at the placed S/E).
    META["result"] = _movie_payload(tmdb=555, imdb="tt0000555")
    RESOLVE["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=0, episode=7,
        title="Donghua Fixture", quality="1080p", ep_title="Placed Title")
    msg = _donghua_message()

    async def go():
        # No _seed_donghua_record(): the store holds no record.
        assert await store.get_record_by_id_field(
            "tmdb", DONGHUA_TMDB) is None
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"season": 0, "episode": 7},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "indexed"
        assert result["ids"] == (DONGHUA_IMDB, DONGHUA_TMDB, "tv")
        assert result["filed"] == {"season": 0, "episode": 7}
        assert len(RESOLVE["calls"]) == 1
        call = RESOLVE["calls"][0]
        assert call["season"] == 0 and call["episode"] == 7

    _run(go())


def test_absolute_placement_uses_tvdb_lookup_not_arithmetic():
    _reset()
    _settings()
    # Gap fixture: seasons of unequal length with a numbering gap —
    # absolute 30 is S03E02 by TVDB lookup; summing season lengths
    # (12 + 10) would wrongly land on S03E08.
    TVDB_ABS["result"] = {"seasonNumber": 3, "number": 2,
                          "absoluteNumber": 30}
    RESOLVE["result"] = _tv_payload(
        tmdb=DONGHUA_TMDB, imdb=DONGHUA_IMDB, season=3, episode=2,
        title="Donghua Fixture", quality="1080p", ep_title="Abs Title")
    META["result"] = None
    msg = _donghua_message()

    async def go():
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"absolute": 30},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert TVDB_ABS["calls"] == [(333354, 30)]
        assert len(RESOLVE["calls"]) == 1
        call = RESOLVE["calls"][0]
        assert call["season"] == 3 and call["episode"] == 2
        assert result["state"] == "indexed"
        assert result["filed"] == {"absolute": 30}
        assert INSERT_CALLS[0]["absolute_episode"] == 30
        assert INSERT_CALLS[0]["season_number"] == 3
        assert INSERT_CALLS[0]["episode_number"] == 2

    _run(go())


def test_absolute_placement_without_tvdb_id_is_metadata_failed():
    _reset()
    _settings()
    msg = _donghua_message()

    async def go():
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"absolute": 30},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB}))
        assert result["state"] == "metadata_failed"
        assert TVDB_ABS["calls"] == [] and INSERT_CALLS == []

    _run(go())


def test_absolute_placement_lookup_miss_is_metadata_failed():
    _reset()
    _settings()
    TVDB_ABS["result"] = None
    msg = _donghua_message()

    async def go():
        result = await _serve(receiver.reindex_one_post(
            object(), msg, placement={"absolute": 30},
            expected_ids={"tmdb": DONGHUA_TMDB, "imdb": DONGHUA_IMDB},
            series_ids={"tmdb": DONGHUA_TMDB, "tvdb": 333354}))
        assert result["state"] == "metadata_failed"
        assert INSERT_CALLS == [] and REMOVE_CALLS == []

    _run(go())
