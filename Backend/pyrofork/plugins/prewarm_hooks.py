"""Pre-Warm hooks — triggers and the warm runner (Wave 2 Phase B).

This pyrofork plugin wires the pure Pre-Warm core
(``Backend.helper.prewarm``) to the live system:

* Progress trigger — stream_buffer's progress listener fires when a
  real stream crosses the configured % of an episode; the next
  episode(s) of that series are resolved from the media DB and
  queued for warming.
* Pool trigger — DM Link calls ``offer_pooled`` after a file lands
  in the DL Channel.
* Ingest trigger — a channel-post handler (group 12, after the
  receiver) queues a head warm for new auth-channel posts.

The runner consumes REAL prefetch_stream generators with
``meta={"prewarm": True}`` and discards the bytes: identical pool,
flood and cap behaviour to a real stream, chunks land in the shared
(now persistent) chunk cache, and the adapter keeps pre-warm streams
out of the analytics. Safety, per the design: one warm at a time,
only while no real stream is active (a warm aborts the moment one
starts), a persisted daily MB budget, and smart-profile gates for
recent floods and low memory. The master toggle is read live from
settings on every decision — off means no offers, no runs, no cost.
"""

import asyncio
import time
from datetime import date

from pyrogram import Client, filters
from pyrogram.types import Message

from Backend import db
from Backend.helper import prewarm as pw
from Backend.helper import stream_buffer as sb
from Backend.helper.custom_dl import ACTIVE_STREAMS
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_planner = pw.WarmPlanner()
_worker_task = None
_budget_loaded = False
_last_flood_seen = 0
_last_flood_at = 0.0
STATS = {"last_warm_ts": 0.0, "warms_completed": 0}


def _policy() -> pw.PrewarmPolicy:
    return pw.build_policy(SettingsManager.current().to_dict())


def prewarm_snapshot() -> dict:
    """Live state for the Settings page status line."""
    policy = _policy()
    st = _planner.stats(date.today())
    return {
        "enabled": policy.enabled,
        "profile": policy.profile,
        "used_mb_today": st["used_mb_today"],
        "budget_mb": policy.daily_budget_mb,
        "queued": st["queued"],
        "warmed_today": st["warmed_today"],
        "last_warm_ts": STATS["last_warm_ts"],
    }


#----- Daily budget persistence (a restart must not reset the budget)

async def _load_budget_once() -> None:
    global _budget_loaded
    if _budget_loaded:
        return
    _budget_loaded = True
    try:
        doc = await db.dbs["tracking"]["prewarm_state"].find_one({"_id": "budget"})
        if doc and doc.get("day") == date.today().isoformat():
            _planner.restore_usage(float(doc.get("used_mb") or 0), date.today())
    except Exception:
        pass


async def _save_budget() -> None:
    try:
        st = _planner.stats(date.today())
        await db.dbs["tracking"]["prewarm_state"].update_one(
            {"_id": "budget"},
            {"$set": {"day": date.today().isoformat(),
                      "used_mb": st["used_mb_today"]}},
            upsert=True,
        )
    except Exception:
        pass


#----- Worker: one warm at a time, yielding to real streams

def _real_stream_active() -> bool:
    for info in ACTIVE_STREAMS.values():
        if info.get("status") == "active" and not (info.get("meta") or {}).get("prewarm"):
            return True
    return False


def _ensure_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def _worker() -> None:
    await _load_budget_once()
    while True:
        item = _planner.next()
        if item is None:
            await asyncio.sleep(2)
            continue
        try:
            await _run_warm(item)
        except Exception as e:
            LOGGER.warning(f"[PREWARM] warm failed for {item.chat_id}/{item.msg_id}: {e}")
        await asyncio.sleep(0.5)


async def _run_warm(item: pw.WarmItem) -> None:
    global _last_flood_seen, _last_flood_at
    policy = _policy()
    if not policy.enabled:
        return
    if not sb.get_policy().enabled:
        # Buffer off = upstream streamer = no shared cache to warm.
        return
    if policy.only_charging and pw.charging_state() is False:
        _planner.requeue(item)
        await asyncio.sleep(30)
        return
    if policy.smart:
        floods = sb.METRICS.floods_seen
        if floods != _last_flood_seen:
            _last_flood_seen = floods
            _last_flood_at = time.time()
        if _last_flood_at and time.time() - _last_flood_at < 600:
            _planner.requeue(item)
            await asyncio.sleep(30)
            return
        mem = pw.mem_available_mb()
        if mem is not None and mem < 600:
            _planner.requeue(item)
            await asyncio.sleep(30)
            return
    if _real_stream_active():
        _planner.requeue(item)
        await asyncio.sleep(20)
        return
    fetched_mb = await _fetch_into_cache(item, policy)
    _planner.complete(item, fetched_mb)
    STATS["last_warm_ts"] = time.time()
    STATS["warms_completed"] += 1
    await _save_budget()
    LOGGER.info(
        f"[PREWARM] warmed {item.chat_id}/{item.msg_id} "
        f"({item.reason}, {fetched_mb:.1f} MiB)")


async def _fetch_into_cache(item: pw.WarmItem, policy: pw.PrewarmPolicy) -> float:
    """Run real prefetch streams for the item's warm plan, discarding
    bytes. Returns MiB actually fetched (for the daily budget)."""
    from Backend.fastapi.routes.stream_routes import (
        _get_streamer, get_parallel_prefetch, select_best_client)
    from Backend.pyrofork.bot import multi_clients, work_loads

    if not multi_clients:
        return 0.0
    index = select_best_client(0)
    streamer = _get_streamer(multi_clients[index], index)
    file_id = await streamer.get_file_properties(
        chat_id=item.chat_id, message_id=item.msg_id)
    size = item.size or getattr(file_id, "file_size", 0) or 0
    plan = pw.warm_plan(size, policy)
    if not plan:
        return 0.0

    parallelism, prefetch = get_parallel_prefetch(len(multi_clients))
    extras = []
    if parallelism > 1 and len(multi_clients) > 1:
        others = sorted((i for i in multi_clients if i != index),
                        key=lambda i: work_loads.get(i, 0))
        for ec_idx in others[:parallelism - 1]:
            try:
                ec_streamer = _get_streamer(multi_clients[ec_idx], ec_idx)
                ec_fid = await ec_streamer.get_file_properties(
                    chat_id=item.chat_id, message_id=item.msg_id)
                extras.append((ec_idx, ec_streamer, ec_fid))
            except Exception:
                pass

    fetched = 0
    for offset, part_count in plan:
        if _real_stream_active():
            break  # a real stream started: yield immediately
        gen = await streamer.prefetch_stream(
            file_id=file_id, client_index=index, offset=offset,
            first_part_cut=0, last_part_cut=pw.CHUNK_SIZE,
            part_count=part_count, chunk_size=pw.CHUNK_SIZE,
            prefetch=prefetch, parallelism=parallelism,
            stream_id=f"prewarm-{item.chat_id}-{item.msg_id}-{offset}",
            meta={"prewarm": True, "title": item.reason},
            request=None, chat_id=item.chat_id, message_id=item.msg_id,
            extra_clients=extras)
        try:
            async for chunk in gen:
                fetched += len(chunk)
                if _real_stream_active():
                    break
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
    return fetched / pw.MiB


#----- Trigger 1: stream progress -> warm the next episode(s)

async def _resolve_next_episodes(chat_id: int, msg_id: int, ahead: int):
    """Find the tv doc holding this part and walk to the next episodes.

    Returns [(chat_id, msg_id, size_bytes, series_key), ...] — the
    first part of the same quality when available (else the largest).
    """
    part_match = {"$elemMatch": {"chat_id": chat_id, "msg_id": msg_id}}
    for i in range(1, db.current_db_index + 1):
        store = db.dbs[f"storage_{i}"]
        tv = await store["tv"].find_one(
            {"seasons.episodes.telegram.parts": part_match})
        if not tv:
            continue
        flat = []
        for season in sorted(tv.get("seasons") or [],
                             key=lambda s: s.get("season_number") or 0):
            for ep in sorted(season.get("episodes") or [],
                             key=lambda e: e.get("episode_number") or 0):
                flat.append(ep)
        cur_idx, cur_quality = None, None
        for idx, ep in enumerate(flat):
            for q in ep.get("telegram") or []:
                if any(p.get("chat_id") == chat_id and p.get("msg_id") == msg_id
                       for p in q.get("parts") or []):
                    cur_idx, cur_quality = idx, q.get("quality")
                    break
            if cur_idx is not None:
                break
        if cur_idx is None:
            return []
        out = []
        for ep in flat[cur_idx + 1: cur_idx + 1 + ahead]:
            quals = ep.get("telegram") or []
            if not quals:
                continue
            chosen = next((q for q in quals if q.get("quality") == cur_quality), None)
            if chosen is None:
                chosen = max(quals, key=lambda q: sum(
                    p.get("size_bytes") or 0 for p in q.get("parts") or []))
            parts = chosen.get("parts") or []
            if not parts:
                continue
            first = parts[0]
            size = sum(p.get("size_bytes") or 0 for p in parts)
            out.append((int(first["chat_id"]), int(first["msg_id"]),
                        size, str(tv.get("_id"))))
        return out
    return []


async def _offer_next_episodes(chat_id: int, msg_id: int,
                               policy: pw.PrewarmPolicy) -> None:
    await _load_budget_once()
    try:
        nexts = await _resolve_next_episodes(chat_id, msg_id, policy.episodes_ahead)
    except Exception as e:
        LOGGER.warning(f"[PREWARM] next-episode resolve failed: {e}")
        return
    for c, m, size, series_key in nexts:
        plan = pw.warm_plan(size, policy)
        item = pw.WarmItem(c, m, size, pw.plan_mb(plan),
                           reason="next-episode", series_key=series_key)
        if _planner.offer(item, policy, date.today()):
            _ensure_worker()


def _on_progress(event) -> None:
    try:
        policy = _policy()
        if not policy.enabled:
            return
        if (event.get("ratio") or 0) * 100 < policy.trigger_pct:
            return
        chat_id, msg_id = event.get("chat_id"), event.get("msg_id")
        if not chat_id or not msg_id:
            return
        asyncio.create_task(_offer_next_episodes(int(chat_id), int(msg_id), policy))
    except Exception:
        pass


sb.set_progress_listener(_on_progress)


#----- Trigger 2: DM Link pool copies (called by the dl_link plugin)

def offer_pooled(chat_id: int, msg_id: int, size: int = 0) -> None:
    """Queue a head+tail warm for a freshly pooled file. Best-effort:
    any failure here must never affect the link that triggered it."""
    try:
        policy = _policy()
        if not policy.enabled:
            return
        if size:
            est = pw.plan_mb(pw.warm_plan(size, policy))
        else:
            est = float(policy.head_mb + policy.tail_mb)
        item = pw.WarmItem(int(chat_id), int(msg_id), int(size or 0),
                           est, reason="pooled")
        if _planner.offer(item, policy, date.today()):
            _ensure_worker()
    except Exception:
        pass


#----- Trigger 3: new auth-channel posts (after the receiver, group 12)

@Client.on_message(filters.channel & (filters.document | filters.video), group=12)
async def prewarm_ingest(client: Client, message: Message):
    try:
        policy = _policy()
        if not policy.enabled or not policy.ingest_head:
            return
        chat_id = message.chat.id if message.chat else 0
        auth = {str(x).strip() for x in SettingsManager.current().auth_channels}
        if str(chat_id) not in auth:
            return
        media = message.document or message.video
        size = getattr(media, "file_size", 0) or 0
        plan = pw.warm_plan(size, policy)
        item = pw.WarmItem(int(chat_id), int(message.id), size,
                           pw.plan_mb(plan), reason="ingest")
        if _planner.offer(item, policy, date.today()):
            _ensure_worker()
    except Exception as e:
        LOGGER.warning(f"[PREWARM] ingest hook failed: {e}")
