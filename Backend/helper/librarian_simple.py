"""Librarian, simple (Yan's model, 2026-11-20).

The bot verifies and writes the corrected caption. This module just
applies it:

    waiting()  -> the files that need fixing, with what the bot needs
    fix()      -> edit the caption, confirm it indexed, remember it
    undo()     -> put the old caption back
    counts()   -> waiting / fixed, counted straight, no cache games

One safety check survives from the old system, in plain words: if the
live caption is not the caption the bot based its fix on, the fix is
refused ("changed by someone else") — never edit over another edit.
Everything else is the bot's judgement, backed by three standing
Settings: librarian_enabled, librarian_dry_run (test mode), and the
daily edit budget. Fixes are recorded in tracking.simple_fixes, which
powers undo and the counts.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_ID_LINK_RE = re.compile(r"imdb\.com/title/(tt\d+)|themoviedb\.org/"
                         r"(?:tv|movie)/(\d+)")


# ---------------------------------------------------------------------------
# Dependencies — the real ones bind to the app; tests pass fakes.
# ---------------------------------------------------------------------------

class Deps:
    def __init__(self, read_caption, edit_caption, indexed_ids, ledger,
                 locator, fixes, teach_alias, settings):
        self.read_caption = read_caption    # async (chat, msg) -> str|None
        self.edit_caption = edit_caption    # async (chat, msg, caption)
        self.indexed_ids = indexed_ids      # async (chat, msg) -> (imdb, tmdb)|None
        self.ledger = ledger                # motor collection | None
        self.locator = locator              # motor collection | None
        self.fixes = fixes                  # motor collection | None
        self.teach_alias = teach_alias      # async (channel, stem, caption)
        self.settings = settings            # object with the 3 settings


def real_deps() -> Deps:
    from Backend import db
    from Backend.helper.settings_manager import SettingsManager

    tracking = (getattr(db, "dbs", None) or {}).get("tracking")

    async def read_caption(chat_id, msg_id):
        from Backend.pyrofork.bot import StreamBot
        msg = await StreamBot.get_messages(chat_id, msg_id)
        return (msg.caption if msg else None) or None

    async def edit_caption(chat_id, msg_id, caption):
        # Bot API as the Media Manager bot (v1's proven path). A flood
        # wait comes back as an exception carrying retry_after.
        import aiohttp
        token = SettingsManager.current().hermes_bot_token
        url = f"https://api.telegram.org/bot{token}/editMessageCaption"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    url, json={"chat_id": chat_id, "message_id": msg_id,
                               "caption": caption, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                res = await resp.json()
        if res.get("ok"):
            return
        params = res.get("parameters") or {}
        if res.get("error_code") == 429 or "retry_after" in params:
            err = RuntimeError(f"flood wait {params.get('retry_after')}s")
            err.retry_after = int(params.get("retry_after") or 30)
            raise err
        raise RuntimeError(f"edit failed: {res.get('description')}")

    async def indexed_ids(chat_id, msg_id):
        got = await db.get_media_ids_by_part(chat_id, msg_id)
        return (got[0], got[1]) if got else None

    async def teach_alias(channel, stem, caption):
        if tracking is None or not stem:
            return
        m = _ID_LINK_RE.search(caption or "")
        if not m:
            return
        await tracking["alias_map"].update_one(
            {"_id": f"{channel}:{stem}"},
            {"$set": {"channel": channel, "stem": stem,
                      "imdb_id": m.group(1),
                      "tmdb_id": int(m.group(2)) if m.group(2) else None,
                      "title": (caption or "").split("\n")[0],
                      "taught_at": time.time(), "source": "simple-fix"}},
            upsert=True)

    return Deps(
        read_caption, edit_caption, indexed_ids,
        ledger=tracking["reject_ledger"] if tracking is not None else None,
        locator=tracking["locator"] if tracking is not None else None,
        fixes=tracking["simple_fixes"] if tracking is not None else None,
        teach_alias=teach_alias, settings=SettingsManager.current())


# ---------------------------------------------------------------------------
# The whole surface
# ---------------------------------------------------------------------------

async def waiting(deps: Deps, channel=None, stem=None, limit=30):
    """Files waiting for a fix. Located ones carry their source post;
    the rest say so plainly (location_status='unknown')."""
    if deps.ledger is None:
        return {"ok": False, "message": "ledger unavailable"}
    query = {"status": {"$in": ["open", "unresolved"]}}
    if channel:
        query["scan_chat_id"] = int(channel)
    if stem:
        query["bits.stem"] = stem
    out = []
    async for doc in deps.ledger.find(query).limit(int(limit)):
        bits = doc.get("bits") or {}
        chat_id, msg_id, caption = (doc.get("scan_chat_id"),
                                    doc.get("scan_msg_id"),
                                    doc.get("caption"))
        located = bool(chat_id and msg_id)
        if deps.locator is not None:
            loc = await deps.locator.find_one({"_id": doc.get("key")})
            if loc:
                chat_id, msg_id = loc.get("chat_id"), loc.get("msg_id")
                caption = loc.get("caption") or caption
                located = bool(chat_id and msg_id)
        out.append({
            "key": doc.get("key"), "chat_id": chat_id, "msg_id": msg_id,
            "caption": caption, "stem": bits.get("stem"),
            "season": bits.get("season"), "episode": bits.get("episode"),
            "special": bits.get("special"), "year": bits.get("year"),
            "located": located,
            "reason": doc.get("reason_code")})
    return {"ok": True, "files": out, "count": len(out)}


def _caption_ids(caption):
    m = _ID_LINK_RE.search(caption or "")
    if not m:
        return None, None
    return m.group(1), (int(m.group(2)) if m.group(2) else None)


async def fix(deps: Deps, chat_id, msg_id, new_caption, expected_caption,
              key=None, stem=None, actor="bot"):
    """Apply one bot-written caption fix. Returns a plain result dict;
    every refusal says why in plain words."""
    s = deps.settings
    if not s.librarian_enabled:
        return {"ok": False, "message": "Librarian is off in Settings."}
    today = await _fixes_today(deps)
    budget = int(getattr(s, "librarian_edit_budget_per_day", 50) or 50)
    if today >= budget:
        return {"ok": False,
                "message": f"Daily edit budget spent ({today}/{budget})."}
    live = await deps.read_caption(chat_id, msg_id)
    if live is not None and live != expected_caption:
        return {"ok": False,
                "message": "Caption changed by someone else — fix refused.",
                "live_caption": live}
    if s.librarian_dry_run:
        return {"ok": True, "dry_run": True,
                "message": "Test mode: nothing was edited.",
                "would_set": new_caption}
    try:
        await deps.edit_caption(chat_id, msg_id, new_caption)
    except Exception as e:
        retry = getattr(e, "retry_after", None)
        if retry is not None:
            return {"ok": False, "flood_wait": retry,
                    "message": f"Telegram says wait {retry}s."}
        return {"ok": False, "message": f"Edit failed: {e}"}
    # Confirm the fix landed in the library index.
    want_imdb, want_tmdb = _caption_ids(new_caption)
    indexed = False
    for _ in range(15):
        got = await deps.indexed_ids(chat_id, msg_id)
        if got:
            if want_tmdb:
                indexed = int(got[1] or 0) == want_tmdb
            elif want_imdb:
                indexed = got[0] == want_imdb
            else:
                indexed = True
        if indexed:
            break
        await asyncio.sleep(1.0)
    if deps.fixes is not None:
        await deps.fixes.insert_one({
            "key": key, "chat_id": chat_id, "msg_id": msg_id,
            "before": expected_caption, "after": new_caption,
            "ts": time.time(), "actor": actor, "indexed": indexed,
            "reverted": False})
    if key and deps.ledger is not None:
        await deps.ledger.update_one(
            {"key": key},
            {"$set": {"status": "fixed", "fixed_at": time.time(),
                      "fixed_via": "simple"}})
    # Learn the stem -> identity mapping from the bot's own fix.
    if stem:
        await deps.teach_alias(chat_id, stem, new_caption)
    return {"ok": True, "indexed": indexed,
            "message": "Fixed and indexed." if indexed else
                       "Caption edited; index not confirmed yet."}


async def undo(deps: Deps, chat_id, msg_id):
    if deps.fixes is None:
        return {"ok": False, "message": "fix history unavailable"}
    doc = await deps.fixes.find_one(
        {"chat_id": chat_id, "msg_id": msg_id, "reverted": False},
        sort=[("ts", -1)])
    if not doc:
        return {"ok": False, "message": "No fix recorded for that file."}
    try:
        await deps.edit_caption(chat_id, msg_id, doc["before"])
    except Exception as e:
        return {"ok": False, "message": f"Undo edit failed: {e}"}
    await deps.fixes.update_one({"_id": doc["_id"]},
                                {"$set": {"reverted": True,
                                          "reverted_at": time.time()}})
    if doc.get("key") and deps.ledger is not None:
        await deps.ledger.update_one(
            {"key": doc["key"]}, {"$set": {"status": "open"}})
    return {"ok": True, "message": "Caption restored.",
            "restored": doc["before"]}


async def _fixes_today(deps: Deps) -> int:
    if deps.fixes is None:
        return 0
    start = datetime.now(IST).replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp()
    return await deps.fixes.count_documents(
        {"ts": {"$gte": start}, "reverted": False})


async def counts(deps: Deps):
    if deps.ledger is None:
        return {"ok": False, "message": "ledger unavailable"}
    waiting_n = await deps.ledger.count_documents(
        {"status": {"$in": ["open", "unresolved"]}})
    fixed_n = await deps.ledger.count_documents({"status": "fixed"})
    return {"ok": True, "waiting": waiting_n, "fixed": fixed_n,
            "fixed_today": await _fixes_today(deps)}
