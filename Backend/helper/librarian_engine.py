"""Librarian fix engine — DESIGN §8, §9.

Runs the §8 fix pipeline. Steps 1–5 (gates, the step-2 persist,
the two-client probe, the exclusion / drift / caption-ID checks)
are shared by both paths. With test mode on, the run stops there
as a dry run (the Phase 4 behaviour, byte-for-byte). With test
mode off and writes enabled, steps 6–9 execute for real (Phase 5
Workstream A): the Bot API writer below (``editMessageCaption``
with the Media Manager writer token from settings), confirmation
through the re-index seam (the ``librarian_reindex`` bridge —
this module never imports ``receiver.py``), the
``edited_unindexed`` auto-restore, and the terminal record. The
repost send of §8 step 7 is NOT built (phase5-plan DISP-2): its
Phase 5 terminal shapes are pinned in ``fix_one``'s step-3 branch
and in step 6's not-editable branch. Undo for edits (§8 Undo)
lives here too (``undo_one``, Phase 5 Workstream C): its own
operation row, the same writer, the §4.2 reconstruction helper
shared with the auto-restore; repost undo is Phase 6, with the
repost send.

Import discipline mirrors librarian_store / librarian_gateway:
stdlib at top level; Backend modules imported inside functions.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from datetime import datetime, timezone

LOGGER = logging.getLogger("librarian")

PROBE_TIMEOUT = 30  # seconds, per client (DESIGN §8 probe)

# The writer's Bot API base (phase5-plan §2.1): a module constant,
# never an env var or a setting — a settings-reachable base would
# be a credential-exfiltration surface for the writer token. Tests
# rebind this name to point the writer at a local server with real
# Bot API envelope shapes; that scratch override never lands.
BOT_API_BASE = "https://api.telegram.org"

EDIT_TIMEOUT = 30  # seconds, Bot API edit call (§8's probe scale)
REINDEX_TIMEOUT = 120  # seconds, seam completion (§8 pinned)

# §8 Restart reconciliation, verbatim.
RESTART_ERROR = "app restarted mid-operation — outcome unverified"

_WRITE_LOCK = asyncio.Lock()
_INIT_LOCK = asyncio.Lock()
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


_IMDB_URL_LINE = re.compile(r"https?://(?:www\.|m\.)?imdb\.com/title/tt\d+/?")
_TMDB_URL_LINE = re.compile(r"https?://(?:www\.)?themoviedb\.org/(?:movie|tv)/\d+/?")
_URL_LINE = re.compile(r"https?://\S+")


def html_from_canonical(canonical: str) -> str:
    """The HTML form of a canonical caption, reconstructed
    mechanically (phase5-plan §4.2; DISP-7) — the inverse of
    ``canonical_from_html`` for the app's caption shapes, mirroring
    the construction ``caption_with_id`` defines (metadata/entry.py):
    text lines are HTML-escaped; a trailing URL line that
    ``build_id_link`` could have produced becomes an anchor labelled
    IMDB / TMDB, dropping the bare label line the canonical text
    carries for it; any other URL line becomes an anchor over its
    own text. Used for every restore of a stored old caption (the
    ``edited_unindexed`` auto-restore here; undo in Workstream C).
    Exactness is judged where DESIGN judges it: the post-restore
    live re-read must equal the stored text in canonical form."""
    import html as _html

    if not canonical:
        return ""
    out = []
    for line in str(canonical).split("\n"):
        stripped = line.strip()
        label = None
        if _IMDB_URL_LINE.fullmatch(stripped):
            label = "IMDB"
        elif _TMDB_URL_LINE.fullmatch(stripped):
            label = "TMDB"
        if label is not None:
            if out and out[-1].strip().upper() in ("IMDB", "TMDB"):
                out.pop()  # the bare label line becomes the anchor text
            out.append(
                f'<a href="{_html.escape(stripped, quote=True)}">{label}</a>')
        elif _URL_LINE.fullmatch(stripped):
            out.append(
                f'<a href="{_html.escape(stripped, quote=True)}">'
                f'{_html.escape(stripped)}</a>')
        else:
            out.append(_html.escape(line, quote=False))
    return "\n".join(out)


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


# ------------------------------------------------------------------
# The writer (§8 step 6; phase5-plan §2.1) — one Bot API call,
# editMessageCaption, with the Media Manager writer token.
# ------------------------------------------------------------------

class BotApiTransportError(Exception):
    """A Bot API call whose outcome is unknown (transport failure
    or timeout) — §8's ambiguous case. Constructed from scrubbed
    text only and never chained from the raw transport exception
    (Security S5-4): the request URL embeds the writer token, and
    an exception chain rendering it would leak the token."""


async def _bot_api_edit_caption(chat_id, msg_id, caption_html) -> tuple:
    """One Bot API editMessageCaption call with the writer token.
    Returns (http_status, parsed_body_dict); a non-JSON or
    non-dict body normalises to {} (classified edit_failed by the
    classifier, except 429, which it detects from the status
    alone). A transport failure raises BotApiTransportError —
    the caller treats it as the §8 ambiguous case, never as a
    failure verdict. No retry, no sleep, no second client, ever
    (§8 FloodWait). The token is read at call time from settings,
    never cached, and never placed in a log line, an exception
    message, or a return value; no aiohttp request/session logging
    is enabled anywhere on this path (S5-4)."""
    import aiohttp

    from Backend.helper.settings_manager import SettingsManager

    token = SettingsManager.current().librarian_writer_token
    url = f"{BOT_API_BASE}/bot{token}/editMessageCaption"
    payload = {"chat_id": chat_id, "message_id": msg_id,
               "caption": caption_html, "parse_mode": "HTML"}
    failure = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=EDIT_TIMEOUT)) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                return status, body
    except Exception as exc:
        # Scrubbed text only, captured here and raised AFTER the
        # except block, so the raised error has no __context__ /
        # __cause__ chain that could render the token-bearing URL.
        failure = scrub_secrets(f"{type(exc).__name__}: {exc}")
    raise BotApiTransportError(failure)


# ------------------------------------------------------------------
# Batch pacing (§8 Concurrency; phase5-plan §2.4) — batch-scoped
# state carried by a context variable, set by fix_batch for the
# duration of each item task. A standalone fix_one call, a dry
# run, and undo (which calls the writer directly, never this
# helper) see no batch state and are never paced.
# ------------------------------------------------------------------

class _BatchRun:
    """One real batch's execution state: the in-flight semaphore
    (4, or 1 when serial), the early-termination flag, and the
    per-channel pacing reservations (chat_id -> monotonic time of
    the last reserved write slot)."""

    def __init__(self, serial):
        self.semaphore = asyncio.Semaphore(1 if serial else 4)
        self.stop = asyncio.Event()
        self.pacing_lock = asyncio.Lock()
        self.last_write = {}


_BATCH_CTX = contextvars.ContextVar("librarian_batch_ctx", default=None)

#: Seconds between two writes to the SAME channel (§8: ≈1/s).
CHANNEL_WRITE_INTERVAL = 1.0


async def _pace_write(batch, chat_id):
    """Reserve this channel's next write slot and sleep out the
    shortfall. The reservation is taken under the pacing lock (so
    concurrent items to one channel can never reserve the same
    slot) but the sleep happens outside it, so a paced channel
    never delays a write to a different channel."""
    async with batch.pacing_lock:
        now = time.monotonic()
        last = batch.last_write.get(chat_id)
        scheduled = now if last is None else max(
            now, last + CHANNEL_WRITE_INTERVAL)
        batch.last_write[chat_id] = scheduled
    delay = scheduled - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


async def _write_caption(chat_id, msg_id, caption_html):
    """The writer call for a fix operation (the step-6 edit and
    the §8 auto-restore): inside a batch, the per-channel pacing
    delay is applied first — before the writer call, inside the
    item's editing window (§2.4). Reads are never routed through
    here, and neither are dry runs (they never write)."""
    batch = _BATCH_CTX.get()
    if batch is not None:
        await _pace_write(batch, chat_id)
    return await _bot_api_edit_caption(chat_id, msg_id, caption_html)


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


async def _finish(request_id, state, *, error, would_be_action=None,
                  index_confirmed=None, extra=None):
    """Terminal transition through the store's one writer, then the
    persisted row as the result (T-6: results are read back from
    persisted state, not assembled in memory). ``extra`` carries
    result-body-only fields that are not §6 row fields (the
    flood_wait ``retry_after`` seconds, §2.2) — merged into the
    returned body, never persisted."""
    from Backend.helper import librarian_store

    await librarian_store.update_fix_state(
        request_id, state, error=scrub_secrets(error),
        index_confirmed=index_confirmed)
    row = await librarian_store.get_fix_by_request_id(request_id)
    result = dict(row)
    if would_be_action is not None:
        result["would_be_action"] = would_be_action
    if extra:
        result.update(extra)
    return result


def _expected_default(expected_ids):
    """The request's expected_ids rendered as the single default
    ID the caption-ID check compares against (§8 step 5): the
    IMDb ID if present, else str(tmdb)."""
    return expected_ids.get("imdb") or str(expected_ids["tmdb"])


def _operation_row(payload, ledger, current_locator, *, real):
    """The step-2 operation row for one fix item, shared by
    fix_one's persist and the batch machinery's aborted records
    so both write the identical §6 shape."""
    from bson import ObjectId

    return {
        "request_id": payload["request_id"],
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
        # A dry run never reserves budget (§9); a real operation's
        # persist IS the reservation (§8 step 2).
        "budget_counted": bool(real),
        "dry_run": not real,
    }


async def _record_aborted(payload, error):
    """Record one unattempted batch item in terminal state
    ``aborted`` (§8 step 2: early batch termination leaves no item
    silently unrecorded). Inserted directly in ``aborted`` via
    insert_fix — never left in ``prepared`` — with
    ``budget_counted=false``. Idempotency and the
    one-active-operation check apply exactly as at persist."""
    from Backend.helper import librarian_store

    request_id = payload["request_id"]
    existing = await librarian_store.get_fix_by_request_id(request_id)
    if existing is not None:
        return _stored_result(existing)
    ledger = await librarian_store.get_ledger_row(
        payload["chat_id"], payload["msg_id"])
    if ledger is None:
        return {"request_id": request_id, "state": "not_found",
                "error": scrub_secrets(
                    f"unknown ledger row: {payload['chat_id']}:{payload['msg_id']}")}
    current_locator = ledger.get("current_locator") or ledger["source_locator"]
    row = _operation_row(payload, ledger, current_locator, real=True)
    row["state"] = "aborted"
    row["budget_counted"] = False
    row["error"] = scrub_secrets(error)
    row["aborted_at"] = datetime.now(timezone.utc)
    async with _WRITE_LOCK:
        existing = await librarian_store.get_fix_by_request_id(request_id)
        if existing is not None:
            return _stored_result(existing)
        active = await librarian_store.find_active_fix(
            current_locator["chat_id"], current_locator["msg_id"])
        if active is not None:
            return _stored_result(active)
        stored = await librarian_store.insert_fix(row)
    return _stored_result(stored)


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
    async with _INIT_LOCK:
        if _engine_ready:
            return
        await reconcile_on_startup()
        _engine_ready = True


# ------------------------------------------------------------------
# Steps 6–9 — the real path (phase5-plan §2.2)
# ------------------------------------------------------------------

def _norm_filed(filed):
    """A seam ``filed`` report normalised to the form it was given
    in: {"season": int, "episode": int} | {"absolute": int} | None."""
    if not isinstance(filed, dict):
        return None
    try:
        if "absolute" in filed:
            return {"absolute": int(filed["absolute"])}
        if "season" in filed and "episode" in filed:
            return {"season": int(filed["season"]),
                    "episode": int(filed["episode"])}
    except (TypeError, ValueError):
        return None
    return None


def _ids_match_expected(ids_triple, expected_ids) -> bool:
    """§8 step 8's ID confirmation: equal on every ID present in
    expected_ids; IDs absent from expected_ids are ignored, never
    required to be absent."""
    if not isinstance(ids_triple, (tuple, list)) or len(ids_triple) < 2:
        return False
    imdb_id, tmdb_id = ids_triple[0], ids_triple[1]
    if "imdb" in expected_ids:
        if imdb_id is None or str(imdb_id) != str(expected_ids["imdb"]):
            return False
    if "tmdb" in expected_ids:
        try:
            if tmdb_id is None or int(tmdb_id) != int(expected_ids["tmdb"]):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _confirm_seam_result(seam, payload):
    """(confirmed, reason) for an indexed/duplicate seam result:
    IDs against expected_ids, and the filed placement against the
    request's ``intended``. The missing-``intended`` pin (plan
    C-2, Senior B-2): if the seam reports a TV filing (a filed
    season/episode or absolute) and the request carried no
    ``intended``, confirmation cannot complete — judged on the
    seam's report, never on a channel type."""
    if not _ids_match_expected(seam.get("ids"), payload["expected_ids"]):
        return False, ("the re-indexed entry's IDs do not match "
                       "the request's expected_ids")
    filed = _norm_filed(seam.get("filed"))
    intended = (_norm_filed(payload["intended"])
                if payload.get("intended") is not None else None)
    if filed is not None and intended is None:
        return False, ("the seam filed a TV entry but the request "
                       "carried no intended filing to confirm it "
                       "against")
    if intended is not None and filed != intended:
        return False, (f"the filed placement {filed} does not match "
                       f"the intended filing {intended}")
    return True, None


async def _caption_is_live(chat_id, msg_id, canonical_text) -> bool:
    """A live re-read (the two-client probe) showing a post whose
    canonical caption equals the given text — the restore /
    ambiguous-edit confirmation form (§8 step 8, Ambiguous)."""
    from Backend.helper.metadata.common import caption_with_links

    result = await probe(chat_id, msg_id)
    return any(read["state"] == "found"
               and caption_with_links(read["message"]) == canonical_text
               for read in result.values())


def _flood_wait_finish(request_id, retry, context):
    """The §8 FloodWait terminal: seconds surfaced in the result
    body and in `error` as plain words; no seconds invented when
    Telegram gave none. The reservation stands (§8 step 9)."""
    if retry is not None:
        error = (f"{context} — Telegram asked for a {retry}-second "
                 f"wait; the write was not retried")
        extra = {"retry_after": retry}
    else:
        error = (f"{context} — Telegram rate-limited the write "
                 f"(HTTP 429, no retry_after given); the write was "
                 f"not retried")
        extra = None
    return _finish(request_id, "flood_wait", error=error, extra=extra)


async def _escalate_restore_failure(request_id, payload, current_locator,
                                    old_caption_text, *, reason, detail):
    """§8 step 8's last resort: the restore itself failed, so the
    operation escalates with both captions and the post's
    coordinates, and the ledger row is escalated with it."""
    from Backend.helper import librarian_store

    chat_id = current_locator["chat_id"]
    msg_id = current_locator["msg_id"]
    error = (f"{reason}; the previous caption could not be restored "
             f"({detail}) — old caption: {old_caption_text!r}; "
             f"new caption: {payload['new_caption']!r}; "
             f"post: {chat_id}:{msg_id}")
    result = await _finish(request_id, "escalated", error=error)
    try:
        ledger_row, current = await librarian_store.transition_ledger(
            payload["chat_id"], payload["msg_id"], "escalate", error)
        if ledger_row is None or current != "waiting":
            LOGGER.warning(
                f"librarian: restore-failure escalation left ledger "
                f"row for {request_id} untouched (status: {current})")
    except Exception as exc:
        LOGGER.warning(
            f"librarian: restore-failure ledger escalation failed "
            f"for {request_id}: {scrub_secrets(exc)}")
    return result


async def _restore(request_id, payload, ledger, current_locator,
                   old_caption_text, *, reason):
    """The ``edited_unindexed`` auto-restore (§8 step 8): re-send
    the OLD caption through the same writer (its HTML form
    reconstructed from the stored canonical text), confirm by a
    live re-read only — the index is not consulted. Part of the
    same operation: no second budget reservation. The ledger row
    never left ``waiting`` on this path (mark_ledger_fixed runs
    only on confirmation), so a confirmed restore simply
    terminates ``edited_unindexed`` with the recovery recorded."""
    chat_id = current_locator["chat_id"]
    msg_id = current_locator["msg_id"]
    old_html = html_from_canonical(old_caption_text)
    try:
        status, body = await _write_caption(chat_id, msg_id, old_html)
    except BotApiTransportError as exc:
        # Ambiguous restore: re-read before concluding anything.
        if await _caption_is_live(chat_id, msg_id, old_caption_text):
            return await _finish(
                request_id, "edited_unindexed",
                error=f"{reason}; previous caption restored")
        return await _escalate_restore_failure(
            request_id, payload, current_locator, old_caption_text,
            reason=reason,
            detail=f"the restore's outcome is ambiguous ({exc}) and "
                   f"a re-read does not show the previous caption")
    if status == 200 and body.get("ok"):
        outcome, retry = "ok", None
    else:
        outcome, retry = classify_bot_api_error(status, body)
    if outcome == "flood_wait":
        return await _flood_wait_finish(
            request_id, retry,
            f"{reason}; the restore of the previous caption was "
            f"rate-limited, so the corrected caption is still live")
    if outcome in ("ok", "not_modified"):
        if await _caption_is_live(chat_id, msg_id, old_caption_text):
            return await _finish(
                request_id, "edited_unindexed",
                error=f"{reason}; previous caption restored")
        return await _escalate_restore_failure(
            request_id, payload, current_locator, old_caption_text,
            reason=reason,
            detail="the restore was sent but a live re-read does "
                   "not show the previous caption")
    description = str(body.get("description") or f"HTTP {status}")
    return await _escalate_restore_failure(
        request_id, payload, current_locator, old_caption_text,
        reason=reason,
        detail=f"the restore edit failed: {description}")


async def _reindex_and_confirm(request_id, payload, ledger,
                               current_locator, series_record,
                               old_caption_text):
    """Step 8: re-read the post through StreamBot (the seam needs
    the post-edit message object), call the bridge under its
    pinned timeout, map and confirm the result (DISP-3 item 2),
    then the step-9 terminal record on success."""
    from Backend.helper import librarian_reindex, librarian_store

    await librarian_store.update_fix_state(request_id, "reindexing")
    chat_id = current_locator["chat_id"]
    msg_id = current_locator["msg_id"]
    import Backend.pyrofork.bot as botmod

    bot_read = await _probe_client(botmod.StreamBot, chat_id, msg_id,
                                   "bot client not connected")
    if bot_read["state"] != "found":
        return await _restore(
            request_id, payload, ledger, current_locator,
            old_caption_text,
            reason=("indexing could not be confirmed — the post "
                    "could not be re-read through the bot client "
                    f"after the edit ({bot_read['state']})"))
    series_ids = None
    if series_record is not None:
        series_ids = dict(series_record.get("ids") or {})
    try:
        seam = await asyncio.wait_for(
            librarian_reindex.reindex_one_post(
                botmod.StreamBot, bot_read["message"],
                placement=payload.get("placement"),
                expected_ids=dict(payload["expected_ids"]),
                series_ids=series_ids),
            timeout=REINDEX_TIMEOUT)
    except asyncio.TimeoutError:
        return await _restore(
            request_id, payload, ledger, current_locator,
            old_caption_text,
            reason="indexing timed out — the re-index did not "
                   "complete within its time limit")
    if not isinstance(seam, dict):
        seam = {"state": "error", "ids": None, "filed": None,
                "error": "the re-index seam returned a malformed result"}
    seam_state = seam.get("state")
    if seam_state == "error":
        return await _finish(
            request_id, "edit_failed",
            error=f"the re-index seam failed: "
                  f"{seam.get('error') or 'unknown error'}")
    if seam_state == "metadata_failed":
        return await _restore(
            request_id, payload, ledger, current_locator,
            old_caption_text,
            reason="indexing failed — the corrected caption could "
                   "not be filed (the seam reported metadata_failed)")
    if seam_state in ("indexed", "duplicate"):
        confirmed, reason = _confirm_seam_result(seam, payload)
        if not confirmed:
            return await _restore(
                request_id, payload, ledger, current_locator,
                old_caption_text, reason=reason)
        # Step 9 — the terminal record: the row completes `fixed`
        # and only then does the ledger move, and only on the
        # store's guarded waiting -> fixed transition. A refused
        # transition means the operation is NOT reported as a
        # clean fixed (§2 rule 7).
        await librarian_store.update_fix_state(
            request_id, "fixed", index_confirmed=True, error=None)
        ledger_row, current = await librarian_store.mark_ledger_fixed(
            payload["chat_id"], payload["msg_id"])
        if ledger_row is None or current != "waiting":
            return await _finish(
                request_id, "escalated", index_confirmed=True,
                error=("indexing was confirmed but the ledger row "
                       "could not be moved to fixed (its status is "
                       f"{current!r}) — the fix is not reported as "
                       "complete"))
        row = await librarian_store.get_fix_by_request_id(request_id)
        return dict(row)
    return await _finish(
        request_id, "edit_failed",
        error=f"the re-index seam returned an unknown state: "
              f"{seam_state!r}")


async def _run_real_fix(request_id, payload, ledger, current_locator,
                        series_record, old_caption_text):
    """Steps 6–9 for one prepared real operation whose steps 1–5
    have passed: the edit, its exception classes, then re-index
    and confirm. The ledger row stays ``waiting`` throughout
    except on confirmed indexing (step 9, in
    ``_reindex_and_confirm``)."""
    from Backend.helper import librarian_store
    from Backend.helper.metadata.common import caption_with_links

    chat_id = current_locator["chat_id"]
    msg_id = current_locator["msg_id"]
    await librarian_store.update_fix_state(request_id, "editing")
    try:
        status, body = await _write_caption(
            chat_id, msg_id, payload["new_caption"])
    except BotApiTransportError as exc:
        # Ambiguous (§8): re-read before any further action. If
        # the new caption is already live, proceed to step 8;
        # never re-send the edit, never repost.
        result = await probe(chat_id, msg_id)
        landed = any(
            read["state"] == "found"
            and caption_with_links(read["message"])
            == canonical_from_html(payload["new_caption"])
            for read in result.values())
        if not landed:
            return await _finish(
                request_id, "edit_failed",
                error=(f"the edit's outcome is ambiguous ({exc}) "
                       f"and a re-read shows the new caption is not "
                       f"live — the edit was not re-sent"))
        return await _reindex_and_confirm(
            request_id, payload, ledger, current_locator,
            series_record, old_caption_text)
    if status == 200 and body.get("ok"):
        return await _reindex_and_confirm(
            request_id, payload, ledger, current_locator,
            series_record, old_caption_text)
    outcome, retry = classify_bot_api_error(status, body)
    if outcome == "flood_wait":
        return await _flood_wait_finish(
            request_id, retry, "the edit was rate-limited")
    if outcome == "not_modified":
        # The live caption already equals the target (drift
        # compares against expected_caption, §8 step 6).
        return await _reindex_and_confirm(
            request_id, payload, ledger, current_locator,
            series_record, old_caption_text)
    if outcome == "not_editable":
        # DISP-2's pinned terminal: no repost in Phase 5, no
        # second client; the reservation stands (a real write
        # attempt, §8 step 9); the ledger stays waiting.
        return await _finish(
            request_id, "edit_failed",
            error=("the post cannot be edited — repost arrives in "
                   "Phase 6"))
    if outcome == "not_found":
        # Re-run step 3's probe once and classify by its result
        # (§8 step 6). Whatever the outcome, the reservation
        # STANDS and no release mechanism is invoked: the row is
        # post-write-attempt (plan §2.2 item 2, §7.3 deviation).
        result = await probe(chat_id, msg_id)
        userbot, bot = result["userbot"], result["bot"]
        if any(read["state"] == "found" for read in result.values()):
            return await _finish(
                request_id, "edit_failed",
                error=("the Bot API reported the message not "
                       "found, but a re-probe still finds the post "
                       "— the edit was not re-sent"))
        if userbot["state"] == "missing" and bot["state"] == "missing":
            skip = ledger.get("skip_locator")
            if skip:
                skip_probe = await probe(skip["chat_id"], skip["msg_id"])
                if any(read["state"] == "found"
                       for read in skip_probe.values()):
                    return await _finish(
                        request_id, "edit_failed",
                        error=("the Bot API reported the message "
                               "not found; a re-probe finds the "
                               "original missing but the skip copy "
                               "readable — repost arrives in Phase "
                               "6; the budget reservation stands "
                               "because the edit was attempted"))
                skip_word = ("missing" if all(
                    read["state"] == "missing"
                    for read in skip_probe.values()) else "error")
            else:
                skip_word = "none"
            return await _finish(
                request_id, "refused_missing_post",
                error=(f"the Bot API reported the message not "
                       f"found and a re-probe confirms it is gone "
                       f"(userbot: missing; bot: missing; skip: "
                       f"{skip_word}) — the budget reservation "
                       f"stands because the edit was attempted"))
        err = (userbot["error"] if userbot["state"] == "error"
               else bot["error"])
        return await _finish(
            request_id, "edit_failed",
            error=(f"the Bot API reported the message not found "
                   f"and the re-probe could not complete: {err}"))
    description = str(body.get("description") or f"HTTP {status}")
    return await _finish(
        request_id, "edit_failed",
        error=f"the Bot API edit failed: {description}")


# ------------------------------------------------------------------
# Undo for edits (§8 Undo; phase5-plan §4, DISP-7/DISP-8)
# ------------------------------------------------------------------

def _locator_matches(row, current_locator) -> bool:
    loc = row.get("current_locator") or {}
    try:
        return (int(loc.get("chat_id")) == int(current_locator["chat_id"])
                and int(loc.get("msg_id")) == int(current_locator["msg_id"]))
    except (TypeError, ValueError):
        return False


def _undoable_reason(row):
    """Why a candidate row is not an undoable fix, or None when it
    is (§8 Undo: the target is a row in state `fixed`; DISP-7: edit
    undos only, and a dry-run row wrote nothing to undo)."""
    if row.get("state") != "fixed":
        return f"its state is {row.get('state')!r}, not 'fixed'"
    if row.get("action") != "edit":
        return "it is not an edit fix (repost undo arrives in Phase 6)"
    if row.get("dry_run"):
        return "it is a dry-run record — nothing was written to undo"
    return None


async def _resolve_undo_target(payload, current_locator):
    """(target_row, not_found_error): the row to undo, per §8 Undo
    + DISP-8 (OQ-1(a) as ratified). An explicit
    `target_request_id` names the row; absent, the target is the
    most recent `fixed` row for the post's `current_locator` —
    resolved as an in-engine filter over the store's existing
    list_history (created_at-descending), per the Tech Lead's
    ruling; no new store query was added."""
    from Backend.helper import librarian_store

    target_request_id = payload.get("target_request_id")
    if target_request_id:
        row = await librarian_store.get_fix_by_request_id(target_request_id)
        if row is None:
            return None, (f"no fix row with request_id "
                          f"{target_request_id!r} exists")
        if not _locator_matches(row, current_locator):
            return None, (f"the fix {target_request_id!r} belongs to "
                          f"a different post")
        reason = _undoable_reason(row)
        if reason is not None:
            return None, f"the fix {target_request_id!r} cannot be undone: {reason}"
        return row, None
    candidates = [row for row in await librarian_store.list_history()
                  if _locator_matches(row, current_locator)]
    for row in candidates:
        if _undoable_reason(row) is None:
            return row, None
    if candidates:
        return None, ("the most recent fix for this post cannot be "
                      f"undone: {_undoable_reason(candidates[0])}")
    return None, "no fix has been recorded for this post"


async def undo_one(payload: dict) -> dict:
    """{chat_id, msg_id, request_id, target_request_id?} -> the
    undo operation's persisted row, a step-1-class refusal, or a
    per-operation not_found (§4.1's route shape pin).

    §8 Undo, verbatim where the plan pins it: the undo creates its
    OWN librarian_fixes row (`action: "undo_edit"`, the payload's
    `request_id` is the undo's own idempotency key — DISP-8) and
    never mutates the original row. Gates are §8's asymmetry: this
    path requires test mode OFF (it mutates Telegram) and —
    through the route's token check — `librarian_enabled`; it does
    NOT consult `librarian_writes_enabled`, the allowlist, or the
    budget, and never sets `budget_counted`, so an undo works with
    the writes flag off (§15's rollback story depends on it)."""
    from Backend.helper import librarian_store
    from Backend.helper.metadata.common import caption_with_links
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

    # The one gate of §8 Undo that is the engine's own (enabled is
    # the route's token check): test mode on -> refusal, no row.
    if SettingsManager.current().librarian_test_mode:
        return _refusal(request_id, "refused_precondition",
                        "undo mutates Telegram — test mode is on")

    ledger = await librarian_store.get_ledger_row(chat_id, msg_id)
    if ledger is None:
        return {"request_id": request_id, "state": "not_found",
                "error": scrub_secrets(
                    f"unknown ledger row: {chat_id}:{msg_id}")}
    current_locator = ledger.get("current_locator") or ledger["source_locator"]

    target, problem = await _resolve_undo_target(payload, current_locator)
    if target is None:
        return {"request_id": request_id, "state": "not_found",
                "error": scrub_secrets(problem)}

    #----- Persist the undo row under the write lock, behind the
    #----- one-active-operation-per-post check (§8 Undo: refused
    #----- while any non-terminal operation exists on the post —
    #----- an undo racing a fix could otherwise remove the index
    #----- entry the fix just confirmed). The row references the
    #----- undone row (`undone_request_id`, its locators and
    #----- captions) and never counts against the budget.
    row = {
        "request_id": request_id,
        "batch_id": None,
        "ledger_id": ledger["_id"],
        "source_locator": dict(ledger["source_locator"]),
        "current_locator": dict(current_locator),
        "stem": ledger.get("stem"),
        "chat_id": ledger["chat_id"],
        "old_caption": target.get("old_caption") or "",
        "old_caption_text": target.get("old_caption_text") or "",
        "new_caption": target.get("new_caption") or "",
        "new_caption_text": target.get("new_caption_text") or "",
        "expected_ids": dict(target.get("expected_ids") or {}),
        "series_record_id": target.get("series_record_id"),
        "action": "undo_edit",
        "undone_request_id": target["request_id"],
        "state": "prepared",
        "index_confirmed": False,
        "error": None,
        "budget_counted": False,
        "dry_run": False,
    }
    async with _WRITE_LOCK:
        existing = await librarian_store.get_fix_by_request_id(request_id)
        if existing is not None:
            return _stored_result(existing)
        active = await librarian_store.find_active_fix(
            current_locator["chat_id"], current_locator["msg_id"])
        if active is not None:
            return _refusal(
                request_id, "refused_precondition",
                "another operation is in progress on this post "
                f"({active['request_id']}, state "
                f"{active.get('state')}) — undo is refused while it "
                "runs")
        row["prepared_at"] = datetime.now(timezone.utc)
        await librarian_store.insert_fix(row)

    chat_id = current_locator["chat_id"]
    msg_id = current_locator["msg_id"]
    old_caption_text = row["old_caption_text"]

    #----- Live re-read: the live caption must equal the recorded
    #----- new caption in canonical form (§8 Undo) — plain text
    #----- plus URLs via caption_with_links on both sides, never
    #----- the HTML form, so an ID-linked caption can match.
    result = await probe(chat_id, msg_id)
    live_matches = any(
        read["state"] == "found"
        and caption_with_links(read["message"]) == row["new_caption_text"]
        for read in result.values())
    if not live_matches:
        # Plan pin (§4.1): the refusal is recorded on the undo row
        # as refused_drift — the taxonomy's caption-mismatch
        # refusal — rather than inventing a state.
        return await _finish(
            request_id, "refused_drift",
            error="the live caption no longer matches the fix "
                  "being undone")

    #----- Restore the exact old caption through the same writer
    #----- (its HTML form reconstructed from the stored canonical
    #----- text, §4.2's shared helper), confirmed by a live
    #----- re-read in canonical form.
    await librarian_store.update_fix_state(request_id, "editing")
    try:
        status, body = await _bot_api_edit_caption(
            chat_id, msg_id, html_from_canonical(old_caption_text))
    except BotApiTransportError as exc:
        # Ambiguous (§8): re-read before concluding anything; if
        # the old caption is already live the restore landed.
        if not await _caption_is_live(chat_id, msg_id, old_caption_text):
            return await _finish(
                request_id, "edit_failed",
                error=(f"the undo's restore outcome is ambiguous "
                       f"({exc}) and a re-read does not show the "
                       f"previous caption — nothing else was changed"))
        status, body = 200, {"ok": True}
    if status == 200 and body.get("ok"):
        outcome, retry = "ok", None
    else:
        outcome, retry = classify_bot_api_error(status, body)
    if outcome == "flood_wait":
        # §4.1: undo's restore is an edit-class write and takes the
        # ordinary FloodWait rule (the deleting_original exception
        # does not cover it); the partial state is named — nothing
        # else had been attempted yet.
        return await _flood_wait_finish(
            request_id, retry,
            "the undo's restore was rate-limited — the previous "
            "caption was not restored and the index entry was not "
            "removed")
    if outcome not in ("ok", "not_modified"):
        description = str(body.get("description") or f"HTTP {status}")
        return await _finish(
            request_id, "edit_failed",
            error=(f"the undo's restore edit failed: {description} "
                   f"— the index entry was not removed"))
    if not await _caption_is_live(chat_id, msg_id, old_caption_text):
        return await _finish(
            request_id, "edit_failed",
            error=("the undo's restore was sent but a live re-read "
                   "does not show the previous caption — the index "
                   "entry was not removed"))

    #----- Remove the corrected index entry (the stripped locator,
    #----- §6's boundary conversion, as receiver.py calls it).
    #----- From here on the caption IS restored: any failure is a
    #----- partial undo, escalated with coordinates — never
    #----- reported `undone` while Telegram and the index (or the
    #----- ledger) disagree (§8 Undo).
    from Backend import db

    stripped_chat = int(str(chat_id).replace("-100", ""))
    try:
        removed = bool(await db.remove_media_part(stripped_chat, msg_id))
        remove_detail = "remove_media_part reported no entry removed"
    except Exception as exc:
        removed = False
        remove_detail = scrub_secrets(f"{type(exc).__name__}: {exc}")
    if not removed:
        return await _finish(
            request_id, "undo_partial",
            error=("the previous caption was restored, but the "
                   "corrected index entry could not be removed "
                   f"({remove_detail}) — post: {chat_id}:{msg_id}; "
                   f"index entry: {stripped_chat}:{msg_id}"))

    # The ledger must END in `waiting`: `fixed` means the guarded
    # transition just made it so; `waiting` means it already is —
    # reachable when an earlier undo of a stacked fix returned it
    # (a `fixed` ledger row can leave that status only through
    # this transition, so already-waiting is the invariant met,
    # not a disagreement). Any other status is a real divergence.
    ledger_row, current = await librarian_store.return_ledger_to_waiting(
        payload["chat_id"], payload["msg_id"])
    if ledger_row is None or current not in ("fixed", "waiting"):
        return await _finish(
            request_id, "undo_partial",
            error=("the previous caption was restored and the "
                   "corrected index entry was removed, but the "
                   "ledger row could not be returned to waiting "
                   f"(its status is {current!r}) — post: "
                   f"{chat_id}:{msg_id}"))
    return await _finish(request_id, "undone", error=None)


# ------------------------------------------------------------------
# The pipeline (§8 steps 1–9; steps 1–5 shared, then dry-run or real)
# ------------------------------------------------------------------

async def _skip_copy_branch(request_id, payload, ledger, skip_probe):
    """The missing-original / readable-skip-copy branch, real form
    (§8 steps 3 + 7; phase5-plan DISP-2). The missing-original
    branch is not a shortcut past the checks: step 4's exclusion
    re-check runs against the SKIP COPY (its file name, first
    found read in probe order per DISP-9's first-found pin, and
    its index entry) and step 5's caption-ID check runs as usual;
    only the drift check is waived — there is no live original to
    drift from — and the waiver is recorded in the terminal error.
    Every terminal of this branch is pre-write (no writer call has
    been attempted, the row is still ``prepared``), so no budget
    is consumed: the step-2 reservation is released through the
    store's guarded release — this branch is its sole call site
    (§2.3a, Security S5-2). The branch is NOT a ghost:
    ``refused_missing_post`` remains reserved for
    skip-also-missing."""
    from Backend.helper import librarian_store
    from Backend.helper.metadata.common import extract_default_id
    from Backend.helper.subtitles import is_subtitle_file

    found = [read for read in (skip_probe["userbot"], skip_probe["bot"])
             if read["state"] == "found"]
    first = found[0]
    media = (getattr(first["message"], "video", None)
             or getattr(first["message"], "document", None))
    file_name = getattr(media, "file_name", None) or ""
    if is_subtitle_file(file_name):
        state = "refused_subtitle"
        error = ("the skip copy is a subtitle file — subtitles are "
                 "never fixed")
    else:
        from Backend import db

        skip = ledger["skip_locator"]
        stripped_chat = int(str(skip["chat_id"]).replace("-100", ""))
        ids = await db.get_media_ids_by_part(stripped_chat,
                                             skip["msg_id"])
        tmdb_id = None
        if ids:
            try:
                tmdb_id = int(ids[1]) if ids[1] is not None else None
            except (TypeError, ValueError):
                tmdb_id = None
        if tmdb_id is not None and tmdb_id < 0:
            state = "refused_personal"
            error = ("the skip copy is filed as personal media "
                     "(negative TMDB id) — never fixed")
        elif extract_default_id(payload["new_caption"]) != _expected_default(
                payload["expected_ids"]):
            state = "refused_caption_id_mismatch"
            error = "new_caption does not carry the expected default ID"
        else:
            state = "edit_failed"
            error = ("original missing; skip copy readable — repost "
                     "arrives in Phase 6; the drift check is waived "
                     "on this branch (no live original to drift from)")
    await librarian_store.release_budget_reservation(request_id)
    return await _finish(request_id, state, error=error)


async def fix_one(payload: dict) -> dict:
    """One validated fix object (§4.5.1) -> one result body: a
    step-1 refusal {request_id, state, error}, a per-item not_found
    (batch transport outcome; the single-fix route renders it 404),
    or the persisted operation row (plus would_be_action on a fresh
    dry_run_complete, plus retry_after on a fresh flood_wait)."""
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

    # Mode gate (T-2; DESIGN §9 precedence item 4, phase5-plan
    # DISP-1 item 5) — evaluated before any client resolution.
    # Test mode on -> the dry-run path below. Test mode off -> a
    # real fix, which additionally requires the writes gate open;
    # budget remaining and the allowlist are checked at their own
    # steps (budget at the step-2 persist, §2.2 item 1; the
    # allowlist in step 1).
    real = not settings.librarian_test_mode
    if real and not settings.librarian_writes_enabled:
        return _refusal(
            request_id, "refused_precondition",
            "test mode is off and librarian_writes_enabled is off — "
            "a real fix needs the writes gate open")

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
    series_record = None
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
        series_record = record
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
    #----- write lock so check-and-create cannot race. For a real
    #----- operation the budget evaluation and the persist are ONE
    #----- unit inside the same lock (§2.2 item 1, Security S5-3):
    #----- the persist IS the reservation (budget_counted=true),
    #----- recomputed per item against its own IST day (§8 step 9).
    current_locator = ledger.get("current_locator") or ledger["source_locator"]
    row = _operation_row(payload, ledger, current_locator, real=real)
    async with _WRITE_LOCK:
        existing = await librarian_store.get_fix_by_request_id(request_id)
        if existing is not None:
            return _stored_result(existing)
        active = await librarian_store.find_active_fix(
            current_locator["chat_id"], current_locator["msg_id"])
        if active is not None:
            return _stored_result(active)
        if real:
            used = await librarian_store.count_budget_used_today()
            if used >= settings.librarian_edit_budget_per_day:
                if payload.get("batch_id") is None:
                    # Single item: a step-1-class refusal, no row.
                    return _refusal(
                        request_id, "refused_precondition",
                        "the daily edit budget is exhausted — no "
                        "reservation was made")
                # Batch: this item and every item after it are
                # recorded aborted (each later item re-evaluates
                # at its own persist, §2.2 item 1's position rule).
                aborted = dict(row)
                aborted["state"] = "aborted"
                aborted["budget_counted"] = False
                aborted["error"] = scrub_secrets(
                    "the daily edit budget is exhausted — this "
                    "item was not attempted")
                aborted["aborted_at"] = datetime.now(timezone.utc)
                stored = await librarian_store.insert_fix(aborted)
                return _stored_result(stored)
        row["prepared_at"] = datetime.now(timezone.utc)
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
                    if not real:
                        # Missing original, surviving skip copy: the
                        # real path reposts from it (§8 step 3); the
                        # drift check is waived on this branch by
                        # design.
                        return await _finish(
                            request_id, "dry_run_complete",
                            error=None, would_be_action="repost")
                    # The real branch (DISP-2): step 7's checks run
                    # against the skip copy, then the pinned
                    # Phase 5 terminal — no repost is attempted.
                    return await _skip_copy_branch(
                        request_id, payload, ledger, skip_probe)
                skip_word = ("missing" if all(r["state"] == "missing"
                                              for r in skip_probe.values())
                             else "error")
            else:
                skip_word = "none"
            # Ghost. The ledger row is NOT escalated in test mode
            # (§9 invariant); the row's error is the evidence record.
            result = await _finish(
                request_id, "refused_missing_post",
                error=f"userbot: missing; bot: missing; skip: {skip_word}")
            if real:
                # §8 step 3, real form: the ghost's ledger row is
                # escalated with the probe summary as evidence.
                try:
                    ledger_row, current = (
                        await librarian_store.transition_ledger(
                            chat_id, msg_id, "escalate", result["error"]))
                    if ledger_row is None or current != "waiting":
                        LOGGER.warning(
                            f"librarian: ghost escalation left ledger "
                            f"row for {request_id} untouched "
                            f"(status: {current})")
                except Exception as exc:
                    LOGGER.warning(
                        f"librarian: ghost ledger escalation failed "
                        f"for {request_id}: {scrub_secrets(exc)}")
            return result
        # An error is not proof of deletion — never a ghost (§8 step 3).
        err = userbot["error"] if userbot["state"] == "error" else bot["error"]
        return await _finish(request_id, "edit_failed", error=err)

    #----- Steps 4–5 on a found read, in DESIGN §8's order:
    #----- step 4 exclusion BEFORE step 5 drift (DISP-9 / §6.1).
    #----- Neither exclusion check depends on the caption — the
    #----- subtitle check reads the found message's file name and
    #----- the personal check reads the index — so both run first,
    #----- on the FIRST found read in probe order (userbot first;
    #----- first-found semantics, Senior N-1: never on every read,
    #----- never on the drift-selected read). The drift pin then
    #----- runs unchanged on the surviving reads: compare each
    #----- found read's canonical caption against expected_caption
    #----- and proceed on a read that matches; if none matches, it
    #----- is drift.
    from Backend.helper.metadata.common import (caption_with_links,
                                                extract_default_id)
    from Backend.helper.subtitles import is_subtitle_file

    first = found[0]
    media = (getattr(first["message"], "video", None)
             or getattr(first["message"], "document", None))
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

    chosen = None
    for read in found:  # userbot first — the probe's first client
        if caption_with_links(read["message"]) == payload["expected_caption"]:
            chosen = read
            break
    if chosen is None:
        return await _finish(
            request_id, "refused_drift",
            error="the live caption no longer matches expected_caption")

    if extract_default_id(payload["new_caption"]) != _expected_default(
            payload["expected_ids"]):
        return await _finish(
            request_id, "refused_caption_id_mismatch",
            error="new_caption does not carry the expected default ID")

    if not real:
        return await _finish(request_id, "dry_run_complete", error=None,
                             would_be_action="edit")
    # Steps 6–9 (§2.2): the edit, confirmation via the seam, the
    # terminal record.
    return await _run_real_fix(request_id, payload, ledger,
                               current_locator, series_record,
                               row["old_caption_text"])


_FLOOD_ABORT_REASON = ("batch terminated — an earlier item was "
                       "rate-limited by Telegram (flood_wait); this "
                       "item was not attempted")


async def _run_batch_item(batch, raw, batch_id):
    """One item of a real batch, under the batch semaphore. An
    item that acquires a slot after early termination was set is
    unattempted: it is recorded ``aborted`` without ever entering
    fix_one (no probe, no persist-as-prepared, no writer call).
    The termination flag is set while the flooding item still
    holds its slot, so no waiter can slip past it into fix_one."""
    item = dict(raw)
    item["batch_id"] = batch_id
    async with batch.semaphore:
        if batch.stop.is_set():
            return await _record_aborted(item, _FLOOD_ABORT_REASON)
        token = _BATCH_CTX.set(batch)
        try:
            result = await fix_one(item)
        finally:
            _BATCH_CTX.reset(token)
        if result.get("state") == "flood_wait":
            batch.stop.set()
        return result


async def fix_batch(payload: dict) -> dict:
    """{batch_id, serial, items} -> {batch_id, serial, results}.

    Dry-run batches are untouched by the Phase 5 machinery (§2.4):
    serial in request order, no semaphore, no pacing. Real
    batches run under §8 Concurrency: at most 4 items in flight
    (1 when ``serial``), items attempted in request order, writes
    to one channel paced ≈1/second (see ``_write_caption``), and
    the route still awaits the whole batch to terminal — the
    response carries every item's result in request order.

    Early termination (§8 step 2, §8 FloodWait) — semantics
    preserved exactly from the serial executor: a flood_wait item
    terminates the batch; items already in flight finish their
    current classification honestly; every unattempted item is
    recorded ``aborted`` with ``budget_counted=false``. Budget
    exhaustion needs no batch-level rule: each item re-evaluates
    the budget at its own step-2 persist and records itself
    ``aborted`` at the exhaustion point (§2.2 item 1's position
    rule), so a batch crossing the IST midnight still draws on
    the new day."""
    await _ensure_engine_init()
    items = payload["items"]
    serial = bool(payload.get("serial", True))
    from Backend.helper.settings_manager import SettingsManager

    if SettingsManager.current().librarian_test_mode:
        results = []
        for idx, raw in enumerate(items):
            item = dict(raw)
            item["batch_id"] = payload["batch_id"]
            result = await fix_one(item)
            results.append(result)
            if result.get("state") == "flood_wait":
                for rest in items[idx + 1:]:
                    rest = dict(rest)
                    rest["batch_id"] = payload["batch_id"]
                    results.append(
                        await _record_aborted(rest, _FLOOD_ABORT_REASON))
                break
        return {"batch_id": payload["batch_id"], "serial": serial,
                "results": results}
    batch = _BatchRun(serial)
    tasks = [asyncio.create_task(
        _run_batch_item(batch, raw, payload["batch_id"]))
        for raw in items]
    results = await asyncio.gather(*tasks)
    return {"batch_id": payload["batch_id"], "serial": serial,
            "results": list(results)}
