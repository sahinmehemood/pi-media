"""Librarian plugin router — bot API, pairing-token action, and the
/admin/librarian page (DESIGN §9, §11; Phase 3).

One APIRouter: the token-authed bot API under /api/librarian/*, the
session-authed token generate/rotate action, the page, and its
operator actions. Import discipline (mirrors librarian_store /
librarian_gateway): stdlib + fastapi at top level only; everything
else is imported inside functions, so importing this module builds
no app state. No Telegram writes exist in this phase — probe reads,
everything else is database/settings state.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from Backend.fastapi.security.credentials import require_auth

LOGGER = logging.getLogger("librarian")

PROBE_TIMEOUT = 30  # seconds, per client (DESIGN §8 probe)
MEDIA_TYPES = ("movie", "tv", "anime", "donghua", "cartoon", "drama",
               "ova", "special", "unknown")
PROVIDERS = ("tmdb", "tvdb", "anilist", "mal", "kitsu", "imdb")


async def _ensure_indexes():
    from Backend.helper import librarian_store

    await librarian_store.ensure_indexes()


async def _reconcile_fixes():
    # §8 restart reconciliation (Phase 4, §4.3.6 belt one; the
    # engine's init guard is belt two): terminalise any operation
    # row a killed process left non-terminal, before fixes serve.
    from Backend.helper import librarian_engine

    await librarian_engine.reconcile_on_startup()


router = APIRouter(tags=["librarian"], on_startup=[_ensure_indexes, _reconcile_fixes])
templates = Jinja2Templates(directory="Backend/fastapi/templates")


# ------------------------------------------------------------------
# Serialisation helpers (§6.0: datetimes as isoformat, ObjectIds as
# str, no raw Mongo documents in responses)
# ------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _json(status_code, body):
    return JSONResponse(content=body, status_code=status_code)


def _error(status_code, message):
    return _json(status_code, {"error": message})


def _ledger_item(row):
    identity = row.get("identity") or {}
    return {
        "key": f"{row['source_locator']['chat_id']}:{row['source_locator']['msg_id']}",
        "source_locator": {"chat_id": row["source_locator"]["chat_id"],
                           "msg_id": row["source_locator"]["msg_id"]},
        "current_locator": ({"chat_id": row["current_locator"]["chat_id"],
                             "msg_id": row["current_locator"]["msg_id"]}
                            if row.get("current_locator") else None),
        "skip_locator": ({"chat_id": row["skip_locator"]["chat_id"],
                          "msg_id": row["skip_locator"]["msg_id"]}
                         if row.get("skip_locator") else None),
        "file_unique_id": row.get("file_unique_id"),
        "file_name": row.get("file_name"),
        "size": row.get("size"),
        "caption": row.get("caption"),
        "origin": row.get("origin"),
        "stem": row.get("stem"),
        "special_flag": bool(row.get("special_flag")),
        "identity": {"quality": identity.get("quality"),
                     "norm_name": identity.get("norm_name"),
                     "size": identity.get("size")},
        "status": row.get("status"),
        "park_reason": row.get("park_reason"),
        "escalation_reason": row.get("escalation_reason"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _record_out(row):
    return {
        "_id": str(row.get("_id")),
        "canonical_key": row.get("canonical_key"),
        "ids": dict(row.get("ids") or {}),
        "titles": list(row.get("titles") or []),
        "media_type": row.get("media_type"),
        "numbering": dict(row.get("numbering") or {}),
        "status": row.get("status"),
        "related_entries": [str(x) for x in (row.get("related_entries") or [])],
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _fix_item(row):
    src = row.get("source_locator") or {}
    return {
        "request_id": row.get("request_id"),
        "batch_id": row.get("batch_id"),
        "ledger_key": f"{src.get('chat_id')}:{src.get('msg_id')}",
        "action": row.get("action"),
        "state": row.get("state"),
        "index_confirmed": bool(row.get("index_confirmed")),
        "dry_run": bool(row.get("dry_run")),
        "budget_counted": bool(row.get("budget_counted")),
        "error": row.get("error"),
        "old_caption": row.get("old_caption"),
        "new_caption": row.get("new_caption"),
        "expected_ids": dict(row.get("expected_ids") or {}),
        "series_record_id": (str(row["series_record_id"])
                             if row.get("series_record_id") else None),
        "created_at": _iso(row.get("created_at")),
    }


# ------------------------------------------------------------------
# Channel / key boundary conversion (§6.6)
# ------------------------------------------------------------------

def _channel_to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"invalid channel id: {value!r}")


def _channel_to_str(value):
    return str(int(value))


def _parse_key(key):
    """'<chat_id>:<msg_id>' -> (chat_id, msg_id) ints, else 400."""
    if not isinstance(key, str) or key.count(":") != 1:
        raise HTTPException(status_code=400, detail=f"invalid key: {key!r} (expected chat:msg)")
    chat, _, msg = key.partition(":")
    try:
        return int(chat), int(msg)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid key: {key!r} (expected chat:msg)")


# ------------------------------------------------------------------
# Pairing-token authentication (§4.2) + last-seen stamp (§5)
# ------------------------------------------------------------------

async def _stamp_last_seen():
    """The one narrow update of §5 — never SettingsManager.update().
    A stamp failure is logged and swallowed: it must never fail the
    request it rode in on."""
    try:
        from Backend import db

        await db.dbs["tracking"]["settings"].update_one(
            {"_id": "app_settings"},
            {"$set": {"librarian_agent_last_seen": datetime.now(timezone.utc)}},
        )
    except Exception as exc:
        LOGGER.warning(f"librarian: last-seen stamp failed: {exc}")


async def _require_token(request, *, exempt_disabled=False):
    """The single auth gate for every bot-API route. Returns None on
    success, or the error response. Order is pinned: 401 before 403;
    the stamp runs after successful authentication (including on
    requests that then 403). Nothing is logged about tokens."""
    from Backend.helper.settings_manager import SettingsManager

    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    presented = ""
    if header.startswith("Bearer "):
        presented = header[len("Bearer "):].strip()
    if not presented:
        return _error(401, "missing or invalid token")
    stored = SettingsManager.current().librarian_api_token_hash or ""
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    if not stored or not hmac.compare_digest(digest, stored):
        return _error(401, "missing or invalid token")
    await _stamp_last_seen()
    if not exempt_disabled and not SettingsManager.current().librarian_enabled:
        return _json(403, {"state": "disabled", "error": "librarian is disabled"})
    return None


async def _body_json(request):
    """Parsed JSON object body, or an error response tuple marker.
    Returns (body, error_response); exactly one is not None."""
    try:
        body = await request.json()
    except Exception:
        return None, _error(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return None, _error(400, "request body must be a JSON object")
    return body, None


def _int_param(request, name, required=True, default=None):
    raw = request.query_params.get(name)
    if raw is None:
        if required:
            raise HTTPException(status_code=400, detail=f"missing query parameter: {name}")
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"query parameter {name} must be an integer")


def _limit_param(request, default=200):
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="query parameter limit must be an integer")
    return max(0, value)


# ------------------------------------------------------------------
# status / mode (§6.1)
# ------------------------------------------------------------------

def _switch_fields(settings):
    return {
        "enabled": settings.librarian_enabled,
        "mode": settings.librarian_mode,
        "test_mode": settings.librarian_test_mode,
        "writes_enabled": settings.librarian_writes_enabled,
        "channels": list(settings.librarian_channels),
        "channel_types": dict(settings.librarian_channel_types),
    }


async def _budget(settings):
    from Backend.helper import librarian_store

    used = await librarian_store.count_budget_used_today()
    limit = settings.librarian_edit_budget_per_day
    return {"limit": limit, "used_today": used, "remaining": max(0, limit - used)}


@router.get("/api/librarian/status")
async def librarian_status(request: Request):
    err = await _require_token(request, exempt_disabled=True)
    if err is not None:
        return err
    from Backend.helper import librarian_store
    from Backend.helper.settings_manager import SettingsManager
    import Backend.pyrofork.bot as botmod

    settings = SettingsManager.current()
    body = _switch_fields(settings)
    body["budget"] = await _budget(settings)
    body["counts"] = await librarian_store.count_by_status()
    body["userbot"] = "present" if botmod.Userbot is not None else "absent"
    # W-6 derivation, purely local: present iff a writer token is
    # stored AND the bot id bound to it at set time (via getMe) is
    # stored. Claims nothing about present-day validity at Telegram.
    body["bot"] = ("present" if (settings.librarian_writer_token
                                 and settings.librarian_writer_bot_id)
                   else "absent")
    # Pinned field order: enabled, mode, test_mode, writes_enabled,
    # budget, channels, channel_types, counts, userbot, bot.
    ordered = {
        "enabled": body["enabled"], "mode": body["mode"],
        "test_mode": body["test_mode"], "writes_enabled": body["writes_enabled"],
        "budget": body["budget"], "channels": body["channels"],
        "channel_types": body["channel_types"], "counts": body["counts"],
        "userbot": body["userbot"], "bot": body["bot"],
    }
    return _json(200, ordered)


@router.get("/api/librarian/mode")
async def librarian_mode(request: Request):
    err = await _require_token(request, exempt_disabled=True)
    if err is not None:
        return err
    from Backend.helper.settings_manager import SettingsManager

    settings = SettingsManager.current()
    body = _switch_fields(settings)
    ordered = {
        "enabled": body["enabled"], "mode": body["mode"],
        "test_mode": body["test_mode"], "writes_enabled": body["writes_enabled"],
        "budget": await _budget(settings), "channels": body["channels"],
        "channel_types": body["channel_types"],
    }
    return _json(200, ordered)


# ------------------------------------------------------------------
# Fix / fix-batch (Phase 4, §4.5): validation, then the test-mode
# engine. undo keeps the Phase 3 stub (§4.5.3); scan is real from
# Phase 5 Workstream D (§5.2 of the Phase 5 plan).
# ------------------------------------------------------------------

_ID_FIELD_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TV_FAMILY_TYPES = ("tv", "anime", "donghua", "cartoon", "drama",
                    "ova", "special")


def _valid_id_field(value):
    """Security A-3: non-empty string, ≤ 64 chars, [A-Za-z0-9_-]."""
    return (isinstance(value, str) and bool(value) and len(value) <= 64
            and _ID_FIELD_RE.match(value) is not None)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_numbering(value):
    """{season: int, episode: int} or {absolute: int} — exactly one
    of the two shapes (§8 request contract)."""
    if not isinstance(value, dict):
        return False
    if "absolute" in value:
        return set(value) == {"absolute"} and _is_int(value["absolute"])
    return (set(value) == {"season", "episode"}
            and _is_int(value["season"]) and _is_int(value["episode"]))


def _validate_fix_item(body):
    """§4.5.1 shape rules, enforced before any store access (A-3).
    Returns an error message naming the field, or None."""
    if not isinstance(body, dict):
        return "fix item must be a JSON object"
    if not _valid_id_field(body.get("request_id")):
        return ("request_id must be a non-empty string of at most 64 "
                "characters using [A-Za-z0-9_-]")
    for field in ("chat_id", "msg_id"):
        if not _is_int(body.get(field)):
            return f"{field} must be an integer"
    if not isinstance(body.get("expected_caption"), str):
        return "expected_caption must be a string"
    if not isinstance(body.get("new_caption"), str) or not body["new_caption"]:
        return "new_caption must be a non-empty string"
    ids = body.get("expected_ids")
    if not isinstance(ids, dict) or not ids:
        return "expected_ids must be an object with at least one of tmdb / imdb"
    if set(ids) - {"tmdb", "imdb"}:
        return "expected_ids may carry only tmdb / imdb"
    if "tmdb" in ids and not _is_int(ids["tmdb"]):
        return "expected_ids.tmdb must be an integer"
    if "imdb" in ids and not (isinstance(ids["imdb"], str)
                              and ids["imdb"].startswith("tt")):
        return "expected_ids.imdb must be a tt… string"
    if "tmdb" not in ids and "imdb" not in ids:
        return "expected_ids must carry at least one of tmdb / imdb"
    record_id = body.get("series_record_id")
    updated_at = body.get("series_record_updated_at")
    if (record_id is None) != (updated_at is None):
        return "series_record_id and series_record_updated_at must be sent together"
    if record_id is not None:
        if (not isinstance(record_id, str)
                or not re.fullmatch(r"[0-9a-fA-F]{24}", record_id)):
            return "series_record_id must be a 24-hex ObjectId string"
        try:
            text = str(updated_at).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return "series_record_updated_at must be an ISO 8601 timestamp"
    if body.get("placement") is not None:
        if record_id is None:
            return ("placement requires series_record_id and "
                    "series_record_updated_at")
        if not _valid_numbering(body["placement"]):
            return "placement must be {season, episode} or {absolute}"
    intended = body.get("intended")
    if intended is not None and not _valid_numbering(intended):
        return "intended must be {season, episode} or {absolute}"
    # `intended` is required when the channel's stored type is in
    # the TV family (§4.5.1; the stored type map is the only source,
    # §9). This reads settings, not the store.
    from Backend.helper.settings_manager import SettingsManager

    channel_type = (SettingsManager.current().librarian_channel_types
                    .get(str(body["chat_id"])))
    if channel_type in _TV_FAMILY_TYPES and intended is None:
        return f"intended is required for {channel_type} channels"
    return None


def _fix_result_body(result):
    """Engine result -> response body. A persisted operation goes
    through the _fix_item projection (plus would_be_action when the
    engine reports one); a step-1 refusal / per-item not_found is
    the small {request_id, state, error} shape (§8 step 1)."""
    if "source_locator" in result:
        body = _fix_item(result)
        if result.get("would_be_action") is not None:
            body["would_be_action"] = result["would_be_action"]
        return body
    return {"request_id": result.get("request_id"),
            "state": result.get("state"),
            "error": result.get("error")}


@router.post("/api/librarian/fix")
async def librarian_fix(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    problem = _validate_fix_item(body)
    if problem is not None:
        return _error(400, problem)
    from Backend.helper import librarian_engine

    result = await librarian_engine.fix_one(body)
    if result.get("state") == "not_found":
        return _error(404, result["error"])
    return _json(200, _fix_result_body(result))


@router.post("/api/librarian/fix-batch")
async def librarian_fix_batch(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    if not _valid_id_field(body.get("batch_id")):
        return _error(400, "batch_id must be a non-empty string of at "
                           "most 64 characters using [A-Za-z0-9_-]")
    serial = body.get("serial", True)
    if not isinstance(serial, bool):
        return _error(400, "serial must be a boolean")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return _error(400, "items must be a non-empty list")
    # Validation is atomic (§4.5.2): every item is schema-validated
    # upfront; any failure 400s the whole batch before any persist.
    for item in items:
        problem = _validate_fix_item(item)
        if problem is not None:
            return _error(400, problem)
    from Backend.helper import librarian_engine

    result = await librarian_engine.fix_batch(
        {"batch_id": body["batch_id"], "serial": serial, "items": items})
    return _json(200, {
        "batch_id": result["batch_id"],
        "serial": result["serial"],
        "results": [_fix_result_body(r) for r in result["results"]],
    })


async def _not_implemented(request):
    err = await _require_token(request)
    if err is not None:
        return err
    _body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    return _json(200, {"state": "not_implemented"})


@router.post("/api/librarian/undo")
async def librarian_undo(request: Request):
    return await _not_implemented(request)


@router.post("/api/librarian/scan")
async def librarian_scan(request: Request):
    # DESIGN §9's scan paragraph + Phase 5 plan §5.2: drive
    # ScanManager through the seam-7 accessors, mode="scan", over
    # the librarian_channels allowlist (or one allowlisted
    # --channel). Every operation outcome is HTTP 200; a refusal is
    # a result, not a transport error. The route returns after
    # start() — ScanManager runs its own task; the bot polls
    # scan-status.
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    from Backend.helper.settings_manager import SettingsManager

    settings = SettingsManager.current()
    channel = body.get("channel")
    if channel is not None:
        # An explicit channel is pinned to the allowlist (plan
        # C-6): a scan posts a probe message into the channel, so
        # it is channel work under the bot's permission. This check
        # is deliberately a single comparison — removing it is a
        # one-line change if sign-off overrules the pin.
        allowlist = [str(c).strip() for c in settings.librarian_channels]
        if not isinstance(channel, str) or channel.strip() not in allowlist:
            return _json(200, {"state": "refused_precondition",
                               "error": "channel is not in librarian_channels"})
    if settings.librarian_test_mode:
        # ScanManager's last-message probe posts a visible message
        # into the channel — a Telegram write, which test mode's
        # no-writes rule bars (plan §5.2 item 3).
        return _json(200, {"state": "refused_precondition",
                           "error": "scan posts a probe message into the "
                                    "channel — test mode is on"})
    from Backend.fastapi.routes.api_routes import get_scan_client, get_scan_manager

    client = get_scan_client()
    if client is None:
        return _json(200, {"state": "error",
                           "error": "no Telegram client is connected yet"})
    if channel is not None:
        channels = [channel]
    else:
        channels = list(settings.librarian_channels)
        if not channels:
            # Refused by the route itself, before start (Senior
            # B-3): ScanManager.start with empty channels and
            # non-empty leftover pending RESUMES the pending
            # channels — UI resume semantics this route's
            # "scan the allowlist" contract must never inherit.
            return _json(200, {"state": "refused",
                               "error": "No channels selected."})
    manager = get_scan_manager()
    result = await manager.start(client, channels, mode="scan")
    if result.get("ok"):
        return _json(200, {"state": "started", "scan": manager.get_status()})
    return _json(200, {"state": "refused", "error": result.get("message")})


# ------------------------------------------------------------------
# Reads (§6.2, §6.3, §6.9)
# ------------------------------------------------------------------

def _api(handler):
    """Render HTTPException as the plain {_error} body, so every bot
    route speaks one error shape."""

    @functools.wraps(handler)
    async def wrapper(request, *args, **kwargs):
        try:
            return await handler(request, *args, **kwargs)
        except HTTPException as exc:
            return _error(exc.status_code, str(exc.detail))

    return wrapper


_TOKEN_SXXEXX = re.compile(r"S\d{1,2}E\d{1,3}", re.IGNORECASE)
_TOKEN_NXN = re.compile(r"\d{1,2}x\d{2,3}", re.IGNORECASE)
_TOKEN_EP = re.compile(r"E(?:P)?\s?\d{1,3}", re.IGNORECASE)


def _episode_token(file_name):
    """One raw episode token as it appears in the file name (display
    only, §6.2): the SxxExx match wins when a file carries one."""
    name = file_name or ""
    for pattern in (_TOKEN_SXXEXX, _TOKEN_NXN, _TOKEN_EP):
        m = pattern.search(name)
        if m:
            return m.group(0)
    return None


def _build_groups(rows, alias_by_pair, records_by_id, channel_titles):
    """Shared grouping helper (§6.2/§6.10): groups are consecutive
    (chat_id, stem) runs over rows already sorted by
    (chat_id, stem, msg_id)."""
    groups = []
    by_key = {}
    for row in rows:
        chat_id = row["chat_id"]
        stem = row.get("stem")
        key = (chat_id, stem)
        group = by_key.get(key)
        if group is None:
            record = None
            alias = alias_by_pair.get(key)
            if alias is not None:
                rec = records_by_id.get(str(alias.get("series_record_id")))
                if rec is not None:
                    titles = rec.get("titles") or []
                    record = {
                        "canonical_key": rec.get("canonical_key"),
                        "canonical_title": titles[0] if titles else rec.get("canonical_key"),
                        "status": rec.get("status"),
                    }
            group = {
                "channel_id": _channel_to_str(chat_id),
                "channel_name": channel_titles.get(str(chat_id)) or str(chat_id),
                "stem": stem,
                "count": 0,
                "files": [],
                "episode_tokens": [],
                "tokens_label": "unverified — from file names",
                "record": record,
            }
            by_key[key] = group
            groups.append(group)
        group["files"].append(_ledger_item(row))
        group["count"] += 1
        token = _episode_token(row.get("file_name"))
        if token is not None:
            group["episode_tokens"].append(token)
    return groups


async def _grouping_context(rows):
    """(alias_by_pair, records_by_id, channel_titles) for _build_groups."""
    from Backend.helper import librarian_store
    from Backend.helper.settings_manager import SettingsManager

    aliases = await librarian_store.list_aliases()
    alias_by_pair = {(a["channel_id"], a["stem"]): a for a in aliases}
    records_by_id = {}
    wanted = {str(a.get("series_record_id")) for a in aliases}
    if wanted:
        for rec in await librarian_store.list_records(limit=1000):
            if str(rec.get("_id")) in wanted:
                records_by_id[str(rec["_id"])] = rec
    titles = SettingsManager.current().channel_titles
    return alias_by_pair, records_by_id, titles


@router.get("/api/librarian/waiting")
@_api
async def librarian_waiting(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend.helper import librarian_store

    channel = request.query_params.get("channel")
    channel_id = _channel_to_int(channel) if channel else None
    stem = request.query_params.get("stem")
    grouped = (request.query_params.get("grouped") or "").lower() == "true"
    limit = _limit_param(request, default=200)
    rows = await librarian_store.list_waiting(channel_id=channel_id, stem=stem, limit=limit)
    if grouped:
        alias_by_pair, records_by_id, titles = await _grouping_context(rows)
        groups = _build_groups(rows, alias_by_pair, records_by_id, titles)
        return _json(200, {"groups": groups, "count": sum(g["count"] for g in groups)})
    return _json(200, {"items": [_ledger_item(r) for r in rows], "count": len(rows)})


def _quality_out(entry):
    return {
        "quality": entry.get("quality"),
        "name": entry.get("name"),
        "size": entry.get("size"),
        "parts": [{"chat_id": p.get("chat_id"), "msg_id": p.get("msg_id")}
                  for p in (entry.get("parts") or [])],
    }


def _ids_out(doc):
    ids = {}
    if doc.get("imdb_id"):
        ids["imdb"] = doc["imdb_id"]
    if doc.get("tmdb_id") is not None:
        ids["tmdb"] = doc["tmdb_id"]
    if doc.get("kitsu_id") is not None:
        ids["kitsu"] = doc["kitsu_id"]
    return ids


@router.get("/api/librarian/find")
@_api
async def librarian_find(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend import db

    text = request.query_params.get("text")
    if text is None or not str(text).strip():
        return _error(400, "missing query parameter: text")
    words = str(text).split()
    pattern = ".*".join(re.escape(w) for w in words)
    match = {"$or": [{field: {"$regex": pattern, "$options": "i"}}
                     for field in ("title", "title_english", "original_title")]}
    results = []
    storage_keys = sorted(
        (k for k in db.dbs if k.startswith("storage_")),
        key=lambda k: int(k.split("_", 1)[1]),
    )
    for key in storage_keys:
        db_index = int(key.split("_", 1)[1])
        storage = db.dbs[key]
        for coll_name, media_type in (("movie", "movie"), ("tv", "tv")):
            docs = await storage[coll_name].find(match).to_list(None)
            for doc in docs:
                entry = {
                    "title": doc.get("title"),
                    "year": doc.get("release_year"),
                    "media_type": media_type,
                    "ids": _ids_out(doc),
                    "db_index": db_index,
                }
                if media_type == "movie":
                    entry["qualities"] = [_quality_out(q) for q in (doc.get("telegram") or [])]
                else:
                    entry["seasons"] = [
                        {"season": season.get("season_number"),
                         "episodes": [
                             {"episode": ep.get("episode_number"),
                              "qualities": [_quality_out(q) for q in (ep.get("telegram") or [])]}
                             for ep in (season.get("episodes") or [])]}
                        for season in (doc.get("seasons") or [])]
                results.append(entry)
                if len(results) >= 25:
                    return _json(200, {"results": results})
    return _json(200, {"results": results})


async def _find_indexed(chat_id, msg_id):
    """Filed placement for one post, per plan OQ-2: query the storage
    DBs directly (the DB part-helpers take the stripped chat form)."""
    from Backend import db

    stripped = int(str(chat_id).replace("-100", ""))
    part_match = {"$elemMatch": {"chat_id": stripped, "msg_id": msg_id}}
    part_match_full = {"$elemMatch": {"chat_id": chat_id, "msg_id": msg_id}}

    def _part_hit(entry):
        return any(p.get("chat_id") in (stripped, chat_id) and p.get("msg_id") == msg_id
                   for p in (entry.get("parts") or []))

    storage_keys = sorted(
        (k for k in db.dbs if k.startswith("storage_")),
        key=lambda k: int(k.split("_", 1)[1]),
    )
    for key in storage_keys:
        db_index = int(key.split("_", 1)[1])
        storage = db.dbs[key]
        doc = await storage["movie"].find_one(
            {"$or": [{"telegram.parts": part_match}, {"telegram.parts": part_match_full}]})
        if doc:
            for q in doc.get("telegram") or []:
                if _part_hit(q):
                    return {"ids": {"imdb": doc.get("imdb_id"), "tmdb": doc.get("tmdb_id")},
                            "media_type": "movie", "db_index": db_index,
                            "season": None, "episode": None,
                            "quality": q.get("quality"), "name": q.get("name")}
        doc = await storage["tv"].find_one(
            {"$or": [{"seasons.episodes.telegram.parts": part_match},
                     {"seasons.episodes.telegram.parts": part_match_full}]})
        if doc:
            for season in doc.get("seasons") or []:
                for ep in season.get("episodes") or []:
                    for q in ep.get("telegram") or []:
                        if _part_hit(q):
                            return {"ids": {"imdb": doc.get("imdb_id"),
                                            "tmdb": doc.get("tmdb_id")},
                                    "media_type": "tv", "db_index": db_index,
                                    "season": season.get("season_number"),
                                    "episode": ep.get("episode_number"),
                                    "absolute": ep.get("absolute_episode"),
                                    "quality": q.get("quality"),
                                    "name": q.get("name")}
    return None


@router.get("/api/librarian/file")
@_api
async def librarian_file(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend.helper import librarian_store

    chat = _int_param(request, "chat")
    msg = _int_param(request, "msg")
    row = await librarian_store.get_ledger_row(chat, msg)
    indexed = await _find_indexed(chat, msg)
    return _json(200, {
        "ledger": _ledger_item(row) if row is not None else None,
        "indexed": indexed,
    })


async def _probe_client(client, chat, msg, absent_message):
    if client is None:
        return {"state": "error", "error": absent_message}
    try:
        message = await asyncio.wait_for(client.get_messages(chat, msg),
                                         timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"state": "error", "error": "probe timed out"}
    except Exception as exc:
        return {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
    if message is None:
        return {"state": "missing"}
    media = getattr(message, "video", None) or getattr(message, "document", None)
    if media is None:
        return {"state": "missing"}
    return {
        "state": "found",
        "file_id": getattr(media, "file_id", None),
        "media": {"file_name": getattr(media, "file_name", None),
                  "size": getattr(media, "file_size", None),
                  "mime_type": getattr(media, "mime_type", None)},
    }


@router.get("/api/librarian/probe")
@_api
async def librarian_probe(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    import Backend.pyrofork.bot as botmod

    chat = _int_param(request, "chat")
    msg = _int_param(request, "msg")
    userbot = await _probe_client(botmod.Userbot, chat, msg, "userbot not connected")
    bot = await _probe_client(botmod.StreamBot, chat, msg, "bot client not connected")
    return _json(200, {"userbot": userbot, "bot": bot})


@router.get("/api/librarian/history")
@_api
async def librarian_history(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend.helper import librarian_store

    rows = await librarian_store.list_history(
        stem=request.query_params.get("stem"),
        today=(request.query_params.get("today") or "").lower() == "true",
        batch_id=request.query_params.get("batch"),
        limit=_limit_param(request, default=200),
    )
    return _json(200, {"items": [_fix_item(r) for r in rows], "count": len(rows)})


@router.post("/api/librarian/lookup")
@_api
async def librarian_lookup(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error(400, "name is required")
    media_type = body.get("media_type", "unknown")
    if media_type not in MEDIA_TYPES:
        return _error(400, f"unknown media_type: {media_type!r}")
    from Backend.helper import librarian_gateway

    try:
        result = await librarian_gateway.lookup(name, media_type)
    except ValueError as exc:
        return _error(400, str(exc))
    return _json(200, result)


async def _gateway_ref(request, fn_name):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return _error(400, "ref is required")
    from Backend.helper import librarian_gateway

    try:
        result = await getattr(librarian_gateway, fn_name)(ref)
    except ValueError as exc:
        return _error(400, str(exc))
    return _json(200, result)


@router.post("/api/librarian/series")
@_api
async def librarian_series(request: Request):
    return await _gateway_ref(request, "series")


@router.post("/api/librarian/crossid")
@_api
async def librarian_crossid(request: Request):
    return await _gateway_ref(request, "crossid")


@router.get("/api/librarian/scan-status")
@_api
async def librarian_scan_status(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend.fastapi.routes.api_routes import get_scan_manager

    manager = get_scan_manager()
    state = getattr(manager, "state", {}) or {}
    if not state.get("started_at") and state.get("status") == "idle":
        return _json(200, {"state": "never_run"})
    status = manager.get_status()
    return _json(200, {"state": status["status"], "scan": status})


# ------------------------------------------------------------------
# Writes, DB-only (§6.4, §6.5, §6.6) — then the token action (§4.3)
# ------------------------------------------------------------------

async def _transition(request, action):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    return await _apply_transition(action, body)


async def _apply_transition(action, body):
    """Shared by the bot routes (§6.4) and the page operator actions
    (§6.8) — the same store functions, the same result bodies."""
    from Backend.helper import librarian_store

    key = body.get("key")
    chat_id, msg_id = _parse_key(key)
    reason = body.get("reason")
    if action in ("park", "escalate"):
        if not isinstance(reason, str) or not reason.strip():
            return _error(400, f"{action} requires a non-empty reason")
        reason = reason.strip()
    await librarian_store.ensure_indexes()
    row, current = await librarian_store.transition_ledger(chat_id, msg_id, action, reason)
    if row is None:
        return _error(404, f"unknown ledger row: {key}")
    spec = librarian_store._LEDGER_TRANSITIONS[action]
    if current not in spec["from"]:
        return _json(200, {
            "state": "refused_wrong_status", "key": key, "status": current,
            "error": f"cannot {action} a row in status '{current}'",
        })
    return _json(200, {"state": spec["to"], "key": key, "status": spec["to"]})


@router.post("/api/librarian/park")
@_api
async def librarian_park(request: Request):
    return await _transition(request, "park")


@router.post("/api/librarian/unpark")
@_api
async def librarian_unpark(request: Request):
    return await _transition(request, "unpark")


@router.post("/api/librarian/escalate")
@_api
async def librarian_escalate(request: Request):
    return await _transition(request, "escalate")


#----- Records (§6.5)

async def _resolve_record(ref):
    """Resolve a record reference (provider:id or canonical_key).
    Provider form matches the indexed ids.<provider> field first;
    the canonical_key fallback keeps record-set -> record-get by
    canonical key a true round-trip."""
    from Backend.helper import librarian_store

    if isinstance(ref, str) and ":" in ref:
        prefix, _, value = ref.partition(":")
        if prefix in PROVIDERS:
            if prefix == "imdb":
                found = await librarian_store.get_record_by_id_field("imdb", value)
            else:
                try:
                    found = await librarian_store.get_record_by_id_field(prefix, int(value))
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400,
                                        detail=f"invalid {prefix} id in ref: {ref!r}")
            if found is not None:
                return found
    return await librarian_store.get_record_by_canonical_key(ref)


@router.post("/api/librarian/record-get")
@_api
async def librarian_record_get(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    from Backend.helper import librarian_store

    record = None
    if body.get("ref") is not None:
        record = await _resolve_record(body["ref"])
    elif body.get("channel") is not None and body.get("stem") is not None:
        alias = await librarian_store.get_alias(_channel_to_int(body["channel"]),
                                                str(body["stem"]))
        if alias is not None:
            record = await librarian_store.get_record_by_id(alias["series_record_id"])
    else:
        return _error(400, "record-get requires a ref, or channel + stem")
    if record is None:
        return _json(200, {"found": False})
    return _json(200, {"found": True, "record": _record_out(record)})


def _validate_record_payload(body):
    """§9.5 E2 field rules, enforced server-side. Returns an error
    message naming the field, or None."""
    for forbidden in ("_id", "created_at", "updated_at"):
        if forbidden in body:
            return f"{forbidden} is server-owned and must not be sent"
    canonical_key = body.get("canonical_key")
    if not isinstance(canonical_key, str) or not canonical_key.strip():
        return "canonical_key is required and must be a non-empty string"
    ids = body.get("ids")
    if not isinstance(ids, dict) or not ids:
        return "ids is required and must be an object with at least one entry"
    for provider, value in ids.items():
        if provider not in PROVIDERS:
            return f"ids.{provider} is not a known provider"
        if provider == "imdb":
            if not isinstance(value, str) or not value.startswith("tt"):
                return "ids.imdb must be a tt… string"
        elif isinstance(value, bool) or not isinstance(value, int):
            return f"ids.{provider} must be an integer"
    titles = body.get("titles")
    if (not isinstance(titles, list) or not titles
            or any(not isinstance(t, str) or not t.strip() for t in titles)):
        return "titles is required and must be a non-empty list of strings"
    if body.get("media_type") not in MEDIA_TYPES:
        return "media_type is required and must be one of the known media types"
    numbering = body.get("numbering")
    if not isinstance(numbering, dict):
        return "numbering is required and must be an object"
    if numbering.get("source") not in ("tmdb", "tvdb", "anilist"):
        return "numbering.source must be tmdb, tvdb or anilist"
    if numbering.get("order") not in ("aired", "absolute", "specials"):
        return "numbering.order must be aired, absolute or specials"
    offset = numbering.get("offset")
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
        return "numbering.offset must be an integer or null"
    if not isinstance(numbering.get("note"), str):
        return "numbering.note must be a string"
    status = body.get("status", "verified")
    if status not in ("verified", "conflicted"):
        return "status must be verified or conflicted"
    related = body.get("related_entries", [])
    if (not isinstance(related, list)
            or any(not isinstance(r, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", r)
                   for r in related)):
        return "related_entries must be a list of 24-hex ObjectId strings"
    return None


@router.post("/api/librarian/record-set")
@_api
async def librarian_record_set(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    problem = _validate_record_payload(body)
    if problem is not None:
        return _error(400, problem)
    from bson import ObjectId

    from Backend.helper import librarian_store

    numbering = body["numbering"]
    record = {
        "canonical_key": body["canonical_key"].strip(),
        "ids": dict(body["ids"]),
        "titles": list(body["titles"]),
        "media_type": body["media_type"],
        "numbering": {"source": numbering["source"], "order": numbering["order"],
                      "offset": numbering.get("offset"), "note": numbering["note"]},
        "status": body.get("status", "verified"),
        "related_entries": [ObjectId(r) for r in body.get("related_entries", [])],
    }
    await librarian_store.ensure_indexes()
    stored = await librarian_store.upsert_record(record)
    return _json(200, {"found": True, "record": _record_out(stored)})


#----- Aliases (§6.6)

@router.get("/api/librarian/aliases")
@_api
async def librarian_aliases(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    from Backend.helper import librarian_store

    channel = request.query_params.get("channel")
    channel_id = _channel_to_int(channel) if channel else None
    out = []
    for alias in await librarian_store.list_aliases(channel_id=channel_id):
        record = await librarian_store.get_record_by_id(alias["series_record_id"])
        out.append({
            "channel": _channel_to_str(alias["channel_id"]),
            "stem": alias["stem"],
            "record_id": str(alias["series_record_id"]),
            "canonical_key": record.get("canonical_key") if record else None,
        })
    return _json(200, {"aliases": out})


@router.post("/api/librarian/alias-set")
@_api
async def librarian_alias_set(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    from Backend.helper import librarian_store

    channel = body.get("channel")
    stem = body.get("stem")
    ref = body.get("record")
    if channel is None or not isinstance(stem, str) or not stem.strip() or ref is None:
        return _error(400, "alias-set requires channel, stem and record")
    channel_id = _channel_to_int(channel)
    record = await _resolve_record(ref)
    if record is None:
        return _error(404, f"unknown record: {ref}")
    await librarian_store.ensure_indexes()
    alias = await librarian_store.set_alias(channel_id, stem.strip(), record["_id"])
    return _json(200, {
        "channel": _channel_to_str(alias["channel_id"]),
        "stem": alias["stem"],
        "record_id": str(alias["series_record_id"]),
        "canonical_key": record.get("canonical_key"),
    })


@router.post("/api/librarian/alias-forget")
@_api
async def librarian_alias_forget(request: Request):
    err = await _require_token(request)
    if err is not None:
        return err
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    from Backend.helper import librarian_store

    channel = body.get("channel")
    stem = body.get("stem")
    if channel is None or not isinstance(stem, str) or not stem.strip():
        return _error(400, "alias-forget requires channel and stem")
    channel_id = _channel_to_int(channel)
    forgotten = await librarian_store.forget_alias(channel_id, stem.strip())
    return _json(200, {"forgotten": forgotten,
                       "channel": _channel_to_str(channel_id),
                       "stem": stem.strip()})


#----- Pairing-token generate/rotate (§4.3) — session-authed, the
#----- only Phase 3 code that writes a non-empty token hash, and
#----- the only sanctioned SettingsManager.update() call in the
#----- plugin. The plaintext is returned exactly once, here.

@router.post("/admin/librarian/token/generate")
async def librarian_token_generate(request: Request, _: bool = Depends(require_auth)):
    from Backend import db
    from Backend.helper import librarian_store
    from Backend.helper.settings_manager import SettingsManager

    await librarian_store.ensure_indexes()
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    await SettingsManager.update(db, {"librarian_api_token_hash": token_hash})
    return _json(200, {
        "token": token,
        "message": "Store this token in the Media Manager profile's secrets now — it is shown once and cannot be retrieved again.",
    })


#----- Writer-token set/clear (Phase 4, security W-2/W-3/W-7) —
#----- session-authed like the pairing action above, and the second
#----- sanctioned SettingsManager.update() call in the plugin. The
#----- candidate is validated with Telegram (getMe, via the existing
#----- _resolve_bot_identity) BEFORE anything persists; on success
#----- one update() persists token + fingerprint + bot id + username
#----- atomically. The token is never echoed: Yan supplied it; the
#----- app confirms the identity it resolved to. In Phase 4 the
#----- token makes no engine call (W-8) — the getMe here is the only
#----- call it ever makes. Rotation = set with a new token; in-app
#----- rotation does NOT revoke the old token at Telegram — only
#----- BotFather /revoke does (W-7).

# The tree's own bot-token shape (Backend/helper/own_bots.py TOKEN_RE),
# copied as a constant: own_bots imports Backend.db at module load and
# must not be top-level-imported here.
_WRITER_TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{30,}$")


async def _writer_identity_collides(user_id):
    """(collides, checked): does user_id equal the streaming bot's id
    or a started pool bot's id (§18-D10 — the writer must be the
    distinct Media Manager bot)? `checked` is False when neither
    identity is available at set time; W-3 then skips the sub-check
    and the caller logs that fact — never a silent pass."""
    import Backend.pyrofork.bot as botmod

    ids = set()
    stream = getattr(botmod, "StreamBot", None)
    me = getattr(stream, "me", None) if stream is not None else None
    if me is not None and getattr(me, "id", None):
        ids.add(int(me.id))
    for client in (getattr(botmod, "multi_clients", None) or {}).values():
        cme = getattr(client, "me", None)
        if cme is not None and getattr(cme, "id", None):
            ids.add(int(cme.id))
    return (user_id in ids), bool(ids)


@router.post("/admin/librarian/writer-token")
async def librarian_writer_token_set(request: Request, _: bool = Depends(require_auth)):
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    token = body.get("token")
    if not isinstance(token, str) or not token.strip():
        return _error(400, "a writer token is required — nothing was saved")
    token = token.strip()
    if not _WRITER_TOKEN_RE.match(token):
        return _error(400, "that does not look like a bot token — nothing was saved")
    # Lazy import (the get_scan_manager precedent at the scan-status
    # route): api_routes is a seam module this router never imports
    # at top level. _resolve_bot_identity logs nothing — its URL
    # embeds the token (W-5 point 4).
    from Backend.fastapi.routes.api_routes import _resolve_bot_identity

    identity = await _resolve_bot_identity(token)
    if identity is None:
        return _error(400, "could not verify that token with Telegram — nothing was saved")
    collides, checked = await _writer_identity_collides(identity["user_id"])
    if collides:
        return _error(400, "that token belongs to the streaming bot or a pool bot — "
                           "the writer must be the separate Media Manager bot; nothing was saved")
    if not checked:
        LOGGER.warning("librarian: writer token set without the streaming/pool "
                       "identity check — no client identities were available")
    from Backend import db
    from Backend.helper.settings_manager import SettingsManager

    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    await SettingsManager.update(db, {
        "librarian_writer_token": token,
        "librarian_writer_token_fingerprint": fingerprint,
        "librarian_writer_bot_id": identity["user_id"],
        "librarian_writer_bot_username": identity["username"],
    })
    return _json(200, {"set": True, "username": identity["username"],
                       "fingerprint": fingerprint})


@router.delete("/admin/librarian/writer-token")
async def librarian_writer_token_clear(request: Request, _: bool = Depends(require_auth)):
    from Backend import db
    from Backend.helper.settings_manager import SettingsManager

    await SettingsManager.update(db, {
        "librarian_writer_token": "",
        "librarian_writer_token_fingerprint": "",
        "librarian_writer_bot_id": 0,
        "librarian_writer_bot_username": "",
    })
    return _json(200, {"set": False})


# ------------------------------------------------------------------
# Page operator actions (§6.8) + the /admin/librarian page (§6.10)
# ------------------------------------------------------------------

async def _page_undo(body):
    """The page's per-fix undo (DESIGN §11; phase5-plan §4.4;
    OQ-1b(a) as ratified, Security S5-7). The template's Undo
    control carries the target fix's `request_id` in the page
    action's `key` field (the field every page action uses); the
    undo operation's OWN `request_id` is minted here, server-side,
    with `secrets.token_hex(16)` — at least 16 bytes of entropy,
    persisted verbatim as the undo row's `request_id` by the
    engine, so the minted ID stays auditable in fix history. This
    minting exists ONLY on this session-authenticated operator
    surface: the bot-API undo route never receives an
    app-invented ID (DESIGN §18, DISP-8). Gates are §8 Undo's,
    exactly — enabled (checked by the caller) and test mode off;
    undo deliberately does NOT require the writes flag, the
    allowlist, or budget. Target eligibility (a `fixed`,
    non-dry-run `edit` row) is the engine's call (§4.1); this
    surface only resolves the target row to the post it names —
    the post's `current_locator`, §8's undo target — and passes
    both IDs through."""
    from Backend.helper import librarian_engine, librarian_store
    from Backend.helper.settings_manager import SettingsManager

    if SettingsManager.current().librarian_test_mode:
        return _json(200, {"state": "refused_precondition",
                           "error": "undo mutates Telegram — test mode is on"})
    target_request_id = body.get("key") or body.get("target_request_id")
    if not _valid_id_field(target_request_id):
        return _error(400, "undo requires the target fix's request_id")
    target = await librarian_store.get_fix_by_request_id(target_request_id)
    if target is None:
        return _json(200, {"state": "not_found",
                           "error": f"unknown fix: {target_request_id}"})
    locator = target.get("current_locator") or target.get("source_locator") or {}
    chat_id, msg_id = locator.get("chat_id"), locator.get("msg_id")
    if not _is_int(chat_id) or not _is_int(msg_id):
        return _json(200, {"state": "not_found",
                           "error": "the target fix has no post to undo"})
    request_id = secrets.token_hex(16)
    result = await librarian_engine.undo_one({
        "chat_id": chat_id, "msg_id": msg_id,
        "request_id": request_id,
        "target_request_id": target_request_id,
    })
    return _json(200, _fix_result_body(result))


@router.post("/admin/librarian/action")
@_api
async def librarian_page_action(request: Request, _: bool = Depends(require_auth)):
    body, body_err = await _body_json(request)
    if body_err is not None:
        return body_err
    from Backend.helper.settings_manager import SettingsManager

    action = body.get("action")
    if action not in ("park", "unpark", "escalate", "undo"):
        return _error(400, f"unknown action: {action!r}")
    if not SettingsManager.current().librarian_enabled:
        return _json(403, {"state": "disabled", "error": "librarian is disabled"})
    if action == "undo":
        return await _page_undo(body)
    return await _apply_transition(action, body)


def _paginate_groups(groups, page, per_page=50):
    """Whole-group pagination (§6.10): accumulate whole groups until
    adding the next would exceed per_page files. A single group
    larger than a page is split at per_page files across consecutive
    pages, its header repeated with a (continued) marker (§0.8).
    Returns (page_groups, total_pages)."""
    units = []
    for group in groups:
        if group["count"] <= per_page:
            units.append(group)
            continue
        files = group["files"]
        for start in range(0, len(files), per_page):
            chunk = dict(group)
            chunk["files"] = files[start:start + per_page]
            chunk["count"] = len(chunk["files"])
            if start > 0:
                chunk["stem"] = f"{group['stem']} (continued)"
            units.append(chunk)
    pages = []
    current, current_files = [], 0
    for unit in units:
        if current and current_files + unit["count"] > per_page:
            pages.append(current)
            current, current_files = [], 0
        current.append(unit)
        current_files += unit["count"]
    if current or not pages:
        pages.append(current)
    total = len(pages)
    page = max(1, min(page, total))
    return pages[page - 1], total, page


def _format_last_seen(stamp):
    if stamp is None:
        return None
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if not isinstance(stamp, datetime):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
    except Exception:
        from datetime import timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
    return stamp.astimezone(ist).strftime("%d %b %Y, %H:%M IST")


@router.get("/admin/librarian")
async def librarian_page(request: Request, _: bool = Depends(require_auth)):
    from Backend import db
    from Backend.fastapi.security.credentials import get_current_user
    from Backend.fastapi.themes import get_all_themes, get_theme, theme_key
    from Backend.helper import librarian_store
    from Backend.helper.settings_manager import SettingsManager

    settings = SettingsManager.current()
    try:
        page_no = int(request.query_params.get("page", 1))
    except (TypeError, ValueError):
        page_no = 1

    counts = await librarian_store.count_by_status()
    ops_total, ops_fixed = await librarian_store.count_operations_today()

    # Agent line: read from the settings document directly (§5),
    # never from the in-memory snapshot.
    agent_last_seen = None
    doc = await db.dbs["tracking"]["settings"].find_one({"_id": "app_settings"})
    if doc:
        agent_last_seen = _format_last_seen(doc.get("librarian_agent_last_seen"))

    rows = await librarian_store.list_waiting_all()
    alias_by_pair, records_by_id, titles = await _grouping_context(rows)
    groups = _build_groups(rows, alias_by_pair, records_by_id, titles)
    groups.sort(key=lambda g: (-g["count"], g["stem"] or "", g["channel_id"]))
    page_groups, total_pages, page_no = _paginate_groups(groups, page_no)

    escalated = []
    for row in await librarian_store.list_escalated():
        chat_id = row["chat_id"]
        escalated.append({
            "key": f"{chat_id}:{row['source_locator']['msg_id']}",
            "file_name": row.get("file_name"),
            "channel_id": _channel_to_str(chat_id),
            "channel_name": titles.get(str(chat_id)) or str(chat_id),
            "escalation_reason": row.get("escalation_reason"),
        })

    # Fix history (§11, Phase 4 §4.6): newest 50 fix rows through
    # the _fix_item projection + IST time, extended with the text
    # caption forms and stem from the raw row for the template.
    fix_history = []
    for r in await librarian_store.list_history(limit=50):
        item = _fix_item(r)
        item["time_ist"] = _format_last_seen(r.get("created_at"))
        item["old_caption_text"] = r.get("old_caption_text")
        item["new_caption_text"] = r.get("new_caption_text")
        item["stem"] = r.get("stem")
        fix_history.append(item)

    records = []
    for rec in await librarian_store.list_records(limit=100):
        out = _record_out(rec)
        rec_titles = rec.get("titles") or []
        out["canonical_title"] = rec_titles[0] if rec_titles else rec.get("canonical_key")
        records.append(out)

    aliases = []
    for alias in await librarian_store.list_aliases():
        record = await librarian_store.get_record_by_id(alias["series_record_id"])
        aliases.append({
            "channel": _channel_to_str(alias["channel_id"]),
            "stem": alias["stem"],
            "canonical_key": record.get("canonical_key") if record else None,
        })

    channel_types = settings.librarian_channel_types
    channels = [{
        "channel_id": str(ch),
        "channel_name": titles.get(str(ch)) or str(ch),
        "type": channel_types.get(str(ch)) or "",
    } for ch in settings.librarian_channels]

    theme_name = theme_key(request)
    ctx = {
        "request": request,
        "theme": get_theme(theme_name),
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "current_user": get_current_user(request),
        "counts": counts,
        "ops_today_total": ops_total,
        "ops_today_fixed": ops_fixed,
        "agent_last_seen": agent_last_seen,
        "librarian_enabled": settings.librarian_enabled,
        "groups": page_groups,
        "page": page_no,
        "total_pages": total_pages,
        "escalated": escalated,
        "fix_history": fix_history,
        "records": records,
        "aliases": aliases,
        "channels": channels,
    }
    return templates.TemplateResponse("librarian.html", ctx)
