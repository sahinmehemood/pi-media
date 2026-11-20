from asyncio import Lock, Queue, create_task, get_running_loop
from asyncio import sleep as asleep

from pyrogram import Client, filters
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import Message

import Backend
from Backend import db
from Backend.helper import librarian_reindex
from Backend.helper.announcer import announce_new_media
from Backend.helper.auto_catalog import start_single_media_catalog_sync
from Backend.helper.encrypt import encode_string
from Backend.helper.manual_add import resolve_telegram_message, stamp_caption_with_id
from Backend.helper.requests_manager import auto_fulfill
from Backend.helper.metadata import caption_with_links, extract_default_id, has_raw_ref, metadata
from Backend.helper.pyro import apply_video_thumb_to_metadata, clean_filename, finalize_media_name, get_readable_file_size, resolve_video_thumb_url
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.skip_channel import is_skip_channel, route_to_skip_channel
from Backend.helper.split_files import parse_split_info
from Backend.helper.subtitles import ingest_subtitle, is_subtitle_file, remove_subtitle
from Backend.helper.task_manager import delete_message
from Backend.logger import LOGGER

file_queue = Queue()
db_lock = Lock()
manual_session_lock = Lock()


#----- True when the message carries a streamable video or a split-archive part
def _is_supported_media(message: Message) -> bool:
    if message.video:
        return True
    if message.document:
        mime_type = message.document.mime_type or ""
        if mime_type.startswith("video/"):
            return True
        candidate = message.caption or message.document.file_name or ""
        if parse_split_info(candidate):
            return True
        # Single (non-split) STORED zip archives containing a video
        name = (message.document.file_name or candidate or "").lower()
        if name.endswith(".zip") and not parse_split_info(name):
            return True
    return False


#----- True when a chat id belongs to a manual channel (files added by hand, not auto-indexed)
def _is_manual_channel(chat_id) -> bool:
    target = str(chat_id).replace("-100", "")
    return any(str(c).strip().replace("-100", "") == target for c in SettingsManager.current().manual_channels)


#----- Common message field extraction shared by the channel handlers
def _extract_fields(message: Message):
    file = message.video or message.document
    title = message.caption or file.file_name
    channel = str(message.chat.id).replace("-100", "")
    return file, title, message.id, file.file_size, get_readable_file_size(file.file_size), channel


#----- Strip URLs/part suffix from a title and ensure a video extension
def _finalize_title(title: str, metadata_info: dict) -> str:
    return finalize_media_name(title, bool(metadata_info.get('group_key')))


#----- Serialize DB inserts from the queue and trigger catalog sync
#----- Queue items are 8-tuples: the first seven elements are the
#----- pre-Phase-5 shape; the eighth is the optional completion
#----- future of the Librarian re-index seam (§8) — None for the
#----- event handlers (fire-and-forget, exactly as before), a Future
#----- for the seam, which awaits it. The future is settled on EVERY
#----- exit path — normal completion, the duplicate-skipped
#----- early-continue, and exception — because an unset future would
#----- hang the seam's caller to its 120 s timeout on exactly the
#----- duplicate case §8 maps (phase5-plan DISP-3 item 3).
async def process_file():
    while True:
        item = await file_queue.get()
        if len(item) == 8:
            metadata_info, channel, msg_id, size, raw_size, title, stamp, future = item
        else:  # a pre-Phase-5 7-tuple still in flight at upgrade time
            metadata_info, channel, msg_id, size, raw_size, title, stamp = item
            future = None
        try:
            insert_status: dict = {}
            updated_id = None
            async with db_lock:
                updated_id = await db.insert_media(metadata_info, channel=channel, msg_id=msg_id, size=size, raw_size=raw_size, name=title, status=insert_status)
                if updated_id:
                    LOGGER.info(f"{metadata_info['media_type']} updated with ID: {updated_id}")
                else:
                    LOGGER.info("Update failed due to validation errors.")

            if updated_id and insert_status.get("duplicate_skipped"):
                LOGGER.info(f"Duplicate protection: deleting duplicate message {msg_id} from channel {channel}.")
                create_task(delete_message(int(f"-100{channel}"), msg_id))
                if future is not None and not future.done():
                    future.set_result({"updated_id": updated_id,
                                       "status": dict(insert_status)})
                continue

            if updated_id:
                if stamp:
                    create_task(stamp_caption_with_id(stamp, metadata_info))
                start_single_media_catalog_sync(
                    db,
                    tmdb_id=metadata_info.get("tmdb_id"),
                    media_type=metadata_info.get("media_type"),
                )
                announce_new_media(metadata_info)
                create_task(auto_fulfill(
                    tmdb_id=metadata_info.get("tmdb_id"),
                    imdb_id=metadata_info.get("imdb_id"),
                    media_type=metadata_info.get("media_type"),
                ))
            if future is not None and not future.done():
                future.set_result({"updated_id": updated_id,
                                   "status": dict(insert_status)})
        except Exception as e:
            LOGGER.error(f"Error processing queued file {msg_id}: {e}")
            if future is not None and not future.done():
                future.set_exception(e)
        finally:
            file_queue.task_done()


create_task(process_file())


#----- Build a title-level metadata base from an existing media document
def _base_from_doc(doc: dict) -> dict:
    return {
        "tmdb_id": doc.get("tmdb_id"),
        "imdb_id": doc.get("imdb_id"),
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or 0,
        "rate": doc.get("rating") or 0,
        "description": doc.get("description") or "",
        "poster": doc.get("poster") or "",
        "backdrop": doc.get("backdrop") or "",
        "logo": doc.get("logo") or "",
        "genres": doc.get("genres") or [],
        "cast": doc.get("cast") or [],
        "runtime": str(doc.get("runtime") or ""),
        "original_language": doc.get("original_language"),
        "origin_country": doc.get("origin_country") or [],
    }


#----- Highest existing episode number in a season, or 0 if none
def _max_episode(doc: dict, season_number: int) -> int:
    for season in doc.get("seasons", []) or []:
        if season.get("season_number") == season_number:
            eps = [e.get("episode_number", 0) for e in season.get("episodes", []) or []]
            return max(eps) if eps else 0
    return 0


async def _handle_personal_session(client: Client, message: Message) -> None:
    session = Backend.MANUAL_SESSION
    if not session:
        return
    async with manual_session_lock:
        channel = str(message.chat.id).replace("-100", "")
        try:
            resolved = await resolve_telegram_message(client, chat_id=channel, msg_id=message.id)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] Could not resolve message {message.id}: {e}")
            return

        tmdb_id = session["tmdb_id"]
        media_type = session["media_type"]
        location = await db.find_media_doc(media_type, tmdb_id)
        if not location:
            LOGGER.warning(f"[Manual Session] Target id {tmdb_id} not found; ignoring file.")
            return
        doc = location[0]

        p_channel = int(resolved["chat_id"])
        p_msg = int(resolved["msg_id"])
        encoded = await encode_string({"chat_id": p_channel, "msg_id": p_msg})
        name = resolved["name"]
        quality = resolved.get("quality") or session.get("quality") or "HD"

        split_key = resolved.get("split_key")
        metadata_info = _base_from_doc(doc)
        metadata_info.update({
            "media_type": media_type,
            "quality": quality,
            "encoded_string": encoded,
            "group_key": f"{channel}:{quality}:{split_key}" if split_key else None,
            "part_number": resolved.get("part_number"),
            "is_anime": bool(doc.get("is_anime")),
        })

        if media_type == "tv":
            season_number = session["season"]
            episode_number = session["episode"]
            if episode_number is None:
                episode_number = _max_episode(doc, season_number) + 1
            thumb_url = ""
            if resolved.get("has_thumb"):
                thumb_url = await resolve_video_thumb_url(client, message, encoded)
            metadata_info.update({
                "season_number": season_number,
                "episode_number": episode_number,
                "episode_title": f"S{season_number:02d}E{episode_number:02d}",
                "episode_backdrop": thumb_url or metadata_info.get("backdrop") or "",
                "episode_overview": "",
                "episode_released": "",
            })
        else:
            await apply_video_thumb_to_metadata(metadata_info, message, encoded, client)

        async with db_lock:
            updated_id = await db.insert_media(
                metadata_info, channel=p_channel, msg_id=p_msg,
                size=resolved["size"], name=name, raw_size=int(resolved.get("raw_size") or 0),
            )

        if updated_id:
            where = (f"S{metadata_info['season_number']:02d}E{metadata_info['episode_number']:02d} "
                     if media_type == "tv" else "")
            LOGGER.info(f"[Manual Session] Added {quality} {where}to '{metadata_info.get('title')}' (id {tmdb_id}).")
            create_task(stamp_caption_with_id(message, metadata_info))
        else:
            LOGGER.warning(f"[Manual Session] Insert failed for message {message.id}.")


#----- Ingest new channel media into the queue after building metadata
@Client.on_message(filters.channel & (filters.document | filters.video))
async def file_receive_handler(client: Client, message: Message):
    if is_skip_channel(message):
        return

    #----- Librarian coordination (§8; plan §3.3): a post under a
    #----- non-terminal fix operation is the engine's alone.
    if await _has_active_fix(message):
        return

    session = Backend.MANUAL_SESSION
    is_manual = _is_manual_channel(message.chat.id)

    #----- Manual channel + personal session: add straight onto the personal title
    if is_manual and session and session.get("kind") == "personal":
        await _handle_personal_session(client, message)
        return

    #----- Manual channel otherwise only proceeds during a real (TMDB/IMDb) session;
    #----- real files are parsed from their name/caption and forced onto the session id.
    if is_manual:
        if not (session and session.get("kind") == "real"):
            return
    elif str(message.chat.id) not in SettingsManager.current().auth_channels:
        return

    is_real_session = bool(session and session.get("kind") == "real")
    override_id = session["default_id"] if is_real_session else None
    season_hint = session.get("season") if is_real_session else None
    try:
        sub_name = (message.document.file_name if message.document else "") or ""
        if sub_name and is_subtitle_file(sub_name):
            channel = str(message.chat.id).replace("-100", "")
            create_task(ingest_subtitle(sub_name, int(channel), message.id))
            return

        if not _is_supported_media(message):
            await message.reply_text("> Not supported")
            return

        _, title, msg_id, raw_size, size, channel = _extract_fields(message)

        metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id or extract_default_id(caption_with_links(message)), season_hint=season_hint)
        if metadata_info is None:
            LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
            await route_to_skip_channel(client, message)
            return

        title = _finalize_title(title, metadata_info)
        encoded = metadata_info.get("encoded_string") or await encode_string({"chat_id": int(channel), "msg_id": msg_id})
        await apply_video_thumb_to_metadata(metadata_info, message, encoded, client)

        stamp = message if is_real_session or has_raw_ref(message.caption) else None
        await file_queue.put((metadata_info, int(channel), msg_id, size, raw_size, title, stamp, None))
    except FloodWait as e:
        LOGGER.info(f"Sleeping for {str(e.value)}s")
        await asleep(e.value)
        await message.reply_text(
            text=f"Got Floodwait of {str(e.value)}s",
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN
        )


def _override_matches_indexed(override_id: str, imdb_id, tmdb_id) -> bool:
    oid = str(override_id).strip().lower()
    if oid.startswith("tt"):
        return bool(imdb_id) and oid == str(imdb_id).strip().lower()
    if oid.isdigit():
        return tmdb_id not in (None, "") and oid == str(tmdb_id).strip()
    return False


#----- Librarian coordination check (§8; plan §3.3) ----------------
async def _has_active_fix(message: Message) -> bool:
    """True when a non-terminal librarian_fixes operation exists on
    this post — the event path then leaves the post to the engine.
    The locator form is load-bearing (DISP-3 item 4): find_active_fix
    compares current_locator.chat_id by int() with NO -100 stripping
    and fix rows store the full -100… form, so message.chat.id (the
    full form) is passed — never the stripped `channel` string
    _extract_fields produces, which could never match. A store
    failure logs and falls through to the pre-seam behaviour: the
    check is the event path's courtesy, its outage must not halt
    library ingest (the engine's own per-post lock still guards the
    engine side)."""
    try:
        from Backend.helper import librarian_store

        return bool(await librarian_store.find_active_fix(message.chat.id, message.id))
    except Exception as e:
        LOGGER.warning(f"[Librarian] coordination check failed for message {message.id}: {e}")
        return False


#----- Librarian re-index seam (DESIGN §4 seam 2 / §8; plan §3.2) --
async def _filed_for_part(channel: int, msg_id: int):
    """The filed placement for one part, read back from the storage
    DBs: {"season", "episode"} / {"absolute"} / None (a movie entry,
    or no entry). A small direct db.dbs query inside this module
    mirroring librarian_routes._find_indexed's stripped/full part
    matching (Senior N-8) — receiver.py must NOT import the plugin
    router; the bridge is the only crossing. Stored TV docs carry
    season_number / episode_number (modal.py Season/Episode) and
    absolute_episode on the episode; the plain season/episode
    spellings are accepted as well."""
    stripped = int(str(channel).replace("-100", ""))
    full = int(f"-100{stripped}")
    # Single-file qualities carry no parts list; their identity is
    # the stream id — the same legacy-hash match
    # db.get_media_ids_by_part itself performs (database.py).
    legacy = await encode_string({"chat_id": stripped, "msg_id": msg_id})

    def _part_hit(entry):
        if entry.get("id") == legacy:
            return True
        return any(p.get("chat_id") in (stripped, full) and p.get("msg_id") == msg_id
                   for p in (entry.get("parts") or []))

    def _match(chat_id):
        return {"$elemMatch": {"chat_id": chat_id, "msg_id": msg_id}}

    storage_keys = sorted(
        (k for k in db.dbs if k.startswith("storage_")),
        key=lambda k: int(k.split("_", 1)[1]),
    )
    for key in storage_keys:
        storage = db.dbs[key]
        doc = await storage["movie"].find_one(
            {"$or": [{"telegram.parts": _match(stripped)},
                     {"telegram.parts": _match(full)},
                     {"telegram.id": legacy}]})
        if doc and any(_part_hit(q) for q in (doc.get("telegram") or [])):
            return None
        doc = await storage["tv"].find_one(
            {"$or": [{"seasons.episodes.telegram.parts": _match(stripped)},
                     {"seasons.episodes.telegram.parts": _match(full)},
                     {"seasons.episodes.telegram.id": legacy}]})
        if doc:
            for season in doc.get("seasons") or []:
                for ep in season.get("episodes") or []:
                    if any(_part_hit(q) for q in (ep.get("telegram") or [])):
                        absolute = ep.get("absolute_episode")
                        if absolute is not None:
                            return {"absolute": int(absolute)}
                        s = season.get("season")
                        if s is None:
                            s = season.get("season_number")
                        e = ep.get("episode")
                        if e is None:
                            e = ep.get("episode_number")
                        if s is None or e is None:
                            return None
                        return {"season": int(s), "episode": int(e)}
    return None


def _ids_match_expected(imdb_id, tmdb_id, expected_ids) -> bool:
    """Equality on every ID present in expected_ids (§8 step 8's
    rule); absent IDs are ignored. No expected_ids -> no match: a
    placement may only keep metadata()'s payload when the series is
    verified, never on an unverifiable payload."""
    if not expected_ids:
        return False
    if expected_ids.get("imdb") is not None:
        if str(imdb_id or "") != str(expected_ids["imdb"]):
            return False
    if expected_ids.get("tmdb") is not None:
        try:
            if tmdb_id is None or int(tmdb_id) != int(expected_ids["tmdb"]):
                return False
        except (TypeError, ValueError):
            return False
    return True


async def _is_donghua_series(message: Message, series_ids, expected_ids) -> bool:
    """DISP-3a's one determination rule for the specials-placement
    gate: once a series record exists, the record's established
    media_type governs; before a record exists, channel
    name-inference (the channel title in settings) is the signal.
    On conflict between the signals, or when neither establishes
    donghua, the series is NOT donghua (fail-safe: standard S00
    loses nothing; a wrong override misplaces files)."""
    record_donghua = None
    try:
        from Backend.helper import librarian_store

        candidates = []
        for source in (series_ids, expected_ids):
            if isinstance(source, dict):
                for provider in ("tmdb", "tvdb", "anilist", "mal", "kitsu", "imdb"):
                    if source.get(provider) is not None:
                        candidates.append((provider, source[provider]))
        for provider, value in candidates:
            try:
                record = await librarian_store.get_record_by_id_field(provider, value)
            except Exception:
                record = None
            if record is not None:
                record_donghua = record.get("media_type") == "donghua"
                break
    except Exception as e:
        LOGGER.warning(f"[Librarian] donghua record lookup failed for message {message.id}: {e}")

    channel_donghua = None
    try:
        name = SettingsManager.current().channel_titles.get(str(message.chat.id))
        if name is not None:
            channel_donghua = "donghua" in name.lower()
    except Exception:
        channel_donghua = None

    if record_donghua is not None:
        if channel_donghua is not None and channel_donghua != record_donghua:
            return False  # conflicting signals -> fail-safe
        return record_donghua
    return channel_donghua is True


async def _apply_rename_only(title, channel, msg_id, message, existing):
    """file_edited_handler's rename-only path, verbatim: the stored
    file name follows the caption, and a caption already carrying a
    raw ID ref is stamped with the entry's IDs."""
    if title:
        async with db_lock:
            await db.rename_media_part(int(channel), msg_id, finalize_media_name(title, bool(parse_split_info(clean_filename(title)))))
    if has_raw_ref(message.caption):
        await stamp_caption_with_id(message, {"imdb_id": existing[0], "tmdb_id": existing[1], "media_type": existing[2]})


async def _queue_and_confirm(client, metadata_info, channel, msg_id, size, raw_size, title, stamp, placed_filed=None):
    """The handler's remove + queue tail, with the seam's completion
    future: remove the old part, queue the payload, await the
    future process_file settles, then read the filed triple +
    placement back. insert_media reporting failure (updated_id
    falsy) or duplicate_skipped -> `duplicate` with the existing
    entry's data (the engine's mapping decides)."""
    await db.remove_media_part(int(channel), msg_id)

    title = _finalize_title(title, metadata_info)
    encoded = metadata_info.get("encoded_string") or await encode_string({"chat_id": int(channel), "msg_id": msg_id})
    await apply_video_thumb_to_metadata(metadata_info, stamp, encoded, client)
    future = get_running_loop().create_future()
    await file_queue.put((metadata_info, int(channel), msg_id, size, raw_size, title, stamp, future))
    outcome = await future
    status = outcome.get("status") or {}
    ids = await db.get_media_ids_by_part(int(channel), msg_id)
    filed = await _filed_for_part(int(channel), msg_id)
    if not outcome.get("updated_id") or status.get("duplicate_skipped"):
        return {"state": "duplicate", "ids": ids, "filed": filed, "error": None}
    if placed_filed is not None and "absolute" in placed_filed:
        # §8: an absolute filing is reported in the same form it was given.
        filed = dict(placed_filed)
    elif filed is None:
        filed = placed_filed
    return {"state": "indexed", "ids": ids, "filed": filed, "error": None}


async def _placed_payload(title, channel, msg_id, placement, expected_ids, series_ids, override_id):
    """DISP-3 item 5: obtain the TV payload for a placement.
    Returns (payload, filed_form), or (None, None) when TV
    resolution fails (-> metadata_failed; nothing is inserted).
    Re-resolution at the placed S/E — never a relabel: a patched
    payload would carry the wrong episode's title/backdrop/
    overview/released fields, the exact fields insert_media reads.
    Absolute placements look the S/E pair up via TVDB
    episode_by_absolute — never computed by summing season
    lengths."""
    from Backend.helper.metadata.parse import parse_media_name
    from Backend.helper.metadata.resolvers import resolve_series

    cleaned = clean_filename(title)
    try:
        parsed = parse_media_name(cleaned) or {}
    except Exception:
        parsed = {}
    default_id = override_id
    if not default_id and expected_ids:
        default_id = expected_ids.get("imdb") or (
            str(expected_ids["tmdb"]) if expected_ids.get("tmdb") is not None else None)

    if "absolute" in placement:
        absolute = int(placement["absolute"])
        tvdb_id = (series_ids or {}).get("tvdb")
        if tvdb_id is None:
            return None, None
        from Backend.helper.metadata.providers import tvdb

        ep = await tvdb.episode_by_absolute(int(tvdb_id), absolute)
        if not ep:
            return None, None
        season, episode = ep.get("seasonNumber"), ep.get("number")
        if season is None or episode is None:
            return None, None
        encoded = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
        payload = await resolve_series(
            parsed.get("title") or cleaned, int(season), int(episode), encoded,
            year=parsed.get("year"), quality=parsed.get("quality"),
            default_id=default_id)
        if payload is None:
            return None, None
        payload["absolute_episode"] = absolute
        return payload, {"absolute": absolute}

    season, episode = int(placement["season"]), int(placement["episode"])
    meta_payload = await metadata(cleaned, int(channel), msg_id, override_id=override_id)
    if (meta_payload is not None
            and meta_payload.get("media_type") == "tv"
            and meta_payload.get("season_number") == season
            and meta_payload.get("episode_number") == episode
            and _ids_match_expected(meta_payload.get("imdb_id"),
                                    meta_payload.get("tmdb_id"), expected_ids)):
        return meta_payload, {"season": season, "episode": episode}
    encoded = (meta_payload or {}).get("encoded_string") or await encode_string(
        {"chat_id": int(channel), "msg_id": msg_id})
    payload = await resolve_series(
        (meta_payload or {}).get("title") or parsed.get("title") or cleaned,
        season, episode, encoded,
        year=(meta_payload or {}).get("year") or parsed.get("year"),
        quality=(meta_payload or {}).get("quality") or parsed.get("quality"),
        default_id=default_id)
    if payload is None:
        return None, None
    return payload, {"season": season, "episode": episode}


async def reindex_one_post(client, message, *, placement=None, expected_ids=None, series_ids=None, future_timeout=120):
    """The re-index seam: file_edited_handler's core, extracted
    branch-for-branch, returning the DISP-3 item 2 result contract
    {state, ids, filed, error}. Coordination/suppression is NOT
    here (it is the handlers' entry check); auth/supported-media
    gates stay in the handler — the seam is entered with a
    supported channel post. future_timeout is carried for the
    bridge contract: the engine's wait_for bounds the whole call;
    the seam awaits its completion future directly."""
    try:
        _, title, msg_id, raw_size, size, channel = _extract_fields(message)
        override_id = extract_default_id(caption_with_links(message))

        effective_placement = placement
        if placement is not None and "absolute" not in placement:
            # DISP-3a (binding): the S/E placement override fires
            # only for a donghua series; otherwise the seam falls
            # through to the standard path and the special files
            # under standard S00. (Absolute placements are not
            # narrowed by DISP-3a; their triggers are DISP-3b's,
            # decided before the seam is called.)
            if not await _is_donghua_series(message, series_ids, expected_ids):
                effective_placement = None

        if effective_placement is not None:
            existing = await db.get_media_ids_by_part(int(channel), msg_id)
            if existing is not None:
                filed = await _filed_for_part(int(channel), msg_id)
                if (filed == dict(effective_placement)
                        and _ids_match_expected(existing[0], existing[1], expected_ids)):
                    await _apply_rename_only(title, channel, msg_id, message, existing)
                    return {"state": "duplicate", "ids": existing,
                            "filed": filed, "error": None}
            payload, filed_form = await _placed_payload(
                title, channel, msg_id, effective_placement,
                expected_ids, series_ids, override_id)
            if payload is None:
                return {"state": "metadata_failed", "ids": None,
                        "filed": None, "error": None}
            return await _queue_and_confirm(
                client, payload, channel, msg_id, size, raw_size,
                title, message, placed_filed=filed_form)

        # Standard path — the handler's decision tree, verbatim.
        existing = await db.get_media_ids_by_part(int(channel), msg_id)
        if existing and (not override_id or _override_matches_indexed(override_id, existing[0], existing[1])):
            await _apply_rename_only(title, channel, msg_id, message, existing)
            filed = await _filed_for_part(int(channel), msg_id)
            return {"state": "duplicate", "ids": existing,
                    "filed": filed, "error": None}
        if not override_id:
            return {"state": "metadata_failed", "ids": None,
                    "filed": None, "error": None}

        LOGGER.info(f"Detected override ID '{override_id}' in edited message {msg_id}")
        metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)
        if metadata_info is None:
            LOGGER.warning(f"Metadata failed for edited file: {title} (ID: {msg_id})")
            return {"state": "metadata_failed", "ids": None,
                    "filed": None, "error": None}
        return await _queue_and_confirm(
            client, metadata_info, channel, msg_id, size, raw_size,
            title, message)
    except Exception as e:
        return {"state": "error", "ids": None, "filed": None,
                "error": str(e) or type(e).__name__}


# Registration happens at import (DESIGN §4): the app loads its
# plugins before the API can serve a fix, and the engine reaches
# this implementation only through the bridge.
librarian_reindex.register(reindex_one_post)


#----- Edited file: same title (or no id) renames it, another title's id re-indexes it
@Client.on_edited_message(filters.channel & (filters.document | filters.video))
async def file_edited_handler(client: Client, message: Message):
    if str(message.chat.id) not in SettingsManager.current().auth_channels:
        return
    #----- Librarian coordination (§8; plan §3.3): a post under a
    #----- non-terminal fix operation is the engine's alone.
    if await _has_active_fix(message):
        return
    try:
        if not _is_supported_media(message):
            return
        await reindex_one_post(client, message)
    except Exception as e:
        LOGGER.error(f"Error handling edited generic file {message.id}: {e}")


#----- Purge database entries for messages deleted from auth channels
@Client.on_deleted_messages(filters.channel)
async def file_deleted_handler(client: Client, messages: list[Message]):
    try:
        for message in messages:
            if not message.chat:
                continue
            if not (str(message.chat.id) in SettingsManager.current().auth_channels or _is_manual_channel(message.chat.id)):
                continue
            channel = str(message.chat.id).replace("-100", "")
            msg_id = message.id
            try:
                if await db.remove_media_part(int(channel), msg_id):
                    LOGGER.info(f"Automatically purged deleted message {msg_id} from database.")
                if await remove_subtitle(int(channel), msg_id):
                    LOGGER.info(f"Automatically purged deleted subtitle {msg_id} from database.")
            except Exception as ex:
                LOGGER.error(f"Failed to scrub deleted message {msg_id}: {ex}")
    except Exception as e:
        LOGGER.error(f"Error handling deleted messages: {e}")
