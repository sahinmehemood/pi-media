"""BufferedByteStreamer — the streaming buffer plugin's adapter.

Subclasses upstream's ``ByteStreamer`` and overrides only
``prefetch_stream``. The external contract is identical to upstream (same
signature, same ACTIVE_STREAMS registry keys and finish-time accounting,
same in-order trimmed-byte generator, same disconnect handling), so every
existing caller — Stremio routes, /dl links, virtual and zip streamers —
works unchanged. Inside, the producer is the buffer engine from
``Backend.helper.stream_buffer``:

- adaptive in-flight window (AIMD) instead of upstream's fixed formula,
  opened at maximum for mid-file starts (seeks / tail probes),
- client pool scheduled by flood penalty and per-client concurrency
  caps, tie-broken by per-(client, DC) fetch speed,
- one shared FloodBudget per stream (capped waits, client shedding);
  a flood wait sleeps AFTER releasing the client's in-flight slot, so
  a penalised client never blocks its own concurrency budget,
- a byte-budgeted chunk cache consulted before every fetch,
- honest three-state chunk results: a chunk that cannot be fetched ends
  the stream as an *error*, never as a clean EOF (upstream conflates the
  two, silently truncating files).

Kill switch: when the policy is disabled, ``prefetch_stream`` delegates to
upstream unchanged (design invariant I6).

See ``docs/stream-buffer/DESIGN.md``.
"""

from __future__ import annotations

import asyncio
import random
import re
import secrets
import time
from collections import deque
from typing import Dict, List, NamedTuple, Optional, Tuple

from fastapi import Request
from pyrogram import raw
from pyrogram.file_id import FileId

from Backend import db
from Backend.helper import stream_buffer as sb
from Backend.helper.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS, ByteStreamer
from Backend.helper.pyro import get_file_ids
from Backend.logger import LOGGER
from Backend.pyrofork.bot import client_avg_mbps, client_failures, work_loads

FETCH_TIMEOUT_S = 15.0  # per-attempt GetFile timeout (matches upstream)
MAX_CHUNK_TRIES = 3  # non-flood attempts per chunk (matches upstream)
MAX_FLOOD_TRIES = 5  # flood retries per chunk (matches upstream)

_FLOOD_RE = re.compile(r"wait of (\d+) second", re.IGNORECASE)


class PoolMember(NamedTuple):
    """One streamable client in a stream's session pool."""

    client_index: int
    session: object
    location_box: List[object]
    refresh: object


def _backoff_s(tries: int) -> float:
    return min(0.5 * (2 ** (tries - 1)), 10.0)


def _initial_window(policy: sb.BufferPolicy, offset: int) -> Tuple[int, int, bool]:
    """(initial, maximum, seek_burst) for a new stream's adaptive window.

    A stream starting mid-file (offset > 0) is a player seek or a tail
    probe: it opens at the maximum window so the player gets its bytes
    immediately, and AIMD settles the window within a few chunks.
    Sequential streams start from the speed tracker's scaled workers.
    """
    if policy.adaptive:
        maximum = policy.workers * 2
        initial = max(1, int(round(policy.workers * sb.get_speed_tracker().multiplier())))
        initial = min(maximum, initial)
    else:
        maximum = initial = policy.workers
    seek_burst = offset > 0 and policy.seek_burst
    if seek_burst:
        initial = maximum
    return initial, maximum, seek_burst


class BufferedByteStreamer(ByteStreamer):
    """ByteStreamer with the streaming buffer engine inside."""

    async def prefetch_stream(
        self,
        file_id: FileId,
        client_index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        prefetch: int,
        parallelism: int,
        stream_id: Optional[str] = None,
        meta: Optional[dict] = None,
        request: Optional[Request] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        extra_clients: Optional[List] = None,
    ):
        policy = sb.get_policy()
        if not policy.enabled:
            return await super().prefetch_stream(
                file_id, client_index, offset, first_part_cut, last_part_cut,
                part_count, chunk_size, prefetch, parallelism,
                stream_id=stream_id, meta=meta, request=request,
                chat_id=chat_id, message_id=message_id,
                extra_clients=extra_clients,
            )
        return await self._buffered_prefetch(
            policy, file_id, client_index, offset, first_part_cut,
            last_part_cut, part_count, chunk_size, stream_id, meta, request,
            chat_id, message_id, extra_clients,
        )

    async def _buffered_prefetch(
        self,
        policy: sb.BufferPolicy,
        file_id: FileId,
        client_index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        stream_id: Optional[str],
        meta: Optional[dict],
        request: Optional[Request],
        chat_id: Optional[int],
        message_id: Optional[int],
        extra_clients: Optional[List],
    ):
        if not stream_id:
            stream_id = secrets.token_hex(8)

        # ----- Registry entry: same keys upstream maintains, plus buffer_*
        now = time.time()
        stream_entry = {
            "stream_id": stream_id,
            "msg_id": getattr(file_id, "local_id", None) or None,
            "chat_id": getattr(file_id, "chat_id", None),
            "dc_id": file_id.dc_id,
            "client_index": client_index,
            "start_ts": now,
            "last_ts": now,
            "total_bytes": 0,
            "avg_mbps": 0.0,
            "instant_mbps": 0.0,
            "peak_mbps": 0.0,
            "recent_measurements": deque(maxlen=3),
            "status": "active",
            "part_count": part_count,
            "prefetch": policy.readahead_chunks(chunk_size),
            "meta": meta or {},
            "load_held": True,
            "buffered": True,
            "buffer_profile": policy.profile,
        }
        ACTIVE_STREAMS[stream_id] = stream_entry
        work_loads[client_index] += 1

        cache = sb.get_cache()
        media_id = getattr(file_id, "media_id", None)
        budget = sb.FloodBudget(policy.max_flood_wait_s)
        initial, maximum, seek_burst = _initial_window(policy, offset)
        stream_entry["buffer_seek_burst"] = seek_burst
        window = sb.AdaptiveWindow(initial, maximum, adaptive=policy.adaptive)
        file_dc = getattr(file_id, "dc_id", 0) or 0
        dc_speeds = sb.get_dc_speed_table()

        q: asyncio.Queue = asyncio.Queue(maxsize=policy.readahead_chunks(chunk_size))
        stop_event = asyncio.Event()

        # ----- Session pool (same construction as upstream)
        media_session = await self._get_media_session(file_id)
        location_box: List[object] = [await self._get_location(file_id)]

        async def _make_refresh_fn(loc_b, streamer_ref, file_id_ref):
            async def _refresh() -> bool:
                if not chat_id or not message_id:
                    return False
                try:
                    key = (int(chat_id), int(message_id))
                    streamer_ref._file_id_cache.pop(key, None)
                    fresh = await get_file_ids(streamer_ref.client, chat_id, message_id)
                    if fresh:
                        streamer_ref._file_id_cache[key] = fresh
                        loc_b[0] = await ByteStreamer._get_location(fresh)
                        return True
                except Exception as exc:
                    LOGGER.warning("Location refresh failed for chat=%s msg_id=%s: %s", chat_id, message_id, exc)
                return False
            return _refresh

        primary_refresh = await _make_refresh_fn(location_box, self, file_id)
        pool = [PoolMember(client_index, media_session, location_box, primary_refresh)]
        if extra_clients:
            for ec_idx, ec_streamer, ec_file_id in extra_clients:
                try:
                    ec_session = await ec_streamer._get_media_session(ec_file_id)
                    ec_loc_box = [await ByteStreamer._get_location(ec_file_id)]
                    ec_refresh = await _make_refresh_fn(ec_loc_box, ec_streamer, ec_file_id)
                    pool.append(PoolMember(ec_idx, ec_session, ec_loc_box, ec_refresh))
                except Exception as e:
                    LOGGER.warning("Skipping extra client %s (session setup failed): %s", ec_idx, e)

        in_flight = [0] * len(pool)

        def pick_pool_pos(tried: set, seq: int, attempt: int) -> Optional[int]:
            """Least-penalised, least-loaded pool member under its cap.

            The per-client cap is strict (invariant I5): members at cap are
            not candidates. Returns None when no member can take a fetch
            right now — either all are shed (permanent) or all live members
            are at cap (the caller waits for a slot; see fetch_one).
            """
            candidates = [pos for pos, member in enumerate(pool)
                          if not budget.is_shed(member.client_index)
                          and in_flight[pos] < policy.per_client_cap]
            if not candidates:
                return None
            fresh = [pos for pos in candidates if pos not in tried]
            if fresh:
                candidates = fresh
            start = (seq + attempt) % len(candidates)
            rotated = candidates[start:] + candidates[:start]
            # Order: flood penalty, then load, then fastest-to-this-DC.
            return min(rotated, key=lambda pos: (
                budget.penalty(pool[pos].client_index),
                in_flight[pos],
                -dc_speeds.mbps(pool[pos].client_index, file_dc),
            ))

        def any_client_alive() -> bool:
            return any(not budget.is_shed(m.client_index) for m in pool)

        async def fetch_one(seq: int, abs_off: int) -> Tuple[int, Optional[bytes], str]:
            """Fetch one chunk -> (seq, bytes, status); status in ok/eof/failed."""
            if chunk_size > 0 and abs_off % chunk_size == 0:
                position = ("chunk", abs_off // chunk_size)
            else:
                position = ("offset", abs_off)
            key = sb.cache_key(media_id, chunk_size, position)
            if key is not None:
                cached = cache.get(key)
                if cached is not None:
                    sb.METRICS.incr("cache_hits")
                    return seq, cached, "ok"
                sb.METRICS.incr("cache_misses")

            tries = 0
            flood_tries = 0
            tried: set = set()
            attempt = 0
            while tries < MAX_CHUNK_TRIES and flood_tries < MAX_FLOOD_TRIES and not stop_event.is_set():
                pos = pick_pool_pos(tried, seq, attempt)
                attempt += 1
                if pos is None:
                    if not any_client_alive():
                        break  # every client shed — nothing left to try
                    # All live clients are at their concurrency cap: wait
                    # for a slot instead of failing the chunk (I5).
                    await asyncio.sleep(0.02)
                    continue
                tried.add(pos)
                member = pool[pos]
                c_idx, c_session, c_loc_box, c_refresh = (
                    member.client_index, member.session,
                    member.location_box, member.refresh)
                in_flight[pos] += 1
                flood_wait = 0.0
                send_started = time.monotonic()
                try:
                    r = await asyncio.wait_for(
                        c_session.send(
                            raw.functions.upload.GetFile(
                                location=c_loc_box[0], offset=abs_off, limit=chunk_size
                            )
                        ),
                        timeout=FETCH_TIMEOUT_S,
                    )
                    chunk_bytes = getattr(r, "bytes", None) if r else None
                    if chunk_bytes == b"":
                        return seq, None, "eof"
                    if chunk_bytes:
                        budget.record_success(c_idx)
                        window.note_success()
                        sb.METRICS.incr("chunks_fetched")
                        elapsed = time.monotonic() - send_started
                        if elapsed > 0:
                            sb.note_dc_speed(
                                c_idx, file_dc,
                                (len(chunk_bytes) / (1024 * 1024)) / elapsed)
                        if key is not None:
                            cache.put(key, chunk_bytes)
                        return seq, chunk_bytes, "ok"
                    tries += 1  # empty/None payload without EOF semantics: retry
                except asyncio.TimeoutError:
                    tries += 1
                    client_failures[c_idx] = client_failures.get(c_idx, 0) + 1
                    await asyncio.sleep(_backoff_s(tries))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    err_str = str(e)
                    if "FILE_REFERENCE" in err_str or "file_reference" in err_str.lower():
                        await c_refresh()
                    flood_m = _FLOOD_RE.search(err_str)
                    if flood_m:
                        flood_tries += 1
                        required = float(flood_m.group(1))
                        wait = required + random.uniform(0.5, 2.0)
                        sb.METRICS.incr("floods_seen")
                        window.note_flood()
                        if sb.get_flood_sender() is not None:
                            _title = (meta or {}).get("title") or (meta or {}).get("file_name") or ""
                            for _ev in sb.note_flood(c_idx, wait, _title):
                                asyncio.create_task(sb.publish_flood_event(_ev))
                        if budget.register_flood(c_idx, wait):
                            # Do NOT sleep here: the finally block first
                            # releases this client's in-flight slot, the
                            # wait happens after it (below), and the loop
                            # then re-picks — possibly a healthier client.
                            flood_wait = wait
                        else:
                            # Budget exhausted: shed this client for the
                            # stream and let another client take the chunk.
                            budget.shed(c_idx)
                            sb.METRICS.incr("workers_shed")
                            tries += 1
                    else:
                        tries += 1
                        await asyncio.sleep(_backoff_s(tries))
                finally:
                    in_flight[pos] -= 1
                if flood_wait:
                    await asyncio.sleep(flood_wait)
            sb.METRICS.incr("chunks_failed")
            return seq, None, "failed"

        async def producer():
            scheduled: Dict[int, asyncio.Task] = {}
            results: Dict[int, bytes] = {}
            next_to_schedule = 0
            next_to_put = 0
            end_seq: Optional[int] = None
            end_kind: Optional[str] = None
            try:
                if part_count <= 0:
                    await q.put((None, None))
                    return
                while True:
                    if stop_event.is_set():
                        break
                    while (end_seq is None and next_to_schedule < part_count
                           and len(scheduled) < window.window):
                        seq = next_to_schedule
                        scheduled[seq] = asyncio.create_task(
                            fetch_one(seq, offset + seq * chunk_size))
                        next_to_schedule += 1
                    if not scheduled:
                        break
                    done, _ = await asyncio.wait(
                        scheduled.values(), return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        seq = next(s for s, t in scheduled.items() if t is task)
                        del scheduled[seq]
                        try:
                            _s, data, status = task.result()
                        except Exception:
                            _s, data, status = seq, None, "failed"
                        if status == "ok":
                            results[seq] = data
                        elif end_seq is None or seq < end_seq:
                            end_seq = seq
                            end_kind = "eof" if status == "eof" else "error"
                    while (next_to_put in results
                           and (end_seq is None or next_to_put < end_seq)):
                        data = results.pop(next_to_put)
                        await q.put((offset + next_to_put * chunk_size, data))
                        next_to_put += 1
                    if end_seq is not None and next_to_put >= end_seq:
                        break
                    if next_to_put >= part_count:
                        break
                if end_kind == "error":
                    await q.put(("error", None))
                else:
                    await q.put((None, None))
            except asyncio.CancelledError:
                try:
                    await q.put((None, None))
                except Exception:
                    pass
                raise
            except Exception as e:
                LOGGER.exception("Buffered producer error for stream %s: %s", stream_id, e)
                try:
                    await q.put(("error", None))
                except Exception:
                    pass
            finally:
                stop_event.set()
                for t in scheduled.values():
                    if not t.done():
                        t.cancel()
                if scheduled:
                    await asyncio.gather(*scheduled.values(), return_exceptions=True)
                    scheduled.clear()

        async def consumer_generator():
            producer_task = asyncio.create_task(producer())
            current_part_idx = 1
            disconnect_counter = 0
            # Progress events (Pre-Warm triggers): fired at coarse marks
            # of the file served, only when a listener is registered and
            # this is a real stream (never for pre-warm streams — a warm
            # must not chain-trigger further warms).
            progress_marks = ()
            approx_total = part_count * chunk_size if part_count > 0 and chunk_size > 0 else 0
            if (approx_total > 0 and not (meta or {}).get("prewarm")
                    and sb.get_progress_listener() is not None):
                progress_marks = sb.PROGRESS_MARKS
            fired_marks = set()
            try:
                while True:
                    disconnect_counter += 1
                    if disconnect_counter % 8 == 0:
                        try:
                            if request and await request.is_disconnected():
                                stop_event.set()
                                stream_entry["status"] = "cancelled"
                                break
                        except Exception:
                            pass
                    try:
                        off_chunk = await asyncio.wait_for(
                            q.get(), timeout=float(policy.stall_timeout_s))
                    except asyncio.TimeoutError:
                        LOGGER.error("Buffered producer stall (%ss) for stream %s — aborting",
                                     policy.stall_timeout_s, stream_id)
                        sb.METRICS.incr("stall_timeouts")
                        window.note_stall()
                        stop_event.set()
                        stream_entry["status"] = "error"
                        break
                    if off_chunk is None:
                        break
                    off, chunk = off_chunk
                    if off == "error":
                        # Honest failure: end the generator short of the
                        # promised Content-Length instead of faking EOF.
                        stream_entry["status"] = "error"
                        break
                    if off is None and chunk is None:
                        break

                    if part_count == 1:
                        out_chunk = chunk[first_part_cut:last_part_cut]
                    elif current_part_idx == 1:
                        out_chunk = chunk[first_part_cut:]
                    elif current_part_idx == part_count:
                        out_chunk = chunk[:last_part_cut]
                    else:
                        out_chunk = chunk

                    chunk_len = len(out_chunk) if out_chunk else 0
                    now_ts = time.time()
                    last_ts = stream_entry.get("last_ts") or stream_entry.get("start_ts") or now_ts
                    elapsed = max(now_ts - last_ts, 1e-6)
                    recent = stream_entry.setdefault("recent_measurements", deque(maxlen=3))
                    recent.append((chunk_len, elapsed))
                    if len(recent) >= 2:
                        total_b = sum(b for b, _ in recent)
                        total_t = sum(t for _, t in recent)
                        instant_mbps = min((total_b / (1024 * 1024)) / max(total_t, 0.01), 1000.0)
                    else:
                        instant_mbps = 0.0
                    stream_entry["total_bytes"] = stream_entry.get("total_bytes", 0) + chunk_len
                    if progress_marks:
                        ratio = stream_entry["total_bytes"] / approx_total
                        for mark in progress_marks:
                            if ratio >= mark and mark not in fired_marks:
                                fired_marks.add(mark)
                                sb.notify_progress({
                                    "stream_id": stream_id,
                                    "chat_id": chat_id,
                                    "msg_id": message_id,
                                    "ratio": round(ratio, 3),
                                    "title": (meta or {}).get("title") or "",
                                    "meta": meta or {},
                                })
                    stream_entry["last_ts"] = now_ts
                    total_time = max(now_ts - (stream_entry.get("start_ts") or now_ts), 1e-6)
                    stream_entry["avg_mbps"] = (stream_entry["total_bytes"] / (1024 * 1024)) / total_time
                    stream_entry["instant_mbps"] = instant_mbps
                    if instant_mbps > stream_entry.get("peak_mbps", 0.0):
                        stream_entry["peak_mbps"] = instant_mbps

                    yield out_chunk
                    current_part_idx += 1
            except asyncio.CancelledError:
                stop_event.set()
                if not producer_task.done():
                    producer_task.cancel()
                stream_entry["status"] = "cancelled"
                raise
            except Exception as e:
                LOGGER.exception("Buffered consumer error for stream %s: %s", stream_id, e)
                stop_event.set()
                stream_entry["status"] = "error"
                if not producer_task.done():
                    producer_task.cancel()
            finally:
                stop_event.set()
                if not producer_task.done():
                    try:
                        producer_task.cancel()
                        await asyncio.wait_for(producer_task, timeout=2.0)
                    except (Exception, asyncio.CancelledError):
                        pass
                try:
                    end_ts = time.time()
                    total_bytes = stream_entry.get("total_bytes", 0)
                    start_ts = stream_entry.get("start_ts") or end_ts
                    duration = end_ts - start_ts if end_ts > start_ts else 0.0
                    avg_mbps = (total_bytes / (1024 * 1024)) / (duration if duration > 0 else 1e-6)
                    final_status = ("finished" if stream_entry.get("status") == "active"
                                    else stream_entry.get("status", "finished"))
                    stream_entry.update({
                        "end_ts": end_ts,
                        "duration": duration,
                        "avg_mbps": avg_mbps,
                        "status": final_status,
                        "parallelism": policy.workers,
                        "chunk_size": chunk_size,
                        "buffer_window_end": window.window,
                        "buffer_floods": budget.floods_seen,
                        "buffer_shed": list(budget.shed_clients()),
                        "buffer_flood_spent_s": round(budget.spent_s(), 1),
                    })
                    if final_status == "finished":
                        sb.METRICS.incr("streams_completed")
                        sb.note_stream_speed(avg_mbps)
                    elif final_status == "error":
                        sb.METRICS.incr("streams_failed")

                    prev = client_avg_mbps.get(client_index, 0.0)
                    client_avg_mbps[client_index] = avg_mbps if prev == 0.0 else 0.5 * prev + 0.5 * avg_mbps

                    if not (meta or {}).get("prewarm"):
                        asyncio.create_task(db.log_stream_stats(stream_entry))

                    async def delayed_pop():
                        await asyncio.sleep(3)
                        try:
                            if stream_id in ACTIVE_STREAMS:
                                RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(stream_id))
                        except Exception:
                            pass

                    asyncio.create_task(delayed_pop())
                finally:
                    if stream_entry.pop("load_held", False):
                        work_loads[client_index] = max(0, work_loads.get(client_index, 0) - 1)
                stop_event.set()

        return consumer_generator()
