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

# Yan's channels, by chat id — output says "Donghua", not a number.
CHANNEL_NAMES = {
    -1003923670111: "Announcement",
    -1004399192319: "Skip",
    -1004395609351: "Ani",
    -1003981572300: "Movie",
    -1003966209940: "Series",
    -1004494510060: "Donghua",
    -1004450903732: "Cartoon",
}


# ---------------------------------------------------------------------------
# Dependencies — the real ones bind to the app; tests pass fakes.
# ---------------------------------------------------------------------------

class Deps:
    def __init__(self, read_caption, edit_caption, indexed_ids, ledger,
                 locator, fixes, teach_alias, settings,
                 records=None, aliases=None, media_search=None,
                 scan_start=None, scan_status_of=None):
        self.read_caption = read_caption    # async (chat, msg) -> str|None
        self.edit_caption = edit_caption    # async (chat, msg, caption)
        self.indexed_ids = indexed_ids      # async (chat, msg) -> (imdb, tmdb)|None
        self.ledger = ledger                # motor collection | None
        self.locator = locator              # motor collection | None
        self.fixes = fixes                  # motor collection | None
        self.teach_alias = teach_alias      # async (channel, stem, caption)
        self.settings = settings            # object with the 3 settings
        self.records = records              # series_records collection
        self.aliases = aliases              # alias_map collection
        self.media_search = media_search    # async text -> [indexed doc]
        self.scan_start = scan_start        # async channels -> dict
        self.scan_status_of = scan_status_of  # () -> dict


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

    async def media_search(text):
        res = await db.search_documents(text, 1, 20)
        return [{"title": d.get("title"), "media_type": d.get("media_type"),
                 "tmdb_id": d.get("tmdb_id"), "imdb_id": d.get("imdb_id"),
                 "year": d.get("release_year")}
                for d in (res or {}).get("results") or []]

    async def scan_start(channels):
        from Backend.helper.scan_manager import scan_manager
        from Backend.pyrofork.bot import StreamBot
        return await scan_manager.start(
            StreamBot, [str(c) for c in channels], mode="scan")

    def scan_status_of():
        from Backend.helper.scan_manager import scan_manager
        return scan_manager.get_status()

    return Deps(
        read_caption, edit_caption, indexed_ids,
        ledger=tracking["reject_ledger"] if tracking is not None else None,
        locator=tracking["locator"] if tracking is not None else None,
        fixes=tracking["simple_fixes"] if tracking is not None else None,
        teach_alias=teach_alias, settings=SettingsManager.current(),
        records=tracking["series_records"] if tracking is not None else None,
        aliases=tracking["alias_map"] if tracking is not None else None,
        media_search=media_search, scan_start=scan_start,
        scan_status_of=scan_status_of)


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
            "channel_name": CHANNEL_NAMES.get(chat_id, str(chat_id)),
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
            "stem": stem, "before": expected_caption, "after": new_caption,
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


async def recent_fixes(deps: Deps, limit=20):
    """The latest fixes, newest first — what the page shows, with undo."""
    if deps.fixes is None:
        return []
    out = []
    cursor = deps.fixes.find({}).sort("ts", -1).limit(int(limit))
    async for doc in cursor:
        ts = doc.get("ts") or 0
        out.append({
            "chat_id": doc.get("chat_id"), "msg_id": doc.get("msg_id"),
            "before": doc.get("before"), "after": doc.get("after"),
            "when": datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M")
                    if ts else "",
            "indexed": doc.get("indexed"),
            "reverted": bool(doc.get("reverted"))})
    return out


async def waiting_grouped(deps: Deps, limit=600):
    """Waiting grouped by series (channel + stem), biggest first,
    files in episode order — how work is actually done."""
    res = await waiting(deps, limit=limit)
    if not res.get("ok"):
        return res
    groups = {}
    for f in res["files"]:
        g = groups.setdefault((f["chat_id"], f["stem"] or "(unknown)"), {
            "chat_id": f["chat_id"], "channel_name": f["channel_name"],
            "stem": f["stem"], "files": []})
        g["files"].append(f)
    out = []
    for g in groups.values():
        g["files"].sort(key=lambda x: (x["episode"] is None,
                                       x["episode"] or 0, x["msg_id"] or 0))
        eps = [x["episode"] for x in g["files"] if x["episode"] is not None]
        g["count"] = len(g["files"])
        g["episodes"] = f"{min(eps)}–{max(eps)}" if eps else ""
        out.append(g)
    out.sort(key=lambda g: (-g["count"], g["stem"] or ""))
    return {"ok": True, "groups": out, "total_files": res["count"]}


async def find(deps: Deps, text: str, limit=30):
    """Search the WHOLE library by name: indexed media + ledger."""
    needle = (text or "").strip().lower()
    if not needle:
        return {"ok": False, "message": "nothing to search for"}
    indexed = []
    if deps.media_search is not None:
        indexed = await deps.media_search(text)
    in_ledger = []
    if deps.ledger is not None:
        async for doc in deps.ledger.find({}).limit(2000):
            bits = doc.get("bits") or {}
            hay = f"{bits.get('stem') or ''} {doc.get('caption') or ''}".lower()
            if needle in hay:
                in_ledger.append({
                    "key": doc.get("key"), "status": doc.get("status"),
                    "stem": bits.get("stem"), "caption": doc.get("caption"),
                    "chat_id": doc.get("scan_chat_id"),
                    "msg_id": doc.get("scan_msg_id")})
            if len(in_ledger) >= int(limit):
                break
    return {"ok": True, "indexed": indexed[: int(limit)],
            "ledger": in_ledger}


async def file_detail(deps: Deps, chat_id, msg_id):
    out = {"ok": True, "chat_id": chat_id, "msg_id": msg_id,
           "channel_name": CHANNEL_NAMES.get(chat_id, str(chat_id)),
           "caption": await deps.read_caption(chat_id, msg_id),
           "indexed_ids": None, "ledger": None, "fixes": []}
    got = await deps.indexed_ids(chat_id, msg_id)
    if got:
        out["indexed_ids"] = {"imdb": got[0], "tmdb": got[1]}
    if deps.locator is not None:
        loc = await deps.locator.find_one(
            {"chat_id": chat_id, "msg_id": msg_id})
        if loc and deps.ledger is not None:
            doc = await deps.ledger.find_one({"key": loc.get("_id")})
            if doc:
                out["ledger"] = {"key": doc.get("key"),
                                  "status": doc.get("status"),
                                  "reason": doc.get("reason_code")}
    if deps.fixes is not None:
        cursor = deps.fixes.find(
            {"chat_id": chat_id, "msg_id": msg_id}).sort("ts", -1).limit(5)
        async for doc in cursor:
            out["fixes"].append({
                "before": doc.get("before"), "after": doc.get("after"),
                "ts": doc.get("ts"), "indexed": doc.get("indexed"),
                "reverted": bool(doc.get("reverted"))})
    return out


async def history(deps: Deps, stem=None, today=False, limit=30):
    if deps.fixes is None:
        return {"ok": False, "message": "fix history unavailable"}
    query = {}
    if stem:
        query["stem"] = stem
    if today:
        start = datetime.now(IST).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        query["ts"] = {"$gte": start}
    out = []
    cursor = deps.fixes.find(query).sort("ts", -1).limit(int(limit))
    async for doc in cursor:
        out.append({
            "chat_id": doc.get("chat_id"), "msg_id": doc.get("msg_id"),
            "stem": doc.get("stem"), "before": doc.get("before"),
            "after": doc.get("after"), "ts": doc.get("ts"),
            "indexed": doc.get("indexed"),
            "reverted": bool(doc.get("reverted"))})
    return {"ok": True, "fixes": out, "count": len(out)}


async def fix_batch(deps: Deps, items: list, actor="bot"):
    """Many fixes, in order, one result each. A flood wait or a
    spent budget stops the batch; other failures do not."""
    results, stopped = [], None
    for item in items or []:
        if stopped:
            results.append({"ok": False, "skipped": True,
                            "message": f"not attempted — batch stopped ({stopped})",
                            "chat_id": item.get("chat_id"),
                            "msg_id": item.get("msg_id")})
            continue
        res = await fix(deps, int(item["chat_id"]), int(item["msg_id"]),
                        str(item.get("new_caption") or ""),
                        str(item.get("expected_caption") or ""),
                        key=item.get("key"), stem=item.get("stem"),
                        actor=actor)
        res = {**res, "chat_id": item.get("chat_id"),
               "msg_id": item.get("msg_id")}
        results.append(res)
        if res.get("flood_wait") is not None:
            stopped = f"flood wait {res['flood_wait']}s"
        elif not res.get("ok") and "budget" in (res.get("message") or ""):
            stopped = "daily budget spent"
    applied = sum(1 for r in results if r.get("ok") and not r.get("dry_run"))
    return {"ok": True, "results": results, "applied": applied,
            "stopped": stopped}


async def series_record_get(deps: Deps, channel, stem):
    if deps.records is None:
        return {"ok": False, "message": "series records unavailable"}
    doc = await deps.records.find_one({"_id": f"{channel}:{stem}"})
    if not doc:
        return {"ok": True, "record": None}
    doc = {k: v for k, v in doc.items() if k != "_id"}
    return {"ok": True, "record": doc}


async def series_record_set(deps: Deps, record: dict):
    if deps.records is None:
        return {"ok": False, "message": "series records unavailable"}
    channel, stem = record.get("channel"), record.get("stem")
    if not channel or not stem:
        return {"ok": False, "message": "a record needs channel + stem"}
    doc = {**record, "updated_at": time.time()}
    await deps.records.update_one({"_id": f"{channel}:{stem}"},
                                  {"$set": doc}, upsert=True)
    return {"ok": True, "message": "Series record stored.",
            "id": f"{channel}:{stem}"}


async def alias_list(deps: Deps, channel=None, limit=100):
    if deps.aliases is None:
        return {"ok": False, "message": "alias map unavailable"}
    query = {"channel": int(channel)} if channel else {}
    out = []
    async for doc in deps.aliases.find(query).limit(int(limit)):
        out.append({"channel": doc.get("channel"), "stem": doc.get("stem"),
                    "imdb_id": doc.get("imdb_id"),
                    "tmdb_id": doc.get("tmdb_id"),
                    "title": doc.get("title")})
    return {"ok": True, "aliases": out, "count": len(out)}


async def alias_forget(deps: Deps, channel, stem):
    if deps.aliases is None:
        return {"ok": False, "message": "alias map unavailable"}
    res = await deps.aliases.delete_one({"_id": f"{channel}:{stem}"})
    gone = getattr(res, "deleted_count", 1)
    return {"ok": True, "forgotten": bool(gone),
            "message": "Alias removed." if gone else "No such alias."}


async def park(deps: Deps, key, parked=True):
    if deps.ledger is None:
        return {"ok": False, "message": "ledger unavailable"}
    doc = await deps.ledger.find_one({"key": key})
    if not doc:
        return {"ok": False, "message": "no such file in the ledger"}
    if parked and doc.get("status") == "fixed":
        return {"ok": False, "message": "that file is fixed, not waiting"}
    await deps.ledger.update_one(
        {"key": key},
        {"$set": {"status": "parked" if parked else "open"}})
    return {"ok": True, "message": "Parked." if parked else "Unparked."}


async def mode_info(deps: Deps):
    s = deps.settings
    channels = list(getattr(s, "librarian_channels", []) or [])
    return {"ok": True, "enabled": bool(s.librarian_enabled),
            "dry_run": bool(s.librarian_dry_run),
            "auto_apply": getattr(s, "librarian_auto_apply", ""),
            "digest": getattr(s, "librarian_digest", ""),
            "budget_per_day": int(
                getattr(s, "librarian_edit_budget_per_day", 50) or 50),
            "channels": channels,
            "channel_names": [CHANNEL_NAMES.get(int(c), str(c))
                              for c in channels]}


async def scan(deps: Deps, channel=None):
    if deps.scan_start is None:
        return {"ok": False, "message": "scan unavailable"}
    channels = [int(channel)] if channel else list(
        getattr(deps.settings, "librarian_channels", []) or [])
    if not channels:
        return {"ok": False,
                "message": "no channels configured in Settings"}
    return await deps.scan_start(channels)


async def scan_status(deps: Deps):
    if deps.scan_status_of is None:
        return {"ok": False, "message": "scan unavailable"}
    return {"ok": True, "scan": deps.scan_status_of()}


async def counts(deps: Deps):
    if deps.ledger is None:
        return {"ok": False, "message": "ledger unavailable"}
    waiting_n = await deps.ledger.count_documents(
        {"status": {"$in": ["open", "unresolved"]}})
    fixed_n = await deps.ledger.count_documents({"status": "fixed"})
    return {"ok": True, "waiting": waiting_n, "fixed": fixed_n,
            "fixed_today": await _fixes_today(deps)}
