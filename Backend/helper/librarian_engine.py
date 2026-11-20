"""Librarian fix engine — Phase 4: test mode only (DESIGN §8, §9).

Runs §8 steps 1–5 as dry runs: gates, the step-2 persist, the
two-client probe (reads only), the exclusion / drift / caption-ID
checks, and a full before/after record in ``librarian_fixes``.
Nothing here can mutate Telegram: this module contains no Bot API
client and no write call of any kind (security T-1) — the writer
arrives in Phase 5, separately reviewed. The writer token is never
read here (W-8); its only Phase 4 call is the admin action's getMe.

Import discipline mirrors librarian_store / librarian_gateway:
stdlib at top level; Backend modules imported inside functions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

LOGGER = logging.getLogger("librarian")

PROBE_TIMEOUT = 30  # seconds, per client (DESIGN §8 probe)

# §8 Restart reconciliation, verbatim.
RESTART_ERROR = "app restarted mid-operation — outcome unverified"

_WRITE_LOCK = asyncio.Lock()
_engine_ready = False


# ------------------------------------------------------------------
# Secret scrubbing (security W-5 point 4)
# ------------------------------------------------------------------

def scrub_secrets(text):
    """Replace the configured writer token's literal value with a
    placeholder. Bot API URLs embed the token, so EVERY error string
    Phase 4 code persists, returns, or logs passes through here
    first. Returns None for None."""
    if text is None:
        return None
    out = str(text)
    try:
        from Backend.helper.settings_manager import SettingsManager

        token = SettingsManager.current().librarian_writer_token
    except Exception:
        token = ""
    if token:
        out = out.replace(token, "[writer-token]")
    return out


# ------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------

_HREF_RE = re.compile(r'<a\s[^>]*href="([^"]*)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")


def canonical_from_html(html: str) -> str:
    """The canonical text form of an HTML caption, derived
    mechanically: every ``<a href>`` URL in document order, all tags
    stripped, text + URLs joined with newlines — the string-space
    mirror of ``caption_with_links``' message-space semantics
    (metadata/common.py). Mechanical only: it re-judges nothing."""
    if not html:
        return ""
    urls = _HREF_RE.findall(html)
    text = _TAG_RE.sub("", html)
    return "\n".join([text, *urls])


def classify_bot_api_error(status_code: int, body) -> tuple:
    """DESIGN §8 step 6's exception classes as a pure function of a
    Bot API response envelope: returns (outcome, retry_after_seconds)
    with outcome in {"flood_wait", "not_editable", "not_modified",
    "not_found", "edit_failed"}. No I/O, no client, no token — this
    is the classification entry point the Phase 5 writer will call;
    in Phase 4 nothing calls it except tests (DISP-4)."""
    if not isinstance(body, dict):
        body = {}
    if status_code == 429 or body.get("error_code") == 429:
        params = body.get("parameters")
        retry = params.get("retry_after") if isinstance(params, dict) else None
        if isinstance(retry, bool) or not isinstance(retry, (int, float)):
            retry = None  # never invent a number (DISP-4)
        else:
            retry = int(retry)
        return ("flood_wait", retry)
    description = str(body.get("description") or "")
    if "message can't be edited" in description:
        return ("not_editable", None)
    if "message is not modified" in description:
        return ("not_modified", None)
    if "message to edit not found" in description or "MESSAGE_ID_INVALID" in description:
        return ("not_found", None)
    return ("edit_failed", None)


def _norm_channel_id(value) -> str:
    """The §9 normalisation: strip the -100 prefix, both sides."""
    return str(value).replace("-100", "")


def _channel_matches_setting(chat_id, setting_value) -> bool:
    """Numeric comparison against a channel setting (skip /
    announcement), through the same normalisation is_skip_channel
    uses. A username-form setting cannot match an int chat id here —
    it simply does not match, as in the helper's numeric branch."""
    ref = str(setting_value or "").strip()
    if not ref:
        return False
    norm = ref.replace("-100", "").lstrip("@")
    if not norm.isdigit():
        return False
    return norm == _norm_channel_id(chat_id)


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_iso(value):
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _as_utc(datetime.fromisoformat(text))


# ------------------------------------------------------------------
# Probe (step 3) — reads only; the engine's own, replicating the
# Phase 3 route probe's shape and discipline (both clients always
# consulted; neither short-circuits the other). Error strings are
# scrubbed after formatting (W-5 point 4) — the route probe's raw
# "Type: message" pattern is never copied unsanitised.
# ------------------------------------------------------------------

async def _probe_client(client, chat_id, msg_id, absent_message):
    if client is None:
        return {"state": "error", "error": absent_message}
    try:
        message = await asyncio.wait_for(client.get_messages(chat_id, msg_id),
                                         timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return {"state": "error", "error": "probe timed out"}
    except Exception as exc:
        return {"state": "error",
                "error": scrub_secrets(f"{type(exc).__name__}: {exc}")}
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
        "message": message,
    }


async def probe(chat_id, msg_id):
    """Both read clients on one post: {userbot: …, bot: …}."""
    import Backend.pyrofork.bot as botmod

    userbot = await _probe_client(botmod.Userbot, chat_id, msg_id,
                                  "userbot not connected")
    bot = await _probe_client(botmod.StreamBot, chat_id, msg_id,
                              "bot client not connected")
    return {"userbot": userbot, "bot": bot}


# ------------------------------------------------------------------
# Result shaping
# ------------------------------------------------------------------

def _refusal(request_id, state, error):
    """A step-1 refusal body (§8 step 1): no operation row exists."""
    return {"request_id": request_id, "state": state,
            "error": scrub_secrets(error)}


def _stored_result(row):
    """A persisted operation's result: the row itself, verbatim
    (T-7). The dry-run classification (would_be_action) is not a
    §6 field and is not persisted, so it is absent from a replayed
    result — never reconstructed from the row's action: the
    recorded action is 'edit' even for a repost-classified dry
    run, so synthesising the classification from it would report
    the one action that was impossible."""
    return dict(row)


async def _finish(request_id, state, *, error, would_be_action=None):
    """Terminal transition through the store's one writer, then the
    persisted row as the result (T-6: results are read back from
    persisted state, not assembled in memory)."""
    from Backend.helper import librarian_store

    await librarian_store.update_fix_state(
        request_id, state, error=scrub_secrets(error))
    row = await librarian_store.get_fix_by_request_id(request_id)
    result = dict(row)
    if would_be_action is not None:
        result["would_be_action"] = would_be_action
    return result


# ------------------------------------------------------------------
# Restart reconciliation (§8; DISP-5)
# ------------------------------------------------------------------

async def reconcile_on_startup() -> int:
    """Terminalise every non-terminal fix row to `escalated` with
    the §8 error verbatim, before the API serves any fix. Flags are
    left exactly as recorded (the store writer cannot change them).
    A dry-run row's ledger row is NOT touched (§9's test-mode
    invariant wins for dry runs — plan C-7); a real row's ledger
    row is escalated with the same reason via the existing
    transition, and a refused transition is logged, never forced."""
    from Backend.helper import librarian_store

    count = 0
    for row in await librarian_store.list_nonterminal_fixes():
        await librarian_store.update_fix_state(
            row["request_id"], "escalated", error=RESTART_ERROR)
        count += 1
        if row.get("dry_run"):
            continue
        src = row.get("source_locator") or {}
        try:
            ledger_row, current = await librarian_store.transition_ledger(
                src.get("chat_id"), src.get("msg_id"), "escalate", RESTART_ERROR)
        except Exception as exc:
            LOGGER.warning(
                f"librarian: reconciliation ledger escalation failed for "
                f"{row['request_id']}: {scrub_secrets(exc)}")
            continue
        if ledger_row is None or current not in ("waiting", "parked"):
            LOGGER.warning(
                f"librarian: reconciliation left ledger row for "
                f"{row['request_id']} untouched (status: {current})")
    return count


async def _ensure_engine_init():
    """Idempotent engine-init guard (belt two of §4.3.6; the router
    startup hook is belt one): reconciliation runs before the first
    fix this process serves."""
    global _engine_ready
    if _engine_ready:
        return
    await reconcile_on_startup()
    _engine_ready = True


# ------------------------------------------------------------------
# The dry-run pipeline (§8 steps 1–5)
# ------------------------------------------------------------------

async def fix_one(payload: dict) -> dict:
    """One validated fix object (§4.5.1) -> one result body: a
    step-1 refusal {request_id, state, error}, a per-item not_found
    (batch transport outcome; the single-fix route renders it 404),
    or the persisted operation row (plus would_be_action on a fresh
    dry_run_complete)."""
    from Backend.helper import librarian_store
    from Backend.helper.settings_manager import SettingsManager

    await _ensure_engine_init()
    request_id = payload["request_id"]
    chat_id = payload["chat_id"]
    msg_id = payload["msg_id"]

    # Idempotency at the engine's door (T-7): a stored operation
    # wins verbatim, even over a different repeated payload.
    existing = await librarian_store.get_fix_by_request_id(request_id)
    if existing is not None:
        return _stored_result(existing)

    settings = SettingsManager.current()

    # Mode gate (T-2) — evaluated before any client resolution; the
    # dry-run path resolves read clients only, inside probe(), after
    # every gate has passed. No real fix is possible in Phase 4:
    # there is no writer (T-1), so test mode off is refused here
    # rather than half-executed.
    if not settings.librarian_test_mode:
        if not settings.librarian_writes_enabled:
            return _refusal(
                request_id, "refused_precondition",
                "test mode is off and librarian_writes_enabled is off — "
                "a real fix needs the writes gate open, and no writer "
                "exists in this build")
        return _refusal(
            request_id, "refused_precondition",
            "test mode is off — real fixes are not available in this "
            "build (they arrive with the Phase 5 writer)")

    ledger = await librarian_store.get_ledger_row(chat_id, msg_id)
    if ledger is None:
        return {"request_id": request_id, "state": "not_found",
                "error": scrub_secrets(f"unknown ledger row: {chat_id}:{msg_id}")}

    #----- Step 1 — permissions and stored-fact checks (§4.3.2).
    #----- Every failure returns with NO operation row created.
    if ledger.get("status") != "waiting":
        return _refusal(
            request_id, "refused_wrong_status",
            f"ledger row is '{ledger.get('status')}', not 'waiting'")
    allowed = {_norm_channel_id(c) for c in settings.librarian_channels}
    if _norm_channel_id(chat_id) not in allowed:
        return _refusal(
            request_id, "refused_precondition",
            "channel is not in librarian_channels")
    if (_channel_matches_setting(chat_id, settings.skip_channel)
            or _channel_matches_setting(chat_id, settings.announcement_channel)):
        return _refusal(
            request_id, "refused_precondition",
            "the skip and announcement channels are never fix targets")
    if settings.librarian_mode == "auto":
        alias = await librarian_store.get_alias(chat_id, ledger.get("stem"))
        record = None
        if alias is not None:
            record = await librarian_store.get_record_by_id(
                alias["series_record_id"])
        if record is None or record.get("status") != "verified":
            return _refusal(
                request_id, "refused_precondition",
                "auto mode requires a verified series record and an "
                "exact (channel, stem) alias for this file")
    if payload.get("series_record_id"):
        from bson import ObjectId

        record = await librarian_store.get_record_by_id(
            ObjectId(payload["series_record_id"]))
        if record is None or record.get("status") == "conflicted":
            return _refusal(
                request_id, "refused_record_conflicted",
                "the referenced series record is missing or conflicted")
        if _parse_iso(payload["series_record_updated_at"]) != _as_utc(
                record.get("updated_at")):
            return _refusal(
                request_id, "refused_record_conflicted",
                "the series record changed since it was read — "
                "re-read it and retry")

    #----- Step 2 — persist before anything else (§4.3.3), under the
    #----- write lock so check-and-create cannot race.
    current_locator = ledger.get("current_locator") or ledger["source_locator"]
    from bson import ObjectId

    row = {
        "request_id": request_id,
        "batch_id": payload.get("batch_id"),
        "ledger_id": ledger["_id"],
        "source_locator": dict(ledger["source_locator"]),
        "current_locator": dict(current_locator),
        "stem": ledger.get("stem"),
        "chat_id": ledger["chat_id"],
        # §4.3.4: the ledger stores captions canonically and holds
        # no HTML form, so at persist the two old forms coincide.
        "old_caption": ledger.get("caption") or "",
        "old_caption_text": ledger.get("caption") or "",
        "new_caption": payload["new_caption"],
        "new_caption_text": canonical_from_html(payload["new_caption"]),
        "expected_ids": dict(payload["expected_ids"]),
        "series_record_id": (ObjectId(payload["series_record_id"])
                             if payload.get("series_record_id") else None),
        "action": "edit",
        "state": "prepared",
        "index_confirmed": False,
        "error": None,
        "budget_counted": False,  # a dry run never reserves budget (§9)
        "dry_run": True,
        "prepared_at": datetime.now(timezone.utc),
    }
    async with _WRITE_LOCK:
        existing = await librarian_store.get_fix_by_request_id(request_id)
        if existing is not None:
            return _stored_result(existing)
        active = await librarian_store.find_active_fix(
            current_locator["chat_id"], current_locator["msg_id"])
        if active is not None:
            return _stored_result(active)
        stored = await librarian_store.insert_fix(row)
        if stored.get("state") != "prepared":
            return _stored_result(stored)

    #----- Step 3 — the two-client live read, then classification.
    result = await probe(current_locator["chat_id"], current_locator["msg_id"])
    userbot, bot = result["userbot"], result["bot"]
    found = [r for r in (userbot, bot) if r["state"] == "found"]
    if not found:
        if userbot["state"] == "missing" and bot["state"] == "missing":
            skip = ledger.get("skip_locator")
            if skip:
                skip_probe = await probe(skip["chat_id"], skip["msg_id"])
                if any(r["state"] == "found" for r in skip_probe.values()):
                    # Missing original, surviving skip copy: the real
                    # path reposts from it (§8 step 3); the drift
                    # check is waived on this branch by design.
                    return await _finish(request_id, "dry_run_complete",
                                         error=None, would_be_action="repost")
                skip_word = ("missing" if all(r["state"] == "missing"
                                              for r in skip_probe.values())
                             else "error")
            else:
                skip_word = "none"
            # Ghost. The ledger row is NOT escalated in test mode
            # (§9 invariant); the row's error is the evidence record.
            return await _finish(
                request_id, "refused_missing_post",
                error=f"userbot: missing; bot: missing; skip: {skip_word}")
        # An error is not proof of deletion — never a ghost (§8 step 3).
        err = userbot["error"] if userbot["state"] == "error" else bot["error"]
        return await _finish(request_id, "edit_failed", error=err)

    #----- Steps 4–5 on a found read. Pin (§4.3.5): compare each
    #----- found read's canonical caption against expected_caption;
    #----- proceed on a read that matches, and run the exclusion
    #----- check on that read; if neither matches, it is drift.
    from Backend.helper.metadata.common import (caption_with_links,
                                                extract_default_id)
    from Backend.helper.subtitles import is_subtitle_file

    chosen = None
    for read in found:  # userbot first — the probe's first client
        if caption_with_links(read["message"]) == payload["expected_caption"]:
            chosen = read
            break
    if chosen is None:
        return await _finish(
            request_id, "refused_drift",
            error="the live caption no longer matches expected_caption")

    media = (getattr(chosen["message"], "video", None)
             or getattr(chosen["message"], "document", None))
    file_name = getattr(media, "file_name", None) or ""
    if is_subtitle_file(file_name):
        return await _finish(
            request_id, "refused_subtitle",
            error="the target is a subtitle file — subtitles are never fixed")
    from Backend import db

    stripped_chat = int(str(current_locator["chat_id"]).replace("-100", ""))
    ids = await db.get_media_ids_by_part(stripped_chat,
                                         current_locator["msg_id"])
    if ids:
        try:
            tmdb_id = int(ids[1]) if ids[1] is not None else None
        except (TypeError, ValueError):
            tmdb_id = None
        if tmdb_id is not None and tmdb_id < 0:
            return await _finish(
                request_id, "refused_personal",
                error="the target is filed as personal media "
                      "(negative TMDB id) — never fixed")

    expected_ids = payload["expected_ids"]
    expected_default = (expected_ids.get("imdb")
                        or str(expected_ids["tmdb"]))
    if extract_default_id(payload["new_caption"]) != expected_default:
        return await _finish(
            request_id, "refused_caption_id_mismatch",
            error="new_caption does not carry the expected default ID")

    return await _finish(request_id, "dry_run_complete", error=None,
                         would_be_action="edit")


async def fix_batch(payload: dict) -> dict:
    """{batch_id, serial, items} -> {batch_id, serial, results}.
    Serial execution in request order, whatever the flag says
    (T-10): §8's concurrency machinery arrives with the writer in
    Phase 5. A Phase 4 batch has no early-termination cause, so
    every item reaches a terminal state or a step-1 refusal."""
    await _ensure_engine_init()
    results = []
    for item in payload["items"]:
        item = dict(item)
        item["batch_id"] = payload["batch_id"]
        results.append(await fix_one(item))
    return {"batch_id": payload["batch_id"],
            "serial": bool(payload.get("serial", True)),
            "results": results}
