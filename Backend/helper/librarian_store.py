"""Librarian store — data access for the Librarian's collections.

Implements DESIGN §5 (rejection capture) and the §6 data model.
Phase 1 builds the capture path plus the index set for all five
collections; the provider gateway (§7), the fix engine (§8), and the
bot API / page (§9, §11) extend this module in their own phases.

All Librarian data lives in the app's existing Mongo ``tracking``
database. No new database, no changes to existing collections.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from Backend.helper.metadata.common import caption_with_links
from Backend.helper.metadata.parse import parse_media_name
from Backend.helper.pyro import clean_filename, get_readable_file_size
from Backend.helper.split_files import strip_part_suffix
from Backend.helper.subtitles import is_subtitle_file

_indexes_ready = False


#----- Database handle (lazy: importing Backend builds app state)
def _tracking():
    from Backend import db

    return db.dbs["tracking"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes() -> None:
    """Create every §6 index, idempotently.

    Called from the capture hook's first enabled invocation (and,
    from Phase 3, the plugin router's module import) before any
    write, so no phase's gate can race ahead of the index it
    depends on.
    """
    global _indexes_ready
    if _indexes_ready:
        return
    tracking = _tracking()

    ledger = tracking["reject_ledger"]
    await ledger.create_index(
        [("source_locator.chat_id", ASCENDING), ("source_locator.msg_id", ASCENDING)],
        unique=True,
    )
    await ledger.create_index([("status", ASCENDING), ("chat_id", ASCENDING)])
    await ledger.create_index([("chat_id", ASCENDING), ("stem", ASCENDING)])

    records = tracking["series_records"]
    for field in ("ids.tmdb", "ids.tvdb", "ids.anilist", "ids.mal", "ids.kitsu", "ids.imdb"):
        await records.create_index([(field, ASCENDING)], sparse=True)
    await records.create_index([("canonical_key", ASCENDING)], unique=True)

    await tracking["alias_map"].create_index(
        [("channel_id", ASCENDING), ("stem", ASCENDING)], unique=True
    )

    fixes = tracking["librarian_fixes"]
    await fixes.create_index([("request_id", ASCENDING)], unique=True)
    await fixes.create_index([("batch_id", ASCENDING)])
    await fixes.create_index([("state", ASCENDING), ("created_at", ASCENDING)])
    await fixes.create_index([("stem", ASCENDING), ("created_at", ASCENDING)])

    cache = tracking["provider_cache"]
    await cache.create_index([("key", ASCENDING)], unique=True)
    await cache.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    _indexes_ready = True


#----- Stem normalisation (§6 — defined once, used by capture, the
#----- alias map, and grouping). Purely mechanical string processing
#----- over the app's own parser vocabulary; it asserts no identity.
_SPECIAL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:SP|OVA|OAD)\s?\d*\b|\bSPECIALS?\b", re.IGNORECASE
)
_SE_TOKEN_RE = re.compile(
    r"\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b|\bSeason\s*\d{1,2}\b", re.IGNORECASE
)
_RES_TOKEN_RE = re.compile(
    r"\b(?:240|360|480|576|720|1080|1440|2160|4320)p\b|\b(?:4K|UHD)\b", re.IGNORECASE
)


def _mechanical_title(base: str) -> str:
    """Fallback title extraction when the parser yields no title:
    strip bracketed segments, season/episode tokens, and resolution
    tokens from the cleaned name (the parser's own vocabulary)."""
    text = clean_filename(base)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = _SE_TOKEN_RE.sub(" ", text)
    text = _RES_TOKEN_RE.sub(" ", text)
    return text


def _stem_and_quality(name_line: str) -> tuple:
    """(stem, quality) for one file-name line, from a single parse.

    The split suffix is stripped first so split parts of one file
    share one stem; the extension is dropped before parsing.
    """
    base = strip_part_suffix(name_line or "")
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base).strip()
    parsed = {}
    try:
        parsed = parse_media_name(base) or {}
    except Exception:
        parsed = {}
    title = parsed.get("title") or _mechanical_title(base)
    stem = re.sub(r"[._\-]+", " ", str(title).lower())
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem, parsed.get("quality")


def compute_stem(name: str) -> str:
    return _stem_and_quality(name)[0]


def detect_special(name: str) -> bool:
    """Mechanical special-token detection (SP / OVA / special
    markers in the file name) — a filing hint, not a judgement."""
    return bool(name) and bool(_SPECIAL_RE.search(name))


#----- §5 capture -------------------------------------------------

async def capture_rejection(message, origin: str) -> Optional[dict]:
    """Write/refresh the ledger row for one funnel rejection.

    Returns the row's source locator, or None when capture is
    disabled or the post is excluded (§2.9 — excluded files are
    absent, never an "excluded" status). Awaited by the funnel
    hook; the funnel isolates any exception this raises.

    Refresh contract (§5, insert-first): ``status``,
    ``current_locator``, ``park_reason``, ``escalation_reason`` and
    ``created_at`` are written on insert only; a refresh updates
    descriptive fields only, so a rescan can never un-park,
    un-escalate, or re-queue a row.
    """
    from Backend.helper.settings_manager import SettingsManager

    if not SettingsManager.current().librarian_capture_enabled:
        return None

    media = message.video or message.document
    if media is None:
        return None

    file_name = getattr(media, "file_name", None) or ""
    # The name both live paths hand the matcher (receiver._extract_fields,
    # ScanManager): the caption when present, else the file name.
    title = message.caption or file_name
    name_line = (title or "").split("\n")[0].strip()

    # Exclusion 1 — subtitles (defence in depth; live paths intercept
    # subtitles before the funnel, §5). The check runs on the
    # message's document file name — exactly the input both live
    # paths pass the helper (receiver.py's subtitle ingest).
    doc_name = (message.document.file_name if message.document else "") or ""
    if is_subtitle_file(doc_name):
        return None

    chat_id = int(message.chat.id)
    msg_id = int(message.id)
    # The DB part-helpers take the stripped integer form (§6).
    stripped_chat = int(str(chat_id).replace("-100", ""))

    # Exclusion 2 — negative TMDB ID, detected through the index, the
    # only place it exists (§5). An unindexed reject returns nothing
    # and capture proceeds.
    from Backend import db

    ids = await db.get_media_ids_by_part(stripped_chat, msg_id)
    if ids:
        # Defensive int coercion, mirroring _is_personal_media in
        # api_routes.py: a stored TMDB ID is an int on every known
        # write path, but a non-int must never crash capture.
        try:
            tmdb_id = int(ids[1]) if ids[1] is not None else None
        except (TypeError, ValueError):
            tmdb_id = None
        if tmdb_id is not None and tmdb_id < 0:
            return None

    await ensure_indexes()

    raw_size = int(getattr(media, "file_size", 0) or 0)
    stem, quality = _stem_and_quality(name_line)
    now = _utcnow()
    source = {"chat_id": chat_id, "msg_id": msg_id}

    # §5's refresh list, exactly: only these fields may change on a
    # re-capture. `caption` (the caption AT REJECTION) and
    # `file_unique_id` are historical record — insert-only, below.
    descriptive = {
        "file_name": file_name or title,
        "size": raw_size,
        "origin": origin,
        "stem": stem,
        "special_flag": detect_special(name_line),
        "identity": {
            "quality": quality or None,
            "norm_name": stem,
            "size": get_readable_file_size(raw_size),
        },
        "updated_at": now,
    }
    on_insert = {
        "current_locator": source,
        "skip_locator": None,
        "chat_id": chat_id,
        "status": "waiting",
        "park_reason": None,
        "escalation_reason": None,
        "created_at": now,
        "caption": caption_with_links(message),
        "file_unique_id": getattr(media, "file_unique_id", None),
    }

    ledger = _tracking()["reject_ledger"]
    locator_filter = {
        "source_locator.chat_id": chat_id,
        "source_locator.msg_id": msg_id,
    }
    try:
        await ledger.update_one(
            locator_filter,
            {"$set": descriptive, "$setOnInsert": on_insert},
            upsert=True,
        )
    except DuplicateKeyError:
        # Two concurrent inserts raced the unique locator index;
        # retry once as a plain refresh (§5).
        await ledger.update_one(locator_filter, {"$set": descriptive})
    return source


async def set_skip_locator(chat_id: int, msg_id: int, skip_chat_id: int, skip_msg_id: int) -> None:
    """Fill in the skip-copy coordinates after the funnel's copy
    succeeds (§5). A no-op when no row exists (capture disabled or
    the post was excluded); never clears an existing locator."""
    await _tracking()["reject_ledger"].update_one(
        {
            "source_locator.chat_id": int(chat_id),
            "source_locator.msg_id": int(msg_id),
        },
        {
            "$set": {
                "skip_locator": {"chat_id": int(skip_chat_id), "msg_id": int(skip_msg_id)},
                "updated_at": _utcnow(),
            }
        },
    )


#----- Phase 3: bot API / page data access (DESIGN §9, §11) --------
#----- Read/write helpers for the plugin router. Only the motor
#----- surface the Phase 1 functions already use, plus find /
#----- count_documents / insert_one / delete_one — no aggregation
#----- pipelines, so the data-access surface stays small.

from datetime import timedelta

try:
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    _IST = timezone(timedelta(hours=5, minutes=30))


def _ist_day_bounds_utc(now_utc: datetime) -> tuple:
    """(start, end) UTC datetimes bounding today in Asia/Kolkata."""
    local = now_utc.astimezone(_IST)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def count_by_status() -> dict:
    ledger = _tracking()["reject_ledger"]
    out = {}
    for status in ("waiting", "parked", "escalated", "fixed"):
        out[status] = await ledger.count_documents({"status": status})
    return out


async def count_budget_used_today() -> int:
    start, end = _ist_day_bounds_utc(_utcnow())
    return await _tracking()["librarian_fixes"].count_documents(
        {"budget_counted": True, "created_at": {"$gte": start, "$lt": end}}
    )


async def count_operations_today() -> tuple:
    """(total, fixed) non-dry-run fix operations in today IST (§11)."""
    start, end = _ist_day_bounds_utc(_utcnow())
    fixes = _tracking()["librarian_fixes"]
    base = {"dry_run": False, "created_at": {"$gte": start, "$lt": end}}
    total = await fixes.count_documents(base)
    fixed = await fixes.count_documents({**base, "state": "fixed"})
    return total, fixed


_WAITING_SORT = [("chat_id", ASCENDING), ("stem", ASCENDING), ("source_locator.msg_id", ASCENDING)]


async def list_waiting(channel_id=None, stem=None, limit=200) -> list:
    filt = {"status": "waiting"}
    if channel_id is not None:
        filt["chat_id"] = int(channel_id)
    if stem is not None:
        filt["stem"] = stem
    cursor = _tracking()["reject_ledger"].find(filt).sort(_WAITING_SORT)
    if limit is not None:
        cursor = cursor.limit(limit)
    return await cursor.to_list(None)


async def list_waiting_all() -> list:
    return await list_waiting(limit=None)


async def get_ledger_row(chat_id, msg_id) -> Optional[dict]:
    return await _tracking()["reject_ledger"].find_one(
        {"source_locator.chat_id": int(chat_id), "source_locator.msg_id": int(msg_id)}
    )


# §6 ledger transitions reachable by command (fix/undo are the engine's).
_LEDGER_TRANSITIONS = {
    "park": {"from": {"waiting"}, "to": "parked"},
    "unpark": {"from": {"parked", "escalated"}, "to": "waiting"},
    "escalate": {"from": {"waiting", "parked"}, "to": "escalated"},
}


async def transition_ledger(chat_id, msg_id, action, reason=None) -> tuple:
    """Apply one §6.4 transition. Returns (row, current_status):
    (None, None) when no row exists; on an illegal transition the
    row is returned WITHOUT writing, with its current status."""
    row = await get_ledger_row(chat_id, msg_id)
    if row is None:
        return None, None
    current = row.get("status")
    spec = _LEDGER_TRANSITIONS[action]
    if current not in spec["from"]:
        return row, current
    now = _utcnow()
    fields = {"status": spec["to"], "updated_at": now}
    if action == "park":
        fields["park_reason"] = reason
    elif action == "escalate":
        fields["escalation_reason"] = reason
    elif action == "unpark":
        fields["park_reason"] = None
        fields["escalation_reason"] = None
    await _tracking()["reject_ledger"].update_one(
        {"source_locator.chat_id": int(chat_id), "source_locator.msg_id": int(msg_id)},
        {"$set": fields},
    )
    row.update(fields)
    return row, current


async def list_escalated(limit=200) -> list:
    cursor = (
        _tracking()["reject_ledger"]
        .find({"status": "escalated"})
        .sort([("updated_at", -1)])
        .limit(limit)
    )
    return await cursor.to_list(None)


async def list_history(stem=None, today=False, batch_id=None, limit=200) -> list:
    filt = {}
    if stem is not None:
        filt["stem"] = stem
    if batch_id is not None:
        filt["batch_id"] = batch_id
    if today:
        start, end = _ist_day_bounds_utc(_utcnow())
        filt["created_at"] = {"$gte": start, "$lt": end}
    cursor = (
        _tracking()["librarian_fixes"]
        .find(filt)
        .sort([("created_at", -1)])
        .limit(limit)
    )
    return await cursor.to_list(None)


async def get_record_by_id_field(provider, value) -> Optional[dict]:
    return await _tracking()["series_records"].find_one({f"ids.{provider}": value})


async def get_record_by_canonical_key(key) -> Optional[dict]:
    return await _tracking()["series_records"].find_one({"canonical_key": key})


async def get_record_by_id(record_id) -> Optional[dict]:
    return await _tracking()["series_records"].find_one({"_id": record_id})


async def upsert_record(record: dict) -> dict:
    """Upsert by canonical_key (§9): insert sets created_at and
    updated_at; update replaces the supplied fields and refreshes
    updated_at only. Returns the stored row."""
    now = _utcnow()
    fields = {k: v for k, v in record.items() if k not in ("_id", "created_at", "updated_at")}
    fields["updated_at"] = now
    records = _tracking()["series_records"]
    filt = {"canonical_key": record["canonical_key"]}
    try:
        await records.update_one(
            filt,
            {"$set": fields, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        await records.update_one(filt, {"$set": fields})
    return await records.find_one(filt)


async def list_records(limit=100) -> list:
    cursor = (
        _tracking()["series_records"]
        .find({})
        .sort([("updated_at", -1)])
        .limit(limit)
    )
    return await cursor.to_list(None)


async def get_alias(channel_id: int, stem: str) -> Optional[dict]:
    return await _tracking()["alias_map"].find_one(
        {"channel_id": int(channel_id), "stem": stem}
    )


async def set_alias(channel_id: int, stem: str, record_id) -> dict:
    now = _utcnow()
    aliases = _tracking()["alias_map"]
    filt = {"channel_id": int(channel_id), "stem": stem}
    try:
        await aliases.update_one(
            filt,
            {"$set": {"series_record_id": record_id, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        await aliases.update_one(
            filt, {"$set": {"series_record_id": record_id, "updated_at": now}}
        )
    return await aliases.find_one(filt)


async def forget_alias(channel_id: int, stem: str) -> bool:
    result = await _tracking()["alias_map"].delete_one(
        {"channel_id": int(channel_id), "stem": stem}
    )
    return bool(result.deleted_count)


async def list_aliases(channel_id=None) -> list:
    filt = {}
    if channel_id is not None:
        filt["channel_id"] = int(channel_id)
    cursor = (
        _tracking()["alias_map"]
        .find(filt)
        .sort([("channel_id", ASCENDING), ("stem", ASCENDING)])
    )
    return await cursor.to_list(None)
