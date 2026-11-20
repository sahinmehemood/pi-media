"""Hermes Librarian v4 — Hermes is the brain, the app is the hands.

The reject ledger records every file the metadata pipeline rejects (the
skip pile, live failures, scan failures, suspect matches). A locator joins
those files back to their original channel posts. The Media Manager
(Hermes) pulls per-channel work packets, decides each series, and submits
fix plans; a server-side gate dry-runs the app's OWN matcher on every
proposed caption before Yan sees it. On Yan's approval in the bot DM, the
apply engine edits the originals' captions through the Media Manager bot
(the app then re-indexes natively), verifies, cleans the skip copies, and
audits everything with rollback.

Design: docs/librarian/DESIGN-v4.md. Hard rules:
- the Hermes/Media Manager bot is never involved in streaming;
- the alias map is taught ONLY by Yan-approved applies;
- manual channels and subtitle files are never touched;
- a fix caption always carries an ID link — without one the app will not
  re-index an unindexed file (file_edited_handler).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from Backend.logger import LOGGER

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_librarian.py)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_EP_SE_RE = re.compile(r"\bS(\d{1,3})E(\d{1,4})(?:\s*[-~]\s*E?(\d{1,4}))?\b", re.I)
_EP_SP_RE = re.compile(r"\bSP(\d{1,3})\s*[-_. ]*E(\d{1,4})\b", re.I)
_EP_E_RE = re.compile(r"\b(?:EP|E)(\d{1,4})\b", re.I)
_ABS_RE = re.compile(r"^(.+?)[\s._-]+(\d{3,4})$")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_NOISE_TOKENS = {
    "esub", "e-sub", "sub", "subbed", "dub", "dubbed", "eng", "english",
    "hindi", "multi", "web", "webdl", "web-dl", "bluray", "hdrip", "dvdrip",
    "x264", "x265", "hevc", "h264", "h265", "aac", "ddp5", "atmos", "hdr",
    "4k", "uhd", "fhd", "hd", "sd", "480p", "720p", "1080p", "1440p",
    "2160p", "540p", "360p", "240p", "mkv", "mp4", "avi", "srt",
}


def _clean_stem_text(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("_", " ").replace(".", " ")
    parts = []
    for tok in re.split(r"[\s\-]+", text):
        t = tok.strip().lower()
        if not t or t in _NOISE_TOKENS:
            continue
        parts.append(t)
    return " ".join(parts)


def name_bits(caption: str = "", file_name: str = "") -> dict:
    """Extract (stem, season, episode, special, year) from a caption or
    file name. The stem is the clustering key: the text before the
    episode token, cleaned of quality/codec/group noise."""
    raw = (caption or "").strip().split("\n")[0].strip() or (file_name or "")
    name = re.sub(r"\.(mkv|mp4|avi|mov|webm|ts)$", "", raw, flags=re.I)
    name = re.sub(r"\.part\d+$", "", name, flags=re.I)

    year = None
    ym = _YEAR_RE.search(name)
    if ym:
        year = int(ym.group(1))

    season = episode = special = None
    cut = None
    m = _EP_SP_RE.search(name)
    if m:
        special, episode, cut = int(m.group(1)), int(m.group(2)), m.start()
    else:
        m = _EP_SE_RE.search(name)
        if m:
            season, episode, cut = int(m.group(1)), int(m.group(2)), m.start()
        else:
            m = _EP_E_RE.search(name)
            if m:
                episode, cut = int(m.group(1)), m.start()
            else:
                m = _ABS_RE.match(name.strip())
                if m:
                    episode, cut = int(m.group(2)), m.start(2)

    head = name[:cut] if cut is not None else name
    stem = _clean_stem_text(head)
    if special is not None:
        # SP<n> arc markers belong to the stem: SP1 and SP3 files of the
        # same show are different runs with overlapping episode numbers.
        stem = f"{stem} sp{special}".strip()
    if year and str(year) in stem.split():
        stem = " ".join(t for t in stem.split() if t != str(year))
    return {
        "stem": stem,
        "season": season,
        "episode": episode,
        "special": special,
        "year": year,
        "raw": raw,
    }


def reason_code_for(reason_text: str) -> str:
    text = (reason_text or "").lower()
    if "multi-part" in text or "split" in text:
        return "multipart"
    if "combined" in text and "season" in text:
        return "combined-pack"
    if "multiple seasons" in text:
        return "multi-season"
    if "no video quality" in text:
        return "no-quality"
    if "no title" in text:
        return "no-title"
    if "could not be parsed" in text:
        return "parse-failure"
    if "could not match" in text:
        return "no-match"
    if "more than one title" in text:
        return "suspect-multi-index"
    if "slot" in text:
        return "suspect-slot-collision"
    return "unknown"


def initials_of(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    return "".join(w[0] for w in words if w).lower()


def stem_title_match(stem: str, title: str) -> bool:
    """True when an abbreviation stem plausibly names `title`: either the
    stem's first token equals the title's word initials (BTTH = Battle
    Through The Heavens), or the whole stem equals the cleaned title."""
    if not stem or not title:
        return False
    first = stem.split()[0]
    if len(first) >= 2 and initials_of(title) == first:
        return True
    return _clean_stem_text(title) == stem


def cluster_items(items: list[dict]) -> list[dict]:
    """Group ledger items into runs: episodic items cluster by
    (chat_id, stem); movies (no episode and no season) stay single."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for it in items:
        bits = it.get("bits") or name_bits(it.get("caption", ""), it.get("file_name", ""))
        chat = it.get("chat_id")
        if bits.get("episode") is None and bits.get("season") is None:
            key = ("single", it.get("key"))
        else:
            key = ("run", chat, bits.get("stem") or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)

    runs = []
    for key in order:
        members = groups[key]
        bits0 = members[0].get("bits") or name_bits(
            members[0].get("caption", ""), members[0].get("file_name", ""))
        episodes = sorted({(m.get("bits") or {}).get("episode") for m in members
                           if (m.get("bits") or {}).get("episode") is not None})
        missing = []
        if len(episodes) > 1:
            missing = [e for e in range(episodes[0], episodes[-1] + 1) if e not in episodes]
        chat = members[0].get("chat_id")
        stem = bits0.get("stem") or ""
        runs.append({
            "run_id": f"{chat if chat is not None else 'unknown'}|{stem or members[0].get('key')}",
            "chat_id": chat,
            "stem": stem,
            "season": bits0.get("season"),
            "special": bits0.get("special"),
            "year": bits0.get("year"),
            "episodes": episodes,
            "missing_episodes": missing,
            "item_keys": [m.get("key") for m in members],
            "items": members,
        })
    return runs


def assign_tier(layer: Optional[str], gate: Optional[dict]) -> str:
    """Tier A: gate-verified with no unresolved checks, from a stamped
    ID (L0) or a taught alias (L1). Tier B: gate-verified but from
    deterministic search/coherence (L2) or with skipped checks.
    Tier C: unverifiable or no proposal — never auto-touched."""
    if not layer or not gate or not gate.get("passed"):
        return "C"
    skipped = [c for c in gate.get("checks", []) if c.get("ok") is None]
    if layer in ("L0", "L1") and not skipped:
        return "A"
    return "B"


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Ledger store (tracking DB)
# ---------------------------------------------------------------------------

def _tracking():
    try:
        from Backend import db
        return (getattr(db, "dbs", None) or {}).get("tracking")
    except Exception:
        return None


def _ledger_coll():
    tracking = _tracking()
    return tracking["reject_ledger"] if tracking is not None else None


def _runs_coll():
    tracking = _tracking()
    return tracking["curation_runs"] if tracking is not None else None


def _alias_coll():
    tracking = _tracking()
    return tracking["alias_map"] if tracking is not None else None


async def record_reject(fields: dict) -> bool:
    """Upsert one ledger entry keyed by `key` (file_unique_id, or a
    tg:<chat>:<msg> key for suspects without one). Repeat sightings of
    the same file update the entry instead of duplicating it — the
    ledger never re-inflates the way the skip pile did."""
    coll = _ledger_coll()
    if coll is None or not fields.get("key"):
        LOGGER.warning("[Librarian] ledger unavailable or key missing; record skipped")
        return False
    now = time.time()
    mutable = {
        "caption": fields.get("caption", ""),
        "file_name": fields.get("file_name", ""),
        "size": fields.get("size", 0),
        "height": fields.get("height"),
        "reason": fields.get("reason", ""),
        "reason_code": fields.get("reason_code", "unknown"),
        "updated_at": now,
    }
    if fields.get("status"):
        mutable["status"] = fields["status"]
    if fields.get("current_match") is not None:
        mutable["current_match"] = fields["current_match"]
    on_insert = {
        "key": fields["key"],
        "file_unique_id": fields.get("file_unique_id"),
        "chat_id": fields.get("chat_id"),
        "msg_id": fields.get("msg_id"),
        "origin": fields.get("origin", "live-fail"),
        "origin_hint": fields.get("origin_hint", ""),
        "status": fields.get("status") or "open",
        "parse": _jsonable(fields.get("parse")),
        "bits": fields.get("bits"),
        "first_seen": now,
    }
    update = {"$set": mutable, "$setOnInsert": on_insert,
              "$push": {"history": {"ts": now, "event": fields.get("origin", "seen")}}}
    if fields.get("skip_msg_id"):
        update["$addToSet"] = {"skip_msg_ids": fields["skip_msg_id"]}
    try:
        await coll.update_one({"_id": fields["key"]}, update, upsert=True)
        invalidate_counts()
        return True
    except Exception as e:
        LOGGER.warning(f"[Librarian] ledger write failed: {e}")
        return False


def _media_fields(message) -> dict:
    media = None
    kind = ""
    for attr, k in (("video", "video"), ("document", "document"),
                    ("audio", "audio"), ("animation", "animation"),
                    ("photo", "photo")):
        media = getattr(message, attr, None)
        if media:
            kind = k
            break
    if not media:
        return {}
    if isinstance(media, list):
        media = media[-1]
    return {
        "file_unique_id": getattr(media, "file_unique_id", None),
        "file_name": getattr(media, "file_name", "") or "",
        "size": getattr(media, "file_size", 0) or 0,
        "height": getattr(media, "height", None) if kind == "video" else None,
        "kind": kind,
    }


def _failure_analysis(caption: str, file_name: str) -> tuple[str, Optional[dict]]:
    target = (caption or "").strip().split("\n")[0] or (file_name or "")
    try:
        from Backend.helper.metadata.parse import analyze_metadata_failure, parse_media_name
        reason = analyze_metadata_failure(target)
        try:
            parsed = _jsonable(parse_media_name(target))
        except Exception:
            parsed = None
        return reason, parsed
    except Exception:
        return "", None


async def record_from_message(message, origin: str) -> None:
    """Ledger hook for the skip-channel router. Best-effort by design:
    a ledger failure must never break ingest, so nothing raises."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        if not SettingsManager.current().librarian_enabled:
            return
        fields = _media_fields(message)
        if not fields.get("file_unique_id"):
            return
        caption = getattr(message, "caption", "") or ""
        reason, parsed = _failure_analysis(caption, fields["file_name"])
        await record_reject({
            "key": fields["file_unique_id"],
            "file_unique_id": fields["file_unique_id"],
            "chat_id": getattr(getattr(message, "chat", None), "id", None),
            "msg_id": getattr(message, "id", None),
            "origin": origin,
            "caption": caption,
            "file_name": fields["file_name"],
            "size": fields["size"],
            "height": fields["height"],
            "reason": reason,
            "reason_code": reason_code_for(reason),
            "parse": parsed,
            "bits": name_bits(caption, fields["file_name"]),
        })
    except Exception as e:
        LOGGER.warning(f"[Librarian] record_from_message failed: {e}")


async def ledger_counts() -> dict:
    coll = _ledger_coll()
    if coll is None:
        return {"available": False}
    out = {"available": True, "total": 0, "by_status": {}, "by_reason": {}, "by_origin": {}}
    try:
        out["total"] = await coll.count_documents({})
        for field, bucket in (("status", "by_status"), ("reason_code", "by_reason"),
                              ("origin", "by_origin")):
            async for row in coll.aggregate([
                    {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}}]):
                out[bucket][row.get("_id") or "unknown"] = row.get("n", 0)
    except Exception as e:
        LOGGER.warning(f"[Librarian] counts failed: {e}")
    runs = _runs_coll()
    if runs is not None:
        try:
            out["runs"] = await runs.count_documents({})
            out["runs_by_tier"] = {}
            async for row in runs.aggregate([
                    {"$group": {"_id": "$tier", "n": {"$sum": 1}}}]):
                out["runs_by_tier"][row.get("_id") or "?"] = row.get("n", 0)
        except Exception:
            pass
    return out


async def list_ledger(status: str = "", reason: str = "", limit: int = 100) -> list[dict]:
    coll = _ledger_coll()
    if coll is None:
        return []
    query = {}
    if status:
        query["status"] = status
    if reason:
        query["reason_code"] = reason
    out = []
    try:
        cursor = coll.find(query).sort("updated_at", -1).limit(max(1, min(500, limit)))
        async for doc in cursor:
            doc = dict(doc)
            doc.pop("history", None)
            doc.pop("parse", None)
            out.append(_jsonable(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] list failed: {e}")
    return out


async def list_runs(limit: int = 100) -> list[dict]:
    coll = _runs_coll()
    if coll is None:
        return []
    out = []
    try:
        cursor = coll.find({}).sort("updated_at", -1).limit(max(1, min(200, limit)))
        async for doc in cursor:
            doc = dict(doc)
            doc.pop("items", None)
            out.append(_jsonable(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] runs list failed: {e}")
    return out


async def lookup_alias(chat_id, stem: str) -> Optional[dict]:
    coll = _alias_coll()
    if coll is None or chat_id is None or not stem:
        return None
    try:
        return await coll.find_one({"_id": f"{chat_id}:{stem}"})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Resolution funnel L0-L2 + verification gate
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# v4 — Locator: join skip-pile files back to their original channel posts
# ---------------------------------------------------------------------------

def _locator_coll():
    tracking = _tracking()
    return tracking["locator"] if tracking is not None else None


def _proposals_coll():
    tracking = _tracking()
    return tracking["curation_proposals"] if tracking is not None else None


def _audit_coll():
    tracking = _tracking()
    return tracking["curation_audit"] if tracking is not None else None


def _norm_chat(value) -> Optional[int]:
    try:
        return int(str(value).replace("-100", ""))
    except (TypeError, ValueError):
        return None


def _full_chat(value) -> Optional[int]:
    n = _norm_chat(value)
    return int(f"-100{n}") if n is not None else None


async def run_locator(channel_id) -> dict:
    """Walk one auth channel with the userbot and record, for every media
    message, file_unique_id -> (chat_id, msg_id, caption, file_name).

    Read-only on Telegram. The walk is newest-first; the LOWEST msg_id wins
    for a duplicated file (the original post), so later sightings overwrite.
    """
    channel_id = _full_chat(channel_id)
    if channel_id is None:
        return {"ok": False, "message": "Bad channel id."}
    coll = _locator_coll()
    if coll is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    from Backend.pyrofork import bot as botmod
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return {"ok": False, "message": "Userbot is not configured."}
    seen: dict[str, dict] = {}
    read = 0
    try:
        async for m in userbot.get_chat_history(channel_id):
            read += 1
            media = getattr(m, "video", None) or getattr(m, "document", None)
            uid = getattr(media, "file_unique_id", None) if media else None
            if not uid:
                continue
            seen[uid] = {
                "_id": uid, "chat_id": channel_id, "msg_id": m.id,
                "caption": m.caption or "",
                "file_name": getattr(media, "file_name", None) or "",
                "size": getattr(media, "file_size", None),
                "height": getattr(media, "height", None),
                "updated_at": time.time(),
            }
    except Exception as e:
        return {"ok": False, "message": f"Locator walk failed: {e}"}
    for doc in seen.values():
        try:
            await coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except Exception as e:
            LOGGER.warning(f"[Librarian] locator upsert failed: {e}")
    located = 0
    ledger = _ledger_coll()
    if ledger is not None and seen:
        try:
            located = await ledger.count_documents(
                {"_id": {"$in": list(seen.keys())}})
        except Exception:
            located = 0
    LOGGER.info(f"[Librarian] locator channel {channel_id}: {read} read, "
                f"{len(seen)} files, {located} ledger items located")
    return {"ok": True, "channel": channel_id, "messages_read": read,
            "files": len(seen), "ledger_items_located": located}


async def _library_index_for_channel(channel_id) -> dict:
    """{(stripped_chat, msg_id): {title, media_type, season, episode,
    quality}} for every library entry whose file lives in this channel.
    DB-only (the telegram entry ids decode locally)."""
    from Backend import db as _db
    from Backend.helper.encrypt import decode_string
    target = _norm_chat(channel_id)
    index: dict = {}
    dbs = getattr(_db, "dbs", None) or {}
    for name, sdb in dbs.items():
        if name == "tracking" or sdb is None:
            continue
        for coll_name, mtype in (("movie", "movie"), ("tv", "tv")):
            try:
                async for doc in sdb[coll_name].find(
                        {}, {"title": 1, "telegram": 1, "seasons": 1}):
                    title = doc.get("title") or "?"
                    slots = [(None, None, doc.get("telegram") or [])]
                    for season in doc.get("seasons") or []:
                        for ep in season.get("episodes") or []:
                            slots.append((season.get("season"),
                                          ep.get("episode"),
                                          ep.get("telegram") or []))
                    for s_no, e_no, entries in slots:
                        for entry in entries:
                            enc = entry.get("id")
                            if not enc:
                                continue
                            try:
                                decoded = await decode_string(enc)
                            except Exception:
                                continue
                            chat = _norm_chat((decoded or {}).get("chat_id"))
                            msg = (decoded or {}).get("msg_id")
                            if chat != target or msg is None:
                                continue
                            index[(chat, msg)] = {
                                "title": title, "media_type": mtype,
                                "season": s_no, "episode": e_no,
                                "quality": entry.get("quality"),
                            }
            except Exception as e:
                LOGGER.warning(f"[Librarian] library index failed "
                               f"({name}.{coll_name}): {e}")
    return index


async def _record_scan_entry(coll, uid: str, post: dict,
                             scan_status: str, match) -> None:
    """Ledger upsert for one scanned file. Scan facts are refreshed on
    every scan; an existing entry's workflow status is NEVER touched —
    only brand-new entries take the scan status (indexed / unindexed),
    so rejects keep flowing through the waiting queue untouched."""
    now = time.time()
    set_fields = {
        "scan_status": scan_status,
        "current_match": match,
        "scan_chat_id": post["chat_id"],
        "scan_msg_id": post["msg_id"],
        "caption": post["caption"],
        "file_name": post["file_name"],
        "size": post["size"],
        "height": post["height"],
        "scanned_at": now,
        "updated_at": now,
    }
    on_insert = {
        "key": uid,
        "file_unique_id": uid,
        "chat_id": post["chat_id"],
        "msg_id": post["msg_id"],
        "origin": "channel-scan",
        "origin_hint": "",
        "status": scan_status,
        "reason": "",
        "reason_code": scan_status,
        "parse": None,
        "bits": name_bits(post["caption"], post["file_name"]),
        "first_seen": now,
    }
    await coll.update_one({"_id": uid},
                          {"$set": set_fields, "$setOnInsert": on_insert},
                          upsert=True)
    invalidate_counts()


def _scan_state_coll():
    tracking = _tracking()
    return tracking["scan_state"] if tracking is not None else None


async def run_channel_scan(channel_id, incremental: bool = True) -> dict:
    """Walk one source channel with the userbot and record EVERY media
    file in the ledger: already-indexed files (with what they are
    indexed as) and unindexed ones, alongside the rejects the ledger
    already holds. Also refreshes the locator map, like run_locator.
    Read-only on Telegram. Stoppable from the page: the walk checks
    ACTION_STATE['stop_requested'] as it goes and keeps whatever it
    has recorded when stopped.

    Incremental (the default): the walk stops at the channel's stored
    watermark — the newest message id a previous scan reached — so a
    repeat scan only walks new posts. The watermark advances to the
    newest message seen; because the walk is newest-first and the
    region below the old watermark was covered by earlier scans, the
    covered range stays contiguous even when a walk is stopped."""
    channel_id = _full_chat(channel_id)
    if channel_id is None:
        return {"ok": False, "message": "Bad channel id."}
    ledger = _ledger_coll()
    if ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    from Backend.pyrofork import bot as botmod
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return {"ok": False, "message": "Userbot is not configured."}
    watermark = 0
    if incremental:
        scoll = _scan_state_coll()
        if scoll is not None:
            try:
                sdoc = await scoll.find_one({"_id": str(channel_id)})
                watermark = int((sdoc or {}).get("last_msg_id") or 0)
            except Exception:
                watermark = 0
    lib_index = await _library_index_for_channel(channel_id)
    seen: dict[str, dict] = {}
    read = 0
    stopped = False
    max_msg_id = 0
    try:
        async for m in userbot.get_chat_history(channel_id):
            if watermark and m.id <= watermark:
                break
            if m.id > max_msg_id:
                max_msg_id = m.id
            read += 1
            if read % 100 == 0:
                if ACTION_STATE.get("stop_requested"):
                    stopped = True
                    break
                ACTION_STATE["progress"] = {
                    "read": read, "files": len(seen), "channel": channel_id}
                await asyncio.sleep(0.05)
            fields = _media_fields(m)
            uid = fields.get("file_unique_id")
            if not uid:
                continue
            # Newest-first walk: later sightings are older posts, so the
            # last write wins and ends up the ORIGINAL post (locator rule).
            seen[uid] = {
                "chat_id": channel_id, "msg_id": m.id,
                "caption": m.caption or "",
                "file_name": fields["file_name"],
                "size": fields["size"], "height": fields["height"],
            }
    except Exception as e:
        return {"ok": False, "message": f"Channel scan failed: {e}"}
    loc_coll = _locator_coll()
    if loc_coll is not None:
        for uid, post in seen.items():
            try:
                await loc_coll.replace_one(
                    {"_id": uid}, {"_id": uid, **post,
                                   "updated_at": time.time()}, upsert=True)
            except Exception:
                pass
    stripped = _norm_chat(channel_id)
    indexed = unindexed = 0
    for uid, post in seen.items():
        match = lib_index.get((stripped, post["msg_id"]))
        scan_status = "indexed" if match else "unindexed"
        if match:
            indexed += 1
        else:
            unindexed += 1
        try:
            await _record_scan_entry(ledger, uid, post, scan_status, match)
        except Exception as e:
            LOGGER.warning(f"[Librarian] scan entry failed: {e}")
    if max_msg_id > watermark:
        scoll = _scan_state_coll()
        if scoll is not None:
            try:
                await scoll.update_one(
                    {"_id": str(channel_id)},
                    {"$set": {"last_msg_id": max_msg_id,
                              "updated_at": time.time()}}, upsert=True)
            except Exception:
                pass
    ACTION_STATE["progress"] = {"read": read, "files": len(seen),
                                "channel": channel_id}
    LOGGER.info(f"[Librarian] scan channel {channel_id}: {read} read, "
                f"{len(seen)} files, {indexed} indexed, "
                f"{unindexed} unindexed, stopped={stopped}")
    return {"ok": True, "channel": channel_id, "messages_read": read,
            "files": len(seen), "indexed": indexed,
            "unindexed": unindexed, "stopped": stopped,
            "incremental": bool(watermark), "watermark": max_msg_id}


# ---------------------------------------------------------------------
# Pre-identification (L5): the app's own deterministic first pass over
# waiting files. A taught alias for the file's stem gives a
# high-confidence candidate; otherwise a provider search on the
# cleaned stem, kept only when it resolves to exactly one strong
# match. Candidates are EVIDENCE for the bot (they ride along in
# packets and in the "ready" scope) — never decisions: the bot still
# confirms every one through the gate before anything is edited.
# ---------------------------------------------------------------------
async def _preidentify_one(doc) -> Optional[dict]:
    bits = name_bits(doc.get("caption") or "", doc.get("file_name") or "")
    stem = bits.get("stem") or ""
    chat = doc.get("chat_id")
    if stem and chat is not None:
        alias = await lookup_alias(chat, stem)
        if alias and (alias.get("tmdb_id") or alias.get("imdb_id")):
            return {"title": alias.get("title") or "",
                    "tmdb_id": alias.get("tmdb_id"),
                    "imdb_id": alias.get("imdb_id"),
                    "media_type": alias.get("media_type"),
                    "year": bits.get("year"),
                    "season": bits.get("season"),
                    "episode": bits.get("episode"),
                    "confidence": "high", "source": "alias",
                    "at": time.time()}
    if len(stem) >= 4:
        try:
            from Backend.helper.metadata.entry import search_any_candidates
            if not callable(search_any_candidates):
                raise TypeError("candidate search unavailable")
            results = await search_any_candidates(
                stem.replace(".", " "), year=bits.get("year"), limit=5)
        except Exception:
            results = []
        strong = [r for r in (results or []) if r.get("title")]
        pick = None
        if len(strong) == 1:
            pick = strong[0]
        elif bits.get("year") and strong:
            same_year = [r for r in strong
                         if str(r.get("year") or "") == str(bits["year"])]
            if len(same_year) == 1:
                pick = same_year[0]
        if pick:
            return {"title": pick.get("title") or "",
                    "tmdb_id": pick.get("tmdb_id"),
                    "imdb_id": pick.get("imdb_id"),
                    "media_type": pick.get("media_type"),
                    "year": bits.get("year"),
                    "season": bits.get("season"),
                    "episode": bits.get("episode"),
                    "confidence": "medium",
                    "source": f"search:{pick.get('source')}",
                    "at": time.time()}
    return None


async def run_preidentify(limit: int = 40) -> dict:
    """One sweep: attach candidates to up to `limit` waiting files
    that have none yet. Idempotent — files with a candidate (or a
    live proposal) are skipped."""
    ledger = _ledger_coll()
    if ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    checked = attached = 0
    try:
        cursor = ledger.find(
            {"status": {"$in": ["open", "unresolved"]}}).sort("first_seen", 1)
        async for doc in cursor:
            if checked >= max(1, int(limit)):
                break
            if doc.get("candidate") or doc.get("proposal_id"):
                continue
            checked += 1
            cand = await _preidentify_one(doc)
            if cand:
                await ledger.update_one(
                    {"_id": doc["_id"]}, {"$set": {"candidate": cand}})
                attached += 1
    except Exception as e:
        return {"ok": False, "message": f"Pre-identify sweep failed: {e}"}
    LOGGER.info(f"[Librarian] pre-identify: {checked} checked, "
                f"{attached} candidates attached")
    return {"ok": True, "checked": checked, "attached": attached}


async def list_preidentified(limit: int = 60) -> list[dict]:
    """Waiting files that carry a server-attached candidate — the
    bot's head start, and Yan's review list on the page."""
    ledger = _ledger_coll()
    if ledger is None:
        return []
    out = []
    try:
        cursor = ledger.find(
            {"status": {"$in": ["open", "unresolved"]},
             "candidate": {"$exists": True}}).sort("first_seen", 1)
        async for doc in cursor:
            out.append({"key": doc["_id"],
                        "caption": (doc.get("caption") or "").split("\n")[0],
                        "chat_id": doc.get("chat_id"),
                        "candidate": doc.get("candidate")})
            if len(out) >= max(1, min(200, int(limit))):
                break
    except Exception as e:
        LOGGER.warning(f"[Librarian] pre-identified list failed: {e}")
    return out


async def clear_candidate(key: str) -> dict:
    """Drop a wrong server candidate so the file goes back to a
    clean slate (and a later sweep may try again)."""
    ledger = _ledger_coll()
    if ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    if await ledger.find_one({"_id": key}) is None:
        return {"ok": False, "message": "No such file."}
    await ledger.update_one({"_id": key},
                            {"$unset": {"candidate": ""}})
    return {"ok": True, "message": "Candidate cleared."}


async def _located_map(keys: list[str]) -> dict:
    coll = _locator_coll()
    if coll is None or not keys:
        return {}
    out = {}
    try:
        async for doc in coll.find({"_id": {"$in": keys}}):
            out[doc["_id"]] = doc
    except Exception as e:
        LOGGER.warning(f"[Librarian] locator read failed: {e}")
    return out


async def _channel_title_index() -> dict:
    """{stripped chat_id: [media doc summaries]} from the storage DBs —
    the context of what each channel already has indexed."""
    from Backend import db as _db
    from Backend.helper.encrypt import decode_string
    index: dict[int, list] = {}
    dbs = getattr(_db, "dbs", None) or {}
    for name, sdb in dbs.items():
        if not name.startswith("storage"):
            continue
        for media_type in ("movie", "tv"):
            try:
                async for doc in sdb[media_type].find(
                        {}, {"title": 1, "release_year": 1, "tmdb_id": 1,
                             "imdb_id": 1, "telegram": 1, "seasons": 1}):
                    entries = list(doc.get("telegram") or [])
                    for season in doc.get("seasons") or []:
                        for ep in season.get("episodes") or []:
                            entries.extend(ep.get("telegram") or [])
                    chats = set()
                    for entry in entries:
                        try:
                            decoded = await decode_string(entry.get("id"))
                            chats.add(int(decoded["chat_id"]))
                        except Exception:
                            continue
                    for chat in chats:
                        index.setdefault(chat, []).append({
                            "title": doc.get("title"),
                            "year": doc.get("release_year"),
                            "tmdb_id": doc.get("tmdb_id"),
                            "imdb_id": doc.get("imdb_id"),
                            "media_type": media_type,
                        })
            except Exception:
                continue
    return index


# ---------------------------------------------------------------------------
# v4 — Work packets for the Media Manager
# ---------------------------------------------------------------------------

def _packets_coll():
    tracking = _tracking()
    return tracking["curation_packets"] if tracking is not None else None


def _norm_caption(text: str) -> str:
    return " ".join((text or "").split())


def _group_stem(stem: str) -> str:
    """Grouping key for the runs hint: drop trailing pure-number tokens
    the parser could not read as numbering (e.g. picture-drama decimals
    'Turn 19.02' -> stem 'code geass turn 19 02' -> group 'code geass
    turn'), so one work's files land in one run."""
    tokens = (stem or "").split()
    while tokens and re.fullmatch(r"\d+(\.\d+)?", tokens[-1]):
        tokens.pop()
    return " ".join(tokens) or (stem or "(single)")


_PACKET_SCOPES = {
    "waiting": ["open", "unresolved"],
    "ready": ["open", "unresolved"],
    "indexed": ["indexed"],
    "unindexed": ["unindexed"],
    "all": ["open", "unresolved", "indexed", "unindexed"],
}


async def get_packet(channel_id, limit: int = None,
                     scope: str = "waiting") -> dict:
    """The next batch of unprocessed failed files for ONE channel, with the
    context the Media Manager needs to decide well: each item's original
    post (via the locator), the titles already indexed from this channel,
    and the aliases already taught for it.

    v4.1: items carry short numbers (n) and the packet gets an id — the
    bot references numbers, never raw file ids (a copied-id slip pointed
    one plan at the wrong file). Items are also pre-grouped into a runs
    hint so the bot decides whole series at once instead of clustering
    raw items itself."""
    channel_id = _full_chat(channel_id)
    if channel_id is None:
        return {"ok": False, "message": "Bad channel id."}
    ledger = _ledger_coll()
    if ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    state = bot_state()
    if state == "killed":
        return {"ok": False,
                "message": "The Media Manager is stopped (killed) from "
                           "the Librarian page. No work until Yan "
                           "restarts it there."}
    if state == "paused":
        return {"ok": True, "paused": True, "channel": channel_id,
                "scope": scope, "items": [], "runs_hint": [],
                "packet_id": None,
                "message": "The bot is paused from the Librarian page "
                           "— no new work right now."}
    policy = resolve_channel_policy(_policies_stored(), channel_id)
    if not limit:
        try:
            from Backend.helper.settings_manager import SettingsManager
            limit = SettingsManager.current().librarian_batch_size
        except Exception:
            limit = 60
    limit = max(1, min(200, int(limit)))
    stripped = _norm_chat(channel_id)
    statuses = _PACKET_SCOPES.get(scope) or _PACKET_SCOPES["waiting"]
    candidates: list[dict] = []
    try:
        cursor = ledger.find(
            {"status": {"$in": statuses}}).sort("first_seen", 1)
        async for doc in cursor:
            candidates.append(dict(doc))
    except Exception as e:
        return {"ok": False, "message": f"Ledger read failed: {e}"}
    keys = [d["_id"] for d in candidates]
    located = await _located_map(keys)
    items = []
    for doc in candidates:
        if len(items) >= limit:
            break
        loc = located.get(doc["_id"])
        item_chat = doc.get("chat_id") or (loc or {}).get("chat_id")
        if _norm_chat(item_chat) != stripped:
            continue
        if doc.get("proposal_id"):
            continue
        if _policy_denied(policy, doc):
            continue
        if scope == "ready" and not doc.get("candidate"):
            continue
        caption = (loc or {}).get("caption") or doc.get("caption") or ""
        file_name = (loc or {}).get("file_name") or doc.get("file_name") or ""
        bits = name_bits(caption, file_name)
        items.append({
            "n": len(items) + 1,
            "key": doc["_id"],
            "caption": caption,
            "file_name": file_name,
            "size": (loc or {}).get("size") or doc.get("size"),
            "height": (loc or {}).get("height") or doc.get("height"),
            "reason": doc.get("reason") or "",
            "reason_code": doc.get("reason_code") or "",
            "parse": doc.get("parse"),
            "special": bits.get("special") is not None,
            "located": bool(loc) or bool(doc.get("chat_id")),
            "chat_id": item_chat,
            "msg_id": (loc or {}).get("msg_id") or doc.get("msg_id"),
            "skip_msg_ids": doc.get("skip_msg_ids") or [],
            "scan_status": doc.get("scan_status"),
            "current_match": doc.get("current_match"),
            "candidate": doc.get("candidate"),
        })
    # Runs hint: group the batch by naming stem (a hint for the bot's
    # grouping, never a decision — the bot still resolves each run).
    runs: dict[str, dict] = {}
    for it in items:
        bits = name_bits(it["caption"], it["file_name"])
        stem = _group_stem(bits.get("stem") or "(single)")
        run = runs.setdefault(stem, {"stem": stem, "numbers": [],
                                     "episodes": [], "special": False})
        run["numbers"].append(it["n"])
        if bits.get("episode") is not None:
            run["episodes"].append(bits["episode"])
        if bits.get("special") is not None:
            run["special"] = True
    runs_hint = []
    for run in runs.values():
        eps = sorted(set(run["episodes"]))
        run["episodes"] = eps
        run["count"] = len(run["numbers"])
        run["missing_episodes"] = (
            [e for e in range(min(eps), max(eps) + 1) if e not in eps]
            if len(eps) > 1 else [])
        runs_hint.append(run)
    runs_hint.sort(key=lambda r: -r["count"])
    # Persist the packet so proposals can reference items by number.
    packet_id = f"pkt{int(time.time())}_{abs(hash((channel_id, len(items)))) % 99999}"
    pcoll = _packets_coll()
    if pcoll is not None:
        try:
            await pcoll.insert_one({
                "_id": packet_id, "channel": channel_id,
                "created_at": time.time(),
                "items": [{"n": it["n"], "key": it["key"],
                           "caption": it["caption"],
                           "file_name": it["file_name"]} for it in items]})
            await pcoll.delete_many(
                {"created_at": {"$lt": time.time() - 7 * 86400}})
        except Exception as e:
            LOGGER.warning(f"[Librarian] packet persist failed: {e}")
    title_index = await _channel_title_index()
    aliases = []
    acoll = _alias_coll()
    if acoll is not None:
        try:
            async for a in acoll.find({"channel": channel_id}):
                aliases.append(_jsonable(a))
        except Exception:
            pass
    return {
        "ok": True, "channel": channel_id, "packet_id": packet_id,
        "scope": scope if scope in _PACKET_SCOPES else "waiting",
        "items": items, "runs_hint": runs_hint,
        "indexed_titles": title_index.get(stripped, []),
        "aliases": aliases,
        "waiting_total": len(candidates),
    }


# ---------------------------------------------------------------------------
# v4 — Proposals, the verification gate, apply, rollback
# ---------------------------------------------------------------------------

def _plan_stamp_ok(new_caption: str) -> Optional[str]:
    """The ID a fix caption carries (the app requires one to re-index)."""
    try:
        from Backend.helper.metadata.common import extract_default_id
        return extract_default_id(new_caption or "")
    except Exception:
        return None


def _effective_auto_level(level: str, policy: dict | None, doc) -> str:
    """One item's auto-apply level: the channel policy's override
    wins over the global ladder, and an "ask" scope never
    auto-applies."""
    eff = level
    if policy:
        if policy.get("auto") not in (None, "inherit"):
            eff = policy["auto"]
        if policy["scopes"].get(_item_scope(doc)) == "ask":
            return "off"
    return eff


def _auto_apply_eligible(level: str, items: list[dict],
                         originals: dict, policies: dict | None = None) -> bool:
    """The automation ladder, decided server-side (the Settings page is
    the bot's permission slip — the bot cannot grant itself more):
    - "all": any gate-passed plan may apply unattended.
    - "safe": gate-passed plans whose originals carry NO special
      marker (no SP / OVA / special arcs) may apply unattended.
    - anything else ("off"): never.
    Per-channel policies (L4) override the level per item: a channel
    set to "off" or an item in an "ask" scope blocks the whole plan's
    auto-apply. The gate has already verified every item's identity
    and numbering; while the specials policy is on guard it also
    refuses SP->season mappings, so a special can only ever
    auto-apply as a verified season-0 special under "all", never as
    a disguised season."""
    if not items:
        return False
    for it in items:
        od = originals.get(it.get("key")) or {}
        policy = (policies or {}).get(str(od.get("chat_id")))
        eff = _effective_auto_level(level, policy, od)
        if eff == "all":
            continue
        if eff != "safe":
            return False
        bits = name_bits(od.get("caption") or "", od.get("file_name") or "")
        if bits.get("special") is not None:
            return False
    return True


async def _run_auto_apply(proposal_id: str, level: str) -> None:
    """Background task: apply one gate-passed proposal under the
    automation permission and record the outcome on the proposal, so
    the control room (and the bot's next status check) shows it."""
    coll = _proposals_coll()
    try:
        res = await apply_proposal(proposal_id, approver=f"auto:{level}")
        if res.get("dry_run"):
            state = "dry_run"
        elif res.get("ok"):
            state = "applied"
        else:
            state = "failed"
        result = {"applied": res.get("applied"), "status": res.get("status"),
                  "message": res.get("message") or ""}
    except Exception as e:
        state, result = "failed", {"message": str(e)}
        LOGGER.warning(f"[Librarian] auto-apply {proposal_id} failed: {e}")
    if coll is not None:
        try:
            await coll.update_one({"_id": proposal_id}, {"$set": {
                "auto_apply_state": state, "auto_apply_result": result}})
        except Exception:
            pass


async def submit_proposal(payload: dict) -> dict:
    """Store a Media Manager fix plan and run the verification gate on it.
    Only gate-passing proposals become 'pending' (presentable to Yan)."""
    from Backend.helper.settings_manager import SettingsManager
    if not SettingsManager.current().librarian_enabled:
        return {"ok": False, "message": "Librarian is disabled in Settings."}
    state = bot_state()
    if state != "active":
        return {"ok": False,
                "message": f"The bot is {state} from the Librarian page "
                           "— proposals are not accepted right now."}
    coll = _proposals_coll()
    if coll is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    items = payload.get("items") or []
    if not items or len(items) > 200:
        return {"ok": False, "message": "A proposal needs 1-200 items."}
    channel = _full_chat(payload.get("channel"))
    if channel is None:
        return {"ok": False, "message": "Bad channel id."}
    policy = resolve_channel_policy(_policies_stored(), channel)
    ledger = _ledger_coll()
    # Pairing guard: items may reference a packet by NUMBER. The server
    # resolves the number to the file itself, and the bot must echo the
    # original caption it saw (seen_caption) — if the echo does not match
    # the server's record for that file, the bot is looking at a different
    # item than it thinks, and the whole proposal is rejected.
    packet_id = payload.get("packet_id")
    packet_items: dict[int, dict] = {}
    if packet_id:
        pcoll = _packets_coll()
        pdoc = await pcoll.find_one({"_id": packet_id}) if pcoll is not None else None
        if pdoc is None:
            return {"ok": False, "message": f"Unknown packet: {packet_id}"}
        packet_items = {p["n"]: p for p in pdoc.get("items") or []}
    clean_items = []
    for it in items:
        caption = (it.get("new_caption") or "").strip()
        if not caption:
            return {"ok": False, "message": "Every item needs a new_caption."}
        key = it.get("key")
        snapshot = None
        if it.get("n") is not None:
            entry = packet_items.get(int(it["n"]))
            if entry is None:
                return {"ok": False,
                        "message": f"Item number {it.get('n')} is not in packet "
                                   f"{packet_id} — pull a fresh packet."}
            key = entry["key"]
            snapshot = entry
        if not key:
            return {"ok": False,
                    "message": "Every item needs a packet number (n) or a key."}
        if not _plan_stamp_ok(caption):
            return {"ok": False,
                    "message": f"Item {it.get('n', key)}: the fix caption carries "
                               "no ID link — the app will not re-index without one."}
        doc = await ledger.find_one({"_id": key}) if ledger is not None else None
        if doc is None:
            return {"ok": False, "message": f"Unknown ledger item: {key}"}
        if _policy_denied(policy, doc):
            return {"ok": False,
                    "message": f"Item {it.get('n', key)}: Yan's channel "
                               f"policy denies the {_item_scope(doc)} "
                               "scope for this channel — those files are "
                               "not yours to fix. Pull a different packet."}
        seen = it.get("seen_caption")
        if packet_id and seen is None:
            return {"ok": False,
                    "message": f"Item {it.get('n')}: seen_caption is required — "
                               "echo the original caption you saw for this file."}
        if seen is not None:
            expected = (snapshot or {}).get("caption") or doc.get("caption") or ""
            if _norm_caption(seen) != _norm_caption(expected):
                return {"ok": False,
                        "message": f"Item {it.get('n', key)}: the caption you echoed "
                                   "does not match the server's record for this "
                                   "file — the plan points at a different file "
                                   "than you think. Re-pull the packet and rebuild."}
        clean_items.append({
            "key": key,
            "n": it.get("n"),
            "seen_caption": seen,
            "new_caption": caption,
            "media_type": it.get("media_type") or "tv",
            "tmdb_id": it.get("tmdb_id"),
            "imdb_id": it.get("imdb_id"),
            "year": it.get("year"),
            "season": it.get("season"),
            "episode": it.get("episode"),
            "absolute": bool(it.get("absolute")),
        })
    pid = f"p{int(time.time())}_{abs(hash((channel, len(clean_items)))) % 99999}"
    doc = {
        "_id": pid, "channel": channel, "packet_id": packet_id,
        "label": payload.get("label") or "Untitled run",
        "note": payload.get("note") or "",
        "items": clean_items, "status": "gating",
        "created_at": time.time(), "gate": None,
    }
    await coll.insert_one(doc)
    if ledger is not None:
        for it in clean_items:
            await ledger.update_one({"_id": it["key"]},
                                    {"$set": {"proposal_id": pid}})
    gate = await _gate_plan(doc)
    status = "pending" if gate.get("ok") else "gate_failed"
    await coll.update_one({"_id": pid},
                          {"$set": {"gate": gate, "status": status}})
    invalidate_counts()
    if status == "gate_failed" and ledger is not None:
        # A rejected plan must not strand its items: release them back
        # to the pool so a corrected plan can pick them up.
        for it in clean_items:
            await ledger.update_one({"_id": it["key"]},
                                    {"$set": {"proposal_id": None}})
    auto_level = None
    if status == "pending":
        try:
            from Backend.helper.settings_manager import SettingsManager
            level = getattr(SettingsManager.current(),
                            "librarian_auto_apply", "off")
        except Exception:
            level = "off"
        originals: dict[str, dict] = {}
        if ledger is not None:
            for it in clean_items:
                try:
                    od = await ledger.find_one({"_id": it["key"]})
                except Exception:
                    od = None
                if od:
                    originals[it["key"]] = od
        if _auto_apply_eligible(level, clean_items, originals,
                                policies={str(channel): policy}):
            auto_level = level
            await coll.update_one({"_id": pid}, {"$set": {
                "auto_apply": level, "auto_apply_state": "scheduled"}})
            asyncio.create_task(_run_auto_apply(pid, level))
    doc.update({"gate": gate, "status": status})
    return {"ok": True, "proposal_id": pid, "status": status, "gate": gate,
            "auto_apply": auto_level}


async def _gate_plan(doc: dict) -> dict:
    """Dry-check a plan with the app's OWN matcher: for each item, run
    metadata() on the proposed caption (it writes nothing) and require the
    result to be the intended title at the intended numbering."""
    from Backend.helper.metadata.entry import metadata as app_metadata
    from Backend.helper.metadata.common import extract_default_id
    from Backend.helper.pyro import clean_filename
    channel = _norm_chat(doc.get("channel"))
    cache: dict[tuple, object] = {}
    per_item = {}
    all_ok = True
    # v4.1 guards need the ORIGINAL names + the live policy settings.
    try:
        from Backend.helper.settings_manager import SettingsManager
        _settings = SettingsManager.current()
        specials_policy = _settings.librarian_specials_policy
        strict_year = bool(_settings.librarian_gate_strict_year)
    except Exception:
        specials_policy, strict_year = "guard", False
    originals: dict[str, dict] = {}
    _ledger = _ledger_coll()
    if _ledger is not None:
        for it in doc.get("items") or []:
            try:
                od = await _ledger.find_one({"_id": it["key"]})
            except Exception:
                od = None
            if od:
                originals[it["key"]] = od
    for it in doc.get("items") or []:
        key = it["key"]
        first_line = (it["new_caption"].split("\n")[0] or "").strip()
        cache_key = (first_line, it.get("season"), it.get("episode"))
        try:
            if cache_key not in cache:
                cache[cache_key] = await app_metadata(
                    clean_filename(first_line), channel, 0,
                    override_id=extract_default_id(it["new_caption"]))
            res = cache[cache_key]
        except Exception as e:
            res = None
            per_item[key] = {"ok": False, "reason": f"trial failed: {e}"}
            all_ok = False
            continue
        if res is None:
            per_item[key] = {"ok": False,
                             "reason": "the app still cannot match this caption"}
            all_ok = False
            continue
        problems = []
        # Specials guard: an SP-marked original (a special arc, e.g.
        # "BTTH 3YA - SP3 E01") must never be mapped into a regular
        # season — specials live in the provider's season 0 / specials
        # structure. (This exact slip shipped a wrong fix on 2026-11-19.)
        if specials_policy == "guard":
            od = originals.get(key) or {}
            obits = name_bits(od.get("caption") or "", od.get("file_name") or "")
            if obits.get("special") is not None and not it.get("absolute"):
                s = it.get("season")
                if s is not None and int(s) >= 1:
                    problems.append(
                        "the original is an SP-marked special but the plan maps "
                        "it to a regular season — specials must use the "
                        "provider's specials structure (season 0) or be "
                        "flagged for Yan")
        # Strict-year guard (Settings): the resolved year must match the
        # year the plan claims, when both are known.
        if strict_year and it.get("year"):
            res_year = res.get("release_year") or res.get("year")
            try:
                if res_year and int(res_year) != int(it["year"]):
                    problems.append(
                        f"resolves to year {res_year}, not {it['year']}")
            except (TypeError, ValueError):
                pass
        want_tmdb = it.get("tmdb_id")
        want_imdb = it.get("imdb_id")
        if want_tmdb and int(want_tmdb) > 0 and res.get("tmdb_id") != int(want_tmdb):
            problems.append(f"resolves to tmdb {res.get('tmdb_id')}, not {want_tmdb}")
        if want_imdb and res.get("imdb_id") != want_imdb:
            problems.append(f"resolves to {res.get('imdb_id')}, not {want_imdb}")
        if it.get("media_type") == "tv" and it.get("episode") is not None:
            got_ep = res.get("absolute_episode") or res.get("episode_number")
            if got_ep is not None and int(got_ep) != int(it["episode"]):
                problems.append(f"episode resolves to {got_ep}, not {it['episode']}")
            if not it.get("absolute") and it.get("season") is not None:
                got_s = res.get("season_number")
                if got_s is not None and int(got_s) != int(it["season"]):
                    problems.append(f"season resolves to {got_s}, not {it['season']}")
        per_item[key] = {"ok": not problems,
                         "got_title": res.get("title"),
                         "reason": "; ".join(problems)}
        if problems:
            all_ok = False
    return {"ok": all_ok, "items": per_item, "checked_at": time.time()}


async def _bot_api(method: str, params: dict) -> dict:
    """Call the Telegram Bot API as the Media Manager (Hermes) bot."""
    from Backend.helper.settings_manager import SettingsManager
    token = SettingsManager.current().hermes_bot_token
    if not token:
        return {"ok": False, "description": "No Hermes bot token in Settings."}
    import aiohttp
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=params,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                return await resp.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def _verify_indexed(chat_id, msg_id, want_tmdb, want_imdb,
                          tries: int = 25, delay: float = 1.0) -> bool:
    from Backend import db as _db
    stripped = _norm_chat(chat_id)
    for _ in range(tries):
        try:
            existing = await _db.get_media_ids_by_part(stripped, int(msg_id))
        except Exception:
            existing = None
        if existing:
            imdb_id, tmdb_id = existing[0], existing[1]
            if want_tmdb and int(want_tmdb) > 0:
                return int(tmdb_id or 0) == int(want_tmdb)
            if want_imdb:
                return imdb_id == want_imdb
            return True
        await asyncio.sleep(delay)
    return False


async def _delete_skip_copies(item: dict) -> None:
    ids = item.get("skip_msg_ids") or []
    if not ids:
        return
    try:
        from Backend.helper.settings_manager import SettingsManager
        from Backend.helper.task_manager import delete_messages_batch
        skip = SettingsManager.current().skip_channel
        if skip:
            await delete_messages_batch(int(skip), [int(i) for i in ids])
    except Exception as e:
        LOGGER.warning(f"[Librarian] skip-copy cleanup failed: {e}")


async def _teach_alias(channel, item: dict, plan_item: dict) -> None:
    coll = _alias_coll()
    if coll is None:
        return
    bits = name_bits(item.get("caption") or "", item.get("file_name") or "")
    stem = bits.get("stem")
    if not stem:
        return
    try:
        await coll.update_one(
            {"_id": f"{channel}:{stem}"},
            {"$set": {"channel": channel, "stem": stem,
                      "tmdb_id": plan_item.get("tmdb_id"),
                      "imdb_id": plan_item.get("imdb_id"),
                      "media_type": plan_item.get("media_type"),
                      "title": plan_item.get("title") or "",
                      "taught_at": time.time(), "source": "yan-approved"}},
            upsert=True)
    except Exception as e:
        LOGGER.warning(f"[Librarian] alias teach failed: {e}")


async def _edits_today() -> int:
    coll = _audit_coll()
    if coll is None:
        return 0
    day_start = time.time() - (time.time() % 86400)
    try:
        return await coll.count_documents(
            {"ts": {"$gte": day_start}, "action": {"$in": ["edit", "repost"]}})
    except Exception:
        return 0


async def _live_identity_ok(chat_id, msg_id, key, expected_caption) -> tuple:
    """Pre-edit identity check (v4.1): re-read the target post through the
    userbot and confirm it is still the file the plan thinks it is —
    same file_unique_id as the item key, and the caption unchanged since
    the packet was issued. Fails CLOSED: if the check cannot run, the
    item is skipped rather than edited blind."""
    try:
        from Backend.pyrofork import bot as botmod
        userbot = getattr(botmod, "Userbot", None)
    except Exception:
        userbot = None
    if userbot is None:
        return False, "identity check unavailable (no userbot) — skipped for safety"
    try:
        m = await userbot.get_messages(int(chat_id), int(msg_id))
    except Exception as e:
        return False, f"identity check failed: {e}"
    if m is None or getattr(m, "empty", False):
        return False, "target post not found — skipped for safety"
    media = getattr(m, "video", None) or getattr(m, "document", None)
    uid = getattr(media, "file_unique_id", None) if media else None
    if key and not str(key).startswith("tg:"):
        if uid != key:
            return False, ("target post is a DIFFERENT file than the plan "
                           "expects — skipped for safety")
    if expected_caption and _norm_caption(m.caption or "") != _norm_caption(expected_caption):
        return False, ("target post's caption changed since the packet — "
                       "skipped for safety")
    return True, ""


# ---------------------------------------------------------------------------
# Apply engine: bounded-concurrency pipeline.
#
# The v4 loop applied one file at a time and summed every wait serially
# (~8s/file: identity call + edit + a 3s-granularity verify poll + a fixed
# 2s pause). The engine overlaps those stages across files while keeping
# every safety invariant: the identity check still runs immediately before
# each write, writes are paced per channel (Telegram's real ceiling is
# about one edit per second inside one chat) and globally, verification
# still gates skip-copy cleanup and alias teaching, and a FLOOD_WAIT
# pauses the whole pool for the demanded duration instead of hammering.
# Worker count: the librarian_apply_workers setting (1 = serial).
# ---------------------------------------------------------------------------

APPLY_GLOBAL_INTERVAL = 0.34   # ~3 writes/s across all channels
APPLY_CHANNEL_INTERVAL = 1.0   # 1 write/s inside one channel


class _Pacer:
    """Reserves evenly spaced write slots; a flood pause pushes them."""

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._next = 0.0
        self._pause_until = 0.0
        self._lock = asyncio.Lock()

    def pause(self, seconds: float) -> None:
        until = time.monotonic() + max(0.0, float(seconds))
        if until > self._pause_until:
            self._pause_until = until

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                slot = max(self._next, self._pause_until)
                if slot <= now:
                    self._next = now + self.interval
                    return
                delay = slot - now
            await asyncio.sleep(delay)


def _flood_seconds(resp: dict) -> float:
    """Seconds Telegram demands we wait, or 0 when resp is not a flood."""
    try:
        params = resp.get("parameters") or {}
        if params.get("retry_after"):
            return float(params["retry_after"])
    except Exception:
        pass
    m = re.search(r"retry after (\d+)", str(resp.get("description") or ""))
    return float(m.group(1)) if m else 0.0


async def apply_proposal(proposal_id: str, approver: str = "yan") -> dict:
    """Execute an approved, gate-passed proposal: edit originals in place
    (or re-post from the skip pile), verify indexing, clean skip copies,
    audit everything. Honours dry-run and the daily edit budget."""
    from Backend.helper.settings_manager import SettingsManager
    settings = SettingsManager.current()
    if not settings.librarian_enabled:
        return {"ok": False, "message": "Librarian is disabled in Settings."}
    coll = _proposals_coll()
    ledger = _ledger_coll()
    audit = _audit_coll()
    if coll is None or ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    doc = await coll.find_one({"_id": proposal_id})
    if doc is None:
        return {"ok": False, "message": "Unknown proposal."}
    if doc.get("status") not in ("pending", "approved"):
        return {"ok": False, "message": f"Proposal is {doc.get('status')}, not pending."}
    if settings.librarian_dry_run:
        return {"ok": True, "dry_run": True,
                "message": "Dry-run is ON: nothing was edited.",
                "would_apply": len(doc.get("items") or [])}
    budget = int(settings.librarian_edit_budget_per_day or 0)
    used = await _edits_today()
    items = doc.get("items") or []
    if budget <= 0 or used + len(items) > budget:
        return {"ok": False,
                "message": f"Daily edit budget reached ({used}/{budget} used)."}
    located = await _located_map([it["key"] for it in items])
    results: list = [None] * len(items)

    global_pacer = _Pacer(APPLY_GLOBAL_INTERVAL)
    channel_pacers: dict = {}

    def pacer_for(channel) -> _Pacer:
        return channel_pacers.setdefault(str(channel),
                                         _Pacer(APPLY_CHANNEL_INTERVAL))

    async def paced_write(channel, method: str, params: dict) -> dict:
        cp = pacer_for(channel)
        resp = {"ok": False, "description": "no attempt made"}
        for _attempt in range(2):
            await cp.wait()
            await global_pacer.wait()
            resp = await _bot_api(method, params)
            if resp.get("ok"):
                return resp
            flood = _flood_seconds(resp)
            if flood <= 0:
                return resp
            global_pacer.pause(flood)
            cp.pause(flood)
            LOGGER.warning(
                f"[Librarian] FLOOD_WAIT {flood:.0f}s on {method}; "
                "apply pool paused")
        return resp

    async def apply_item(idx: int, it: dict) -> None:
        item = await ledger.find_one({"_id": it["key"]}) or {}
        loc = located.get(it["key"]) or {}
        chat_id = item.get("chat_id") or loc.get("chat_id") or doc.get("channel")
        msg_id = item.get("msg_id") or loc.get("msg_id")
        before = loc.get("caption") or item.get("caption") or ""
        entry = {"key": it["key"], "ok": False}
        try:
            if msg_id:
                entry["action"] = "edit"
                id_ok, id_reason = await _live_identity_ok(
                    chat_id, msg_id, it["key"], before)
                if not id_ok:
                    raise RuntimeError(id_reason)
                resp = await paced_write(chat_id, "editMessageCaption", {
                    "chat_id": int(chat_id), "message_id": int(msg_id),
                    "caption": it["new_caption"], "parse_mode": "HTML"})
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("description") or "edit failed")
                ok = await _verify_indexed(chat_id, msg_id,
                                            it.get("tmdb_id"), it.get("imdb_id"))
                entry["chat_id"], entry["msg_id"] = int(chat_id), int(msg_id)
            else:
                skip_ids = item.get("skip_msg_ids") or []
                if not skip_ids:
                    raise RuntimeError("no original and no skip copy to re-post")
                from Backend.helper.settings_manager import SettingsManager as _SM
                skip_channel = int(_SM.current().skip_channel)
                entry["action"] = "repost"
                id_ok, id_reason = await _live_identity_ok(
                    skip_channel, skip_ids[0], it["key"],
                    item.get("caption") or "")
                if not id_ok:
                    raise RuntimeError(id_reason)
                resp = await paced_write(doc["channel"], "copyMessage", {
                    "chat_id": int(doc["channel"]), "from_chat_id": skip_channel,
                    "message_id": int(skip_ids[0]),
                    "caption": it["new_caption"], "parse_mode": "HTML"})
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("description") or "copy failed")
                new_msg = resp["result"]["message_id"]
                ok = await _verify_indexed(doc["channel"], new_msg,
                                            it.get("tmdb_id"), it.get("imdb_id"))
                entry["chat_id"], entry["msg_id"] = int(doc["channel"]), int(new_msg)
            entry["ok"] = bool(ok)
            if not ok:
                entry["reason"] = "edit landed but indexing could not be verified"
        except Exception as e:
            entry["reason"] = str(e)
        if audit is not None and entry.get("action"):
            await audit.insert_one({
                "ts": time.time(), "proposal_id": proposal_id,
                "key": it["key"], "action": entry["action"],
                "chat_id": entry.get("chat_id"), "msg_id": entry.get("msg_id"),
                "before": before, "after": it["new_caption"],
                "ok": entry["ok"], "approver": approver})
        if entry["ok"]:
            await _delete_skip_copies(item)
            await ledger.update_one({"_id": it["key"]}, {"$set": {
                "status": "fixed", "fixed_at": time.time(),
                "fixed_via": entry.get("action")}})
            await _teach_alias(doc.get("channel"), item, it)
        results[idx] = entry

    queue: asyncio.Queue = asyncio.Queue()
    for _idx, _it in enumerate(items):
        queue.put_nowait((_idx, _it))

    async def worker() -> None:
        while True:
            try:
                idx, it = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await apply_item(idx, it)
            except Exception as e:  # a worker must never die mid-proposal
                results[idx] = {"key": it.get("key"), "ok": False,
                                "reason": f"apply worker error: {e}"}

    workers = max(1, min(len(items) or 1,
                         int(getattr(settings, "librarian_apply_workers", 4) or 4)))
    if items:
        await asyncio.gather(*[worker() for _ in range(workers)])
    results = [r if r is not None else {"key": "", "ok": False,
                                        "reason": "not processed"}
               for r in results]
    # Items that did not land return to the pool: a failed or partial
    # apply must not leave them claimed by a dead proposal forever.
    for r in results:
        if not r["ok"] and r.get("key"):
            try:
                await ledger.update_one({"_id": r["key"]},
                                        {"$set": {"proposal_id": None}})
            except Exception:
                pass
    done = sum(1 for r in results if r["ok"])
    status = "applied" if done == len(results) else ("partial" if done else "failed")
    await coll.update_one({"_id": proposal_id}, {"$set": {
        "status": status, "applied_at": time.time(), "results": results}})
    invalidate_counts()
    return {"ok": True, "status": status, "applied": done,
            "total": len(results), "results": results}


async def rollback_proposal(proposal_id: str) -> dict:
    """Undo an applied proposal from its audit trail: restore the original
    caption and drop the index entry (edit path), or delete the re-posted
    copy (re-post path). Ledger items return to open."""
    coll = _proposals_coll()
    audit = _audit_coll()
    ledger = _ledger_coll()
    if coll is None or audit is None or ledger is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    doc = await coll.find_one({"_id": proposal_id})
    if doc is None or doc.get("status") not in ("applied", "partial"):
        return {"ok": False, "message": "Nothing applied to roll back."}
    from Backend import db as _db
    undone = 0
    async for entry in audit.find({"proposal_id": proposal_id, "ok": True}):
        try:
            if entry.get("action") == "edit":
                await _bot_api("editMessageCaption", {
                    "chat_id": entry["chat_id"], "message_id": entry["msg_id"],
                    "caption": entry.get("before") or "", "parse_mode": "HTML"})
                await _db.remove_media_part(_norm_chat(entry["chat_id"]),
                                            entry["msg_id"])
            elif entry.get("action") == "repost":
                from Backend.helper.task_manager import delete_message
                await delete_message(entry["chat_id"], entry["msg_id"])
                await _db.remove_media_part(_norm_chat(entry["chat_id"]),
                                            entry["msg_id"])
            await ledger.update_one({"_id": entry["key"]}, {"$set": {
                "status": "open", "rolled_back_at": time.time()},
                "$unset": {"proposal_id": ""}})
            undone += 1
        except Exception as e:
            LOGGER.warning(f"[Librarian] rollback step failed: {e}")
        await asyncio.sleep(1)
    await coll.update_one({"_id": proposal_id},
                          {"$set": {"status": "rolled_back"}})
    invalidate_counts()
    return {"ok": True, "rolled_back": undone}


PROPOSAL_EXPIRY_SECONDS = 48 * 3600


async def _expire_proposal(coll, ledger, doc) -> bool:
    """Lazy expiry: a proposal left pending past 48h is stale — the
    files may have changed under it. Expire it and release its items
    so they return to packets. Returns True when it expired the doc."""
    if doc.get("status") != "pending":
        return False
    if time.time() - float(doc.get("created_at") or 0) < PROPOSAL_EXPIRY_SECONDS:
        return False
    try:
        await coll.update_one({"_id": doc["_id"]},
                              {"$set": {"status": "expired"}})
        if ledger is not None:
            for it in doc.get("items") or []:
                await ledger.update_one(
                    {"_id": it.get("key")},
                    {"$set": {"proposal_id": None}})
        invalidate_counts()
        return True
    except Exception:
        return False


async def reject_proposal(proposal_id: str) -> dict:
    """Yan rejecting a pending plan from the page: mark it rejected
    and release its items back to the pool."""
    coll = _proposals_coll()
    if coll is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    doc = await coll.find_one({"_id": proposal_id})
    if doc is None:
        return {"ok": False, "message": "No such proposal."}
    if doc.get("status") != "pending":
        return {"ok": False,
                "message": f"Proposal is {doc.get('status')}, not pending."}
    await coll.update_one({"_id": proposal_id},
                          {"$set": {"status": "rejected"}})
    ledger = _ledger_coll()
    if ledger is not None:
        for it in doc.get("items") or []:
            await ledger.update_one({"_id": it.get("key")},
                                    {"$set": {"proposal_id": None}})
    invalidate_counts()
    return {"ok": True, "message": "Proposal rejected; its files are "
                                   "back in the pool."}


async def list_proposals(limit: int = 20) -> list[dict]:
    coll = _proposals_coll()
    if coll is None:
        return []
    out = []
    ledger = _ledger_coll()
    try:
        cursor = coll.find({}).sort("created_at", -1).limit(max(1, min(50, limit)))
        async for doc in cursor:
            doc = dict(doc)
            if await _expire_proposal(coll, ledger, doc):
                doc["status"] = "expired"
            items = doc.get("items") or []
            doc["item_count"] = len(items)
            # Attach each item's current recorded caption so the page can
            # show before -> after per file.
            gate_items = (doc.get("gate") or {}).get("items") or {}
            detail = []
            for it in items:
                before = it.get("seen_caption") or ""
                if not before and ledger is not None:
                    try:
                        ld = await ledger.find_one({"_id": it.get("key")})
                        before = (ld or {}).get("caption") or ""
                    except Exception:
                        before = ""
                g = gate_items.get(it.get("key")) or {}
                detail.append({"before": before,
                               "after": it.get("new_caption") or "",
                               "n": it.get("n"),
                               "reason": g.get("reason") or "",
                               "got_title": g.get("got_title") or ""})
            doc["items_detail"] = detail
            doc.pop("items", None)
            out.append(_jsonable(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] proposals list failed: {e}")
    return out


async def list_audit(limit: int = 20) -> list[dict]:
    coll = _audit_coll()
    if coll is None:
        return []
    out = []
    try:
        cursor = coll.find({"action": {"$in": ["edit", "repost"]}}).sort(
            "ts", -1).limit(max(1, min(50, limit)))
        async for doc in cursor:
            out.append(_jsonable(dict(doc)))
    except Exception as e:
        LOGGER.warning(f"[Librarian] audit list failed: {e}")
    return out


async def list_aliases(limit: int = 200) -> list[dict]:
    """What the bot has been taught: uploader stem -> resolved title/IDs,
    per channel. Shown in the control room so Yan can audit (and remove)
    learned mappings."""
    coll = _alias_coll()
    if coll is None:
        return []
    out = []
    try:
        cursor = coll.find({}).sort("taught_at", -1).limit(
            max(1, min(500, limit)))
        async for doc in cursor:
            doc = dict(doc)
            out.append(_jsonable({
                "id": doc.get("_id"), "channel": doc.get("channel"),
                "stem": doc.get("stem"), "title": doc.get("title") or "",
                "tmdb_id": doc.get("tmdb_id"), "imdb_id": doc.get("imdb_id"),
                "media_type": doc.get("media_type"),
                "taught_at": doc.get("taught_at"),
            }))
    except Exception as e:
        LOGGER.warning(f"[Librarian] alias list failed: {e}")
    return out


async def delete_alias(alias_id: str) -> dict:
    """Forget one taught alias (control-room action). The bot will have
    to re-identify that stem from evidence next time."""
    coll = _alias_coll()
    if coll is None or not alias_id:
        return {"ok": False, "message": "Alias store unavailable."}
    try:
        res = await coll.delete_one({"_id": alias_id})
        n = getattr(res, "deleted_count", 0) or 0
    except Exception as e:
        return {"ok": False, "message": f"Delete failed: {e}"}
    return {"ok": bool(n), "deleted": n,
            "message": "Alias removed." if n else "Alias not found."}


async def dismiss_item(key: str) -> dict:
    """Yan's won't-fix decision on one waiting file: it leaves every
    future packet and the waiting counts, but stays in the ledger for
    the record."""
    coll = _ledger_coll()
    if coll is None or not key:
        return {"ok": False, "message": "Ledger unavailable."}
    doc = await coll.find_one({"_id": key})
    if doc is None:
        return {"ok": False, "message": "Item not found."}
    if doc.get("status") not in ("open", "unresolved"):
        return {"ok": False,
                "message": f"Item is {doc.get('status')}, not waiting."}
    await coll.update_one({"_id": key}, {"$set": {
        "status": "dismissed", "dismissed_at": time.time(),
        "proposal_id": None}})
    invalidate_counts()
    return {"ok": True,
            "message": "Dismissed — the bot will not offer this file again."}


async def list_flagged(per_reason: int = 8) -> list[dict]:
    """The flagged queue for the control room: waiting items grouped by
    why they failed, biggest group first, a sample of items per group."""
    coll = _ledger_coll()
    if coll is None:
        return []
    docs = []
    try:
        cursor = coll.find(
            {"status": {"$in": ["open", "unresolved"]}}).sort(
            "updated_at", -1).limit(5000)
        async for doc in cursor:
            docs.append(dict(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] flagged list failed: {e}")
        return []
    located = await _located_map([d["_id"] for d in docs])
    groups: dict[str, dict] = {}
    for d in docs:
        code = d.get("reason_code") or "unknown"
        # NB: the sample list is keyed "sample", never "items" — in
        # Jinja, dict.items resolves to the dict METHOD, which crashed
        # the control-room page render (2026-11-19).
        g = groups.setdefault(code, {"reason_code": code, "count": 0,
                                     "sample": []})
        g["count"] += 1
        if len(g["sample"]) < per_reason:
            loc = located.get(d["_id"]) or {}
            g["sample"].append(_jsonable({
                "key": d["_id"],
                "caption": ((loc.get("caption") or d.get("caption") or "")
                            .split("\n")[0]),
                "file_name": loc.get("file_name") or d.get("file_name") or "",
                "reason": d.get("reason") or "",
                "chat_id": d.get("chat_id") or loc.get("chat_id"),
                "status": d.get("status"),
            }))
    return sorted(groups.values(), key=lambda g: -g["count"])


# ---------------------------------------------------------------------
# Page modules: the Librarian page is composed of sections, each one a
# registry entry Yan can turn on or off from the page itself. The
# choice persists in the `librarian_modules` setting. Later phases
# append their sections here (automations, pre-identified, agent).
# ---------------------------------------------------------------------
PAGE_MODULES: list[dict] = [
    {"id": "status", "title": "Status summary", "default": True},
    {"id": "channels", "title": "Channels & scans", "default": True},
    {"id": "proposals", "title": "Proposals", "default": True},
    {"id": "activity", "title": "Recent fixes", "default": True},
    {"id": "automations", "title": "Automations", "default": True},
    {"id": "agent", "title": "Hermes agent", "default": True},
    {"id": "preidentified", "title": "Pre-identified queue", "default": False},
    {"id": "flagged", "title": "Flagged files", "default": False},
    {"id": "aliases", "title": "Learned aliases", "default": False},
]

# Sections rendered as lazily-loaded fragments (below the fold); the
# rest render inline with the page shell.
FRAGMENT_MODULES = ("proposals", "activity", "flagged", "aliases",
                    "automations", "agent", "preidentified")


def enabled_modules(stored=None) -> dict:
    """Merge Yan's stored module choices over the registry defaults.
    Unknown keys are dropped; anything that is not a dict is ignored."""
    out = {m["id"]: bool(m["default"]) for m in PAGE_MODULES}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in out:
                out[key] = bool(value)
    return out


# ---------------------------------------------------------------------
# Per-channel policies + bot state (L4/L6): Yan's permission matrix,
# enforced server-side — never by asking the bot nicely.
# Stored setting librarian_channel_policies:
#   {channel_id: {"auto": inherit|off|safe|all,
#                 "scopes": {"waiting"|"indexed"|"unindexed":
#                            allow|ask|deny}}}
# Packets never offer a denied scope; proposals containing a denied
# scope are refused; auto-apply uses each item's effective level
# (channel override else the global ladder; an "ask" scope never
# auto-applies).
# ---------------------------------------------------------------------
POLICY_SCOPES = ("waiting", "indexed", "unindexed")
POLICY_AUTO_LEVELS = ("inherit", "off", "safe", "all")
POLICY_SCOPE_LEVELS = ("allow", "ask", "deny")


def resolve_channel_policy(stored, channel) -> dict:
    out = {"auto": "inherit",
           "scopes": {s: "allow" for s in POLICY_SCOPES}}
    if isinstance(stored, dict):
        raw = stored.get(str(channel)) or {}
        if isinstance(raw, dict):
            if raw.get("auto") in POLICY_AUTO_LEVELS:
                out["auto"] = raw["auto"]
            scopes = raw.get("scopes") or {}
            if isinstance(scopes, dict):
                for s in POLICY_SCOPES:
                    if scopes.get(s) in POLICY_SCOPE_LEVELS:
                        out["scopes"][s] = scopes[s]
    return out


def _policies_stored() -> dict:
    try:
        from Backend.helper.settings_manager import SettingsManager
        raw = getattr(SettingsManager.current(),
                      "librarian_channel_policies", {}) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _item_scope(doc) -> str:
    ss = (doc or {}).get("scan_status")
    if ss in ("indexed", "unindexed"):
        return ss
    return "waiting"


def _policy_denied(policy: dict, doc) -> bool:
    return policy["scopes"].get(_item_scope(doc)) == "deny"


def bot_state() -> str:
    """active | paused | killed — the agent panel's master control."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        value = str(getattr(SettingsManager.current(),
                            "librarian_bot_state", "active")
                    or "active").strip().lower()
        return value if value in ("active", "paused", "killed") else "active"
    except Exception:
        return "active"


def _activity_coll():
    tracking = _tracking()
    return tracking["bot_activity"] if tracking is not None else None


async def record_bot_activity(action: str) -> None:
    """The bot's heartbeat: every bot-API call stamps last_seen, so
    the agent panel can show whether the Media Manager is alive."""
    coll = _activity_coll()
    if coll is None:
        return
    try:
        await coll.update_one({"_id": "mediamanager"}, {"$set": {
            "last_seen": time.time(), "last_action": action}}, upsert=True)
    except Exception:
        pass


async def bot_activity() -> dict:
    coll = _activity_coll()
    if coll is None:
        return {}
    try:
        return await coll.find_one({"_id": "mediamanager"}) or {}
    except Exception:
        return {}


_COUNTS_CACHE: dict = {"coll": None, "at": 0.0, "data": None}
_COUNTS_TTL = 15.0


def invalidate_counts() -> None:
    """Mark the cached panel counts stale (called by every write path)."""
    _COUNTS_CACHE["at"] = 0.0
    _COUNTS_CACHE["data"] = None


async def panel_counts() -> dict:
    """The control panel's numbers, cached for a few seconds: the page
    and the bot's status both read these, and recomputing them scans
    the whole ledger. Write paths invalidate the cache; the cache is
    also keyed to the ledger collection so tests that swap collections
    never see stale numbers."""
    coll = _ledger_coll()
    now = time.time()
    if (coll is not None and _COUNTS_CACHE["coll"] is coll
            and _COUNTS_CACHE["data"] is not None
            and now - _COUNTS_CACHE["at"] < _COUNTS_TTL):
        return _COUNTS_CACHE["data"]
    out = await _panel_counts_fresh()
    if coll is not None:
        _COUNTS_CACHE.update({"coll": coll, "at": now, "data": out})
    return out


async def _panel_counts_fresh() -> dict:
    """The control panel's numbers: waiting per channel, fixed, proposals,
    edits used today."""
    base = await ledger_counts()
    out = {"available": base.get("available", False),
           "waiting": 0, "fixed": 0, "by_status": base.get("by_status", {}),
           "proposals_pending": 0, "edits_today": await _edits_today(),
           "waiting_by_channel": {}, "fixed_by_channel": {},
           "located_by_channel": {}, "by_reason": {},
           "scan_by_channel": {},
           "dismissed": base.get("by_status", {}).get("dismissed", 0)}
    out["waiting"] = sum(base.get("by_status", {}).get(s, 0)
                         for s in ("open", "unresolved"))
    out["fixed"] = base.get("by_status", {}).get("fixed", 0)
    ledger = _ledger_coll()
    if ledger is not None:
        try:
            keys = []
            async for d in ledger.find(
                    {"status": {"$in": ["open", "unresolved"]}},
                    {"_id": 1, "chat_id": 1, "reason_code": 1}):
                keys.append(d)
            located = await _located_map([d["_id"] for d in keys])
            for d in keys:
                code = d.get("reason_code") or "unknown"
                out["by_reason"][code] = out["by_reason"].get(code, 0) + 1
                loc = located.get(d["_id"]) or {}
                chat = d.get("chat_id") or loc.get("chat_id")
                if chat:
                    k = str(_norm_chat(chat))
                    out["waiting_by_channel"][k] = out["waiting_by_channel"].get(k, 0) + 1
                    if loc:
                        out["located_by_channel"][k] = out["located_by_channel"].get(k, 0) + 1
            fixed_keys = []
            async for d in ledger.find(
                    {"status": "fixed"}, {"_id": 1, "chat_id": 1}):
                fixed_keys.append(d)
            flocated = await _located_map([d["_id"] for d in fixed_keys])
            for d in fixed_keys:
                loc = flocated.get(d["_id"]) or {}
                chat = d.get("chat_id") or loc.get("chat_id")
                if chat:
                    k = str(_norm_chat(chat))
                    out["fixed_by_channel"][k] = out["fixed_by_channel"].get(k, 0) + 1
            async for d in ledger.find(
                    {"scan_status": {"$in": ["indexed", "unindexed"]}},
                    {"chat_id": 1, "scan_chat_id": 1, "scan_status": 1}):
                chat = d.get("scan_chat_id") or d.get("chat_id")
                if chat:
                    k = str(_norm_chat(chat))
                    bucket = out["scan_by_channel"].setdefault(
                        k, {"indexed": 0, "unindexed": 0})
                    bucket[d["scan_status"]] += 1
        except Exception:
            pass
    pcoll = _proposals_coll()
    if pcoll is not None:
        try:
            out["proposals_pending"] = await pcoll.count_documents(
                {"status": "pending"})
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Background actions (page-triggered)
# ---------------------------------------------------------------------------

ACTION_STATE = {
    "running": False, "kind": "", "started_at": None,
    "finished_at": None, "result": None, "error": None,
    "stop_requested": False, "progress": None,
}


def request_stop() -> dict:
    """Cooperative stop for a running background action (the walk checks
    the flag as it goes and exits with whatever it has recorded)."""
    if not ACTION_STATE["running"]:
        return {"status": "error", "message": "Nothing is running."}
    ACTION_STATE["stop_requested"] = True
    return {"status": "success",
            "message": f"Stop requested for {ACTION_STATE['kind']}."}
async def run_backlog_sweep() -> dict:
    """Harvest the skip pile into the ledger: one entry per UNIQUE file
    (all copy message ids recorded), origin marked backlog-sweep. Read
    -only on Telegram — nothing is deleted or moved."""
    from Backend.helper.settings_manager import SettingsManager
    settings = SettingsManager.current()
    skip = settings.skip_channel
    if not skip:
        return {"ok": False, "message": "No skip channel configured."}
    import Backend.pyrofork.bot as botmod
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return {"ok": False, "message": "Userbot is not available."}
    skip_id = int(skip) if str(skip).lstrip("-").isdigit() else skip
    seen: dict[str, dict] = {}
    total = 0
    async for message in userbot.get_chat_history(skip_id):
        total += 1
        fields = _media_fields(message)
        if not fields.get("file_unique_id"):
            continue
        entry = seen.setdefault(fields["file_unique_id"], {"fields": fields, "message": message, "copies": []})
        entry["copies"].append(message.id)
        if total % 200 == 0:
            await asyncio.sleep(0.05)
    recorded = 0
    for unique_id, entry in seen.items():
        message = entry["message"]
        caption = getattr(message, "caption", "") or ""
        reason, parsed = _failure_analysis(caption, entry["fields"]["file_name"])
        hint = ""
        m = re.search(r"@([A-Za-z0-9_]{4,})", caption or "")
        if m:
            hint = f"@{m.group(1)}"
        ok = await record_reject({
            "key": unique_id, "file_unique_id": unique_id,
            "chat_id": None, "msg_id": None, "origin": "backlog-sweep",
            "origin_hint": hint, "caption": caption,
            "file_name": entry["fields"]["file_name"],
            "size": entry["fields"]["size"], "height": entry["fields"]["height"],
            "reason": reason, "reason_code": reason_code_for(reason),
            "parse": parsed, "bits": name_bits(caption, entry["fields"]["file_name"]),
            "skip_msg_id": entry["copies"][0] if entry["copies"] else None,
        })
        if ok:
            recorded += 1
        # Record every copy id on the entry (dedupe evidence + Phase 2 cleanup).
        coll = _ledger_coll()
        if coll is not None and len(entry["copies"]) > 1:
            try:
                await coll.update_one({"_id": unique_id},
                                      {"$addToSet": {"skip_msg_ids": {"$each": entry["copies"]}}})
            except Exception:
                pass
    return {"ok": True, "messages_read": total, "unique_files": len(seen),
            "recorded": recorded}


async def run_suspect_audit() -> dict:
    """DB-only suspect checks over the indexed library: (1) the same
    Telegram message indexed under more than one title; (2) a quality
    slot holding entries whose sizes diverge wildly. Nothing changes —
    suspects are ledger entries for review."""
    from Backend import db
    from Backend.helper.encrypt import decode_string
    dbs = getattr(db, "dbs", None) or {}
    locations: dict[tuple, set] = {}
    slot_sizes: dict[tuple, list] = {}
    for name, handle in dbs.items():
        if name == "tracking" or handle is None:
            continue
        for coll_name, mtype in (("movie", "movie"), ("tv", "tv")):
            try:
                cursor = handle[coll_name].find(
                    {}, {"title": 1, "telegram": 1, "seasons": 1})
                async for doc in cursor:
                    title = doc.get("title") or "?"
                    slots = [(None, None, doc.get("telegram") or [])]
                    for season in doc.get("seasons") or []:
                        for ep in season.get("episodes") or []:
                            slots.append((season.get("season"), ep.get("episode"),
                                          ep.get("telegram") or []))
                    for s_no, e_no, entries in slots:
                        for entry in entries:
                            enc = entry.get("id")
                            if not enc:
                                continue
                            try:
                                decoded = await decode_string(enc)
                            except Exception:
                                continue
                            chat = _norm_chat((decoded or {}).get("chat_id"))
                            msg = (decoded or {}).get("msg_id")
                            if chat is None or msg is None:
                                continue
                            locations.setdefault((chat, msg), set()).add(
                                (mtype, title, s_no, e_no))
                            size = entry.get("size") or ""
                            slot_sizes.setdefault((mtype, title, s_no, e_no,
                                                   entry.get("quality")), []).append(
                                (chat, msg, size))
            except Exception as e:
                LOGGER.warning(f"[Librarian] audit scan failed ({name}.{coll_name}): {e}")

    recorded = 0
    found = {"multi_index": 0, "slot_collision": 0}
    for (chat, msg), titles in locations.items():
        distinct = {(t[0], t[1]) for t in titles}
        if len(distinct) < 2:
            continue
        found["multi_index"] += 1
        names = ", ".join(sorted(t[1] for t in distinct))
        ok = await record_reject({
            "key": f"tg:{chat}:{msg}", "file_unique_id": None,
            "chat_id": chat, "msg_id": msg, "origin": "suspect-audit",
            "caption": "", "file_name": "",
            "reason": f"Indexed under more than one title: {names}",
            "reason_code": "suspect-multi-index",
            "current_match": {"titles": sorted(t[1] for t in distinct)},
            "bits": name_bits("", ""),
        })
        recorded += 1 if ok else 0

    for (mtype, title, s_no, e_no, quality), entries in slot_sizes.items():
        if len(entries) < 2:
            continue
        def _bytes(size_str):
            try:
                return float(str(size_str).split()[0]) * {
                    "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
                }.get(str(size_str).split()[-1].upper(), 1)
            except Exception:
                return 0
        sizes = [_bytes(s) for _, _, s in entries]
        sizes = [s for s in sizes if s > 0]
        if len(sizes) >= 2 and max(sizes) > 2.5 * min(sizes):
            found["slot_collision"] += 1
            chat, msg, _ = entries[0]
            ok = await record_reject({
                "key": f"tg:{chat}:{msg}", "file_unique_id": None,
                "chat_id": chat, "msg_id": msg, "origin": "suspect-audit",
                "caption": "", "file_name": "",
                "reason": (f"Quality slot {quality} for '{title}' "
                           f"S{s_no}E{e_no} holds {len(entries)} files with "
                           f"divergent sizes — possibly a wrong match."),
                "reason_code": "suspect-slot-collision",
                "current_match": {"title": title, "media_type": mtype},
                "bits": name_bits("", ""),
            })
            recorded += 1 if ok else 0
    return {"ok": True, "suspects_recorded": recorded, "found": found,
            "messages_indexed": len(locations)}




async def _run_action(kind: str, channel=None) -> None:
    ACTION_STATE.update({"running": True, "kind": kind,
                         "started_at": time.time(), "finished_at": None,
                         "result": None, "error": None,
                         "stop_requested": False, "progress": None})
    try:
        if kind == "sweep":
            result = await run_backlog_sweep()
        elif kind == "audit":
            result = await run_suspect_audit()
        elif kind == "locator":
            result = await run_locator(channel)
        elif kind == "scan":
            result = await run_channel_scan(channel)
        else:
            result = {"ok": False, "message": f"Unknown action: {kind}"}
        ACTION_STATE["result"] = result
    except Exception as e:
        LOGGER.error(f"[Librarian] action {kind} failed: {e}")
        ACTION_STATE["error"] = str(e)
    finally:
        ACTION_STATE["running"] = False
        ACTION_STATE["finished_at"] = time.time()


def start_action(kind: str, channel=None) -> dict:
    if ACTION_STATE["running"]:
        return {"status": "error", "message": f"Already running: {ACTION_STATE['kind']}."}
    asyncio.get_event_loop().create_task(_run_action(kind, channel))
    return {"status": "success", "message": f"{kind} started."}
