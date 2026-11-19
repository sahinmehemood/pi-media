"""Streaming buffer engine core — policy, adaptation, cache, flood budget.

This module is the deep module behind the streaming buffer plugin (see
``docs/stream-buffer/DESIGN.md``). All scheduling intelligence lives here,
behind a small interface; the Telegram-facing adapter
(``Backend.helper.buffered_streamer``) only wires it to Pyrogram sessions.

Standard library only — no ``Backend`` or third-party imports — so the whole
module is testable through its interface without Telegram, a database, or
the web stack.

Interface summary
-----------------
- ``BufferPolicy`` / ``build_policy(data)``: immutable, clamped configuration
  resolved from settings (profiles + custom values).
- ``AdaptiveWindow``: per-stream AIMD concurrency controller.
- ``SpeedTracker``: gated EWMA of finished-stream throughput.
- ``ChunkCache``: byte-budgeted LRU cache for fetched chunks, with an
  optional best-effort disk spill tier (``DiskTier``) that survives
  restarts.
- ``FloodBudget``: per-stream FloodWait accounting and client shedding.
- ``DcSpeedTable``: per (client, DC) gated EWMA of fetch throughput,
  used to break picker ties toward the client fastest to a file's DC.
- ``FloodAlerter``: aggregates flood events for an optional owner
  alert; a plugin registers the sender via ``set_flood_sender``.
- ``BufferMetrics``: counters for the settings-page health block.
- Module state: ``get_policy()``, ``refresh_policy(data)``, ``get_cache()``,
  ``note_stream_speed(mbps)``, ``snapshot()``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Optional, Tuple

MiB = 1024 * 1024

# Hard ceiling for readahead depth in chunks, whatever the settings say:
# at the protocol's 1 MiB chunk size this bounds one stream's queue memory.
MAX_READAHEAD_CHUNKS = 128

PROFILES: Dict[str, Dict[str, int]] = {
    # workers = max concurrent chunk fetches across the whole client pool
    # readahead_mb = queue depth kept ahead of the player
    # cache_mb = shared chunk-cache budget (0 disables the cache)
    # per_client_cap = max concurrent fetches on any single client
    "conservative": {"workers": 2, "readahead_mb": 8, "cache_mb": 16, "per_client_cap": 2},
    "balanced": {"workers": 4, "readahead_mb": 16, "cache_mb": 48, "per_client_cap": 4},
    "aggressive": {"workers": 8, "readahead_mb": 32, "cache_mb": 128, "per_client_cap": 6},
}

DEFAULT_PROFILE = "balanced"
DEFAULT_STALL_TIMEOUT_S = 90
DEFAULT_MAX_FLOOD_WAIT_S = 300
DEFAULT_DISK_CACHE_MB = 512

# Clamp ranges for every numeric setting (the settings API validates with
# the same ranges and rejects non-integer input before it reaches here).
RANGES = {
    "buffer_workers": (1, 16),
    "buffer_readahead_mb": (2, 256),
    "buffer_cache_mb": (0, 1024),
    "buffer_stall_timeout": (15, 600),
    "per_client_cap": (1, 8),
    "max_flood_wait_s": (0, 3600),
    "buffer_disk_cache_mb": (0, 8192),
}


def _as_int(value: Any, default: int) -> int:
    """Best-effort int for runtime policy building (never raises).

    Accepts ints, integral floats and integer strings; anything else
    (including bools, which are ints in Python but never a real setting)
    falls back to ``default``. Strict rejection lives in the settings API.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clamp_key(data: Dict[str, Any], key: str, default: int) -> int:
    low, high = RANGES[key]
    return _clamp(_as_int(data.get(key), default), low, high)


#----- Policy


@dataclass(frozen=True)
class BufferPolicy:
    """Immutable effective configuration for the buffer engine."""

    enabled: bool = True
    profile: str = DEFAULT_PROFILE
    adaptive: bool = True
    workers: int = 4
    readahead_mb: int = 16
    cache_mb: int = 48
    disk_cache_mb: int = DEFAULT_DISK_CACHE_MB
    seek_burst: bool = True
    stall_timeout_s: int = DEFAULT_STALL_TIMEOUT_S
    per_client_cap: int = 4
    max_flood_wait_s: int = DEFAULT_MAX_FLOOD_WAIT_S

    def readahead_chunks(self, chunk_size: int) -> int:
        """Queue depth in chunks for a given chunk size (at least 1)."""
        if chunk_size <= 0:
            return 1
        chunks = (self.readahead_mb * MiB) // chunk_size
        return max(1, min(MAX_READAHEAD_CHUNKS, int(chunks)))

    def cache_budget_bytes(self) -> int:
        return self.cache_mb * MiB

    def disk_cache_bytes(self) -> int:
        return self.disk_cache_mb * MiB

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile": self.profile,
            "adaptive": self.adaptive,
            "workers": self.workers,
            "readahead_mb": self.readahead_mb,
            "cache_mb": self.cache_mb,
            "disk_cache_mb": self.disk_cache_mb,
            "seek_burst": self.seek_burst,
            "stall_timeout_s": self.stall_timeout_s,
            "per_client_cap": self.per_client_cap,
            "max_flood_wait_s": self.max_flood_wait_s,
        }


def build_policy(data: Optional[Dict[str, Any]] = None) -> BufferPolicy:
    """Resolve a ``BufferPolicy`` from raw settings data.

    A named profile supplies workers/readahead/cache/per-client values;
    ``custom`` (or an unknown profile, treated as custom) reads them from
    the individual settings keys instead. Stall timeout and the flood-wait
    budget always come from their own keys. Everything is clamped.
    """
    data = data or {}

    profile = str(data.get("buffer_profile") or DEFAULT_PROFILE).strip().lower()
    if profile not in PROFILES and profile != "custom":
        profile = "custom"

    if profile in PROFILES:
        base = PROFILES[profile]
        workers = base["workers"]
        readahead_mb = base["readahead_mb"]
        cache_mb = base["cache_mb"]
        per_client_cap = base["per_client_cap"]
    else:
        defaults = PROFILES[DEFAULT_PROFILE]
        workers = _clamp_key(data, "buffer_workers", defaults["workers"])
        readahead_mb = _clamp_key(data, "buffer_readahead_mb", defaults["readahead_mb"])
        cache_mb = _clamp_key(data, "buffer_cache_mb", defaults["cache_mb"])
        per_client_cap = _clamp_key(data, "per_client_cap", defaults["per_client_cap"])

    return BufferPolicy(
        enabled=bool(data.get("buffer_enabled", True)),
        profile=profile,
        adaptive=bool(data.get("buffer_adaptive", True)),
        workers=workers,
        readahead_mb=readahead_mb,
        cache_mb=cache_mb,
        disk_cache_mb=_clamp_key(data, "buffer_disk_cache_mb", DEFAULT_DISK_CACHE_MB),
        seek_burst=bool(data.get("buffer_seek_burst", True)),
        stall_timeout_s=_clamp_key(data, "buffer_stall_timeout", DEFAULT_STALL_TIMEOUT_S),
        per_client_cap=per_client_cap,
        max_flood_wait_s=_clamp_key(data, "max_flood_wait_s", DEFAULT_MAX_FLOOD_WAIT_S),
    )


#----- Adaptive window (per-stream AIMD controller)


class AdaptiveWindow:
    """Concurrency controller for one stream.

    The window is the number of chunk fetches allowed in flight. It starts
    at ``initial`` (the policy's workers, pre-scaled by the speed tracker),
    grows by 1 after every full window of consecutive successes, and halves
    on a flood or a stall — classic AIMD, so a healthy link keeps the
    owner's current speed while a penalised link backs off fast. With
    ``adaptive=False`` the window never moves.
    """

    def __init__(self, initial: int, maximum: int, adaptive: bool = True) -> None:
        self._maximum = max(1, int(maximum))
        self._window = max(1, min(self._maximum, int(initial)))
        self._adaptive = bool(adaptive)
        self._successes = 0

    @property
    def window(self) -> int:
        return self._window

    @property
    def maximum(self) -> int:
        return self._maximum

    def note_success(self) -> None:
        if not self._adaptive:
            return
        self._successes += 1
        if self._successes >= self._window:
            self._successes = 0
            self._window = min(self._maximum, self._window + 1)

    def note_flood(self) -> None:
        self._back_off()

    def note_stall(self) -> None:
        self._back_off()

    def _back_off(self) -> None:
        if not self._adaptive:
            return
        self._successes = 0
        self._window = max(1, self._window // 2)


#----- Speed tracker (gated EWMA across streams)


class SpeedTracker:
    """EWMA of finished-stream throughput, with outlier gating.

    Samples that are non-positive, non-finite, or absurd (> 2 Gbps for a
    Telegram fetch) are rejected, so one broken measurement can never
    poison the estimate that seeds new streams' windows.
    """

    _MAX_PLAUSIBLE_MBPS = 2000.0
    _REFERENCE_MBPS = 8.0  # ewma == reference -> multiplier 1.0

    def __init__(self, alpha: float = 0.3) -> None:
        self._alpha = alpha
        self._ewma: Optional[float] = None
        self._samples = 0

    def observe(self, mbps: float) -> bool:
        """Record a sample; returns True when it was accepted."""
        try:
            value = float(mbps)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0 or value > self._MAX_PLAUSIBLE_MBPS:
            return False
        if self._ewma is None:
            self._ewma = value
        else:
            self._ewma = self._alpha * value + (1 - self._alpha) * self._ewma
        self._samples += 1
        return True

    def mbps(self) -> float:
        return self._ewma or 0.0

    def samples(self) -> int:
        return self._samples

    def multiplier(self) -> float:
        """Scale for a new stream's initial window, in [0.5, 2.0]."""
        if self._ewma is None:
            return 1.0
        return max(0.5, min(2.0, self._ewma / self._REFERENCE_MBPS))


#----- Per-(client, DC) speed table


class DcSpeedTable:
    """Gated EWMA of chunk-fetch throughput per (client, data centre).

    A client fast to one Telegram DC can be slow to another; the pool
    picker consults this table to break ties toward the client that is
    fastest to the DC the current file actually lives on. Same outlier
    gating as SpeedTracker, so a broken sample never poisons a cell.
    """

    _MAX_PLAUSIBLE_MBPS = 2000.0

    def __init__(self, alpha: float = 0.3) -> None:
        self._alpha = alpha
        self._ewma: Dict[Tuple[int, int], float] = {}

    def observe(self, client_index: int, dc_id: int, mbps: float) -> bool:
        """Record one fetch's throughput; True when accepted."""
        try:
            value = float(mbps)
            key = (int(client_index), int(dc_id))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0 or value > self._MAX_PLAUSIBLE_MBPS:
            return False
        current = self._ewma.get(key)
        self._ewma[key] = value if current is None else (
            self._alpha * value + (1 - self._alpha) * current)
        return True

    def mbps(self, client_index: int, dc_id: int) -> float:
        try:
            return self._ewma.get((int(client_index), int(dc_id)), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def entries(self) -> int:
        return len(self._ewma)


#----- Chunk cache (byte-budgeted LRU, optional disk spill tier)

CacheKey = Tuple[Hashable, int, Hashable]  # (media_id, chunk_size, position)


class DiskTier:
    """Best-effort disk spill tier for the chunk cache.

    One file per chunk under ``root``, named by a hash of the cache key,
    plus a JSON manifest (name -> [size, last_touch]) that is verified
    against the real files on load: missing or size-mismatched entries
    are dropped silently. Every operation swallows its own errors — a
    disk problem degrades the cache to memory-only, never breaks a
    stream. Budget 0 disables the tier (and evicts everything).
    """

    _MANIFEST = "manifest.json"

    def __init__(self, root: str, budget_bytes: int = 0) -> None:
        self._root = root
        self._budget = max(0, int(budget_bytes))
        self._index: Dict[str, Tuple[int, float]] = {}
        self._size = 0
        self._loaded = False
        self._hits = 0
        self._writes = 0

    @staticmethod
    def _name_for(key: CacheKey) -> str:
        return hashlib.sha1(repr(key).encode("utf-8")).hexdigest()

    def _path(self, name: str) -> str:
        return os.path.join(self._root, name)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path(self._MANIFEST), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for name, meta in raw.items():
                size, touched = int(meta[0]), float(meta[1])
                if os.path.isfile(self._path(name)) and os.path.getsize(self._path(name)) == size:
                    self._index[name] = (size, touched)
                    self._size += size
        except Exception:
            self._index = {}
            self._size = 0

    def _save(self) -> None:
        try:
            os.makedirs(self._root, exist_ok=True)
            tmp = self._path(self._MANIFEST + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({n: [s, t] for n, (s, t) in self._index.items()}, fh)
            os.replace(tmp, self._path(self._MANIFEST))
        except Exception:
            pass

    def _drop(self, name: str) -> None:
        meta = self._index.pop(name, None)
        if meta is not None:
            self._size -= meta[0]
        try:
            os.remove(self._path(name))
        except Exception:
            pass

    def _evict(self) -> None:
        while self._size > self._budget and self._index:
            lru = min(self._index, key=lambda n: self._index[n][1])
            self._drop(lru)

    def get(self, key: CacheKey) -> Optional[bytes]:
        if self._budget <= 0:
            return None
        self._ensure_loaded()
        name = self._name_for(key)
        meta = self._index.get(name)
        if meta is None:
            return None
        try:
            with open(self._path(name), "rb") as fh:
                data = fh.read()
            if len(data) != meta[0]:
                raise ValueError("size mismatch")
        except Exception:
            self._drop(name)
            self._save()
            return None
        self._index[name] = (meta[0], time.time())
        self._hits += 1
        return data

    def put(self, key: CacheKey, data: bytes) -> None:
        if self._budget <= 0 or not data or len(data) > self._budget:
            return
        self._ensure_loaded()
        name = self._name_for(key)
        try:
            os.makedirs(self._root, exist_ok=True)
            tmp = self._path(name + ".tmp")
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, self._path(name))
        except Exception:
            return
        old = self._index.get(name)
        if old is not None:
            self._size -= old[0]
        self._index[name] = (len(data), time.time())
        self._size += len(data)
        self._writes += 1
        self._evict()
        self._save()

    def resize(self, budget_bytes: int) -> None:
        self._budget = max(0, int(budget_bytes))
        self._ensure_loaded()
        self._evict()
        self._save()

    def clear(self) -> None:
        self._ensure_loaded()
        for name in list(self._index):
            self._drop(name)
        self._save()

    def stats(self) -> Dict[str, Any]:
        return {
            "budget_bytes": self._budget,
            "size_bytes": self._size,
            "entries": len(self._index),
            "hits": self._hits,
            "writes": self._writes,
        }


class ChunkCache:
    """LRU cache of fetched chunks, bounded by a byte budget.

    A chunk larger than the whole budget is never stored. Budget 0 disables
    storage entirely. Single-event-loop use; no locking by design.

    With a DiskTier attached, memory evictions spill the evicted chunk to
    disk instead of dropping it, and a memory miss consults the disk tier
    (promoting the chunk back into memory) — so the effective cache
    survives restarts while the hot set stays in RAM.
    """

    def __init__(self, budget_bytes: int = 0, disk: Optional[DiskTier] = None) -> None:
        self._budget = max(0, int(budget_bytes))
        self._entries: "OrderedDict[CacheKey, bytes]" = OrderedDict()
        self._size = 0
        self._hits = 0
        self._misses = 0
        self._disk = disk

    @property
    def budget_bytes(self) -> int:
        return self._budget

    def get(self, key: CacheKey) -> Optional[bytes]:
        data = self._entries.get(key)
        if data is not None:
            self._entries.move_to_end(key)
            self._hits += 1
            return data
        if self._disk is not None:
            data = self._disk.get(key)
            if data is not None:
                self._hits += 1
                self._insert(key, data, spill=False)
                return data
        self._misses += 1
        return None

    def put(self, key: CacheKey, data: bytes) -> None:
        if self._budget <= 0 or not data or len(data) > self._budget:
            return
        self._insert(key, data, spill=True)

    def _insert(self, key: CacheKey, data: bytes, spill: bool) -> None:
        existing = self._entries.pop(key, None)
        if existing is not None:
            self._size -= len(existing)
        self._entries[key] = data
        self._size += len(data)
        self._evict(spill=spill)

    def resize(self, budget_bytes: int, disk_budget_bytes: Optional[int] = None) -> None:
        self._budget = max(0, int(budget_bytes))
        self._evict(spill=True)
        if disk_budget_bytes is not None and self._disk is not None:
            self._disk.resize(disk_budget_bytes)

    def clear(self) -> None:
        self._entries.clear()
        self._size = 0
        if self._disk is not None:
            self._disk.clear()

    def _evict(self, spill: bool = True) -> None:
        while self._size > self._budget and self._entries:
            key, data = self._entries.popitem(last=False)
            self._size -= len(data)
            if spill and self._disk is not None:
                self._disk.put(key, data)

    def stats(self) -> Dict[str, Any]:
        return {
            "budget_bytes": self._budget,
            "size_bytes": self._size,
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "disk": self._disk.stats() if self._disk is not None else None,
        }


def cache_key(media_id: Any, chunk_size: int, position: Hashable) -> Optional[CacheKey]:
    """Build a cache key, or None when the media id is unusable.

    ``position`` identifies which bytes the chunk holds: the caller passes
    ("chunk", index) for chunk-aligned fetches (so the same chunk reached
    from any stream offset shares one entry) and ("offset", byte_offset)
    for unaligned fetches, whose bytes are offset-specific.
    """
    if media_id is None:
        return None
    try:
        return (media_id, int(chunk_size), position)
    except (TypeError, ValueError):
        return None


#----- Flood budget (per stream, shared by all its workers)


class FloodBudget:
    """Shared FloodWait accounting for one stream.

    Every worker of a stream charges its waits against one budget, so N
    workers can never multiply a flood penalty N times. When the budget is
    exhausted the offending client is *shed* — it receives no more chunks
    for this stream — instead of blocking. Penalties decay slowly with
    successful fetches so a client that recovers becomes useful again.
    """

    _SUCCESSES_PER_PENALTY_DECAY = 16

    def __init__(self, max_wait_s: int) -> None:
        self._max_wait = max(0, int(max_wait_s))
        self._spent = 0.0
        self._penalties: Dict[int, int] = {}
        self._successes: Dict[int, int] = {}
        self._shed: set = set()
        self.floods_seen = 0

    def register_flood(self, client_index: int, wait_s: float) -> bool:
        """Record a FloodWait for a client.

        Returns True when the stream may sleep ``wait_s`` (charged against
        the budget); False when the budget is exhausted and the caller must
        shed the client instead of waiting.
        """
        self.floods_seen += 1
        self._penalties[client_index] = self._penalties.get(client_index, 0) + 1
        if self._max_wait <= 0:
            return False
        if self._spent + wait_s > self._max_wait:
            return False
        self._spent += wait_s
        return True

    def record_success(self, client_index: int) -> None:
        count = self._successes.get(client_index, 0) + 1
        self._successes[client_index] = count
        if count % self._SUCCESSES_PER_PENALTY_DECAY == 0:
            penalty = self._penalties.get(client_index, 0)
            if penalty > 0:
                self._penalties[client_index] = penalty - 1

    def penalty(self, client_index: int) -> int:
        return self._penalties.get(client_index, 0)

    def shed(self, client_index: int) -> None:
        self._shed.add(client_index)

    def is_shed(self, client_index: int) -> bool:
        return client_index in self._shed

    def shed_clients(self) -> Tuple[int, ...]:
        return tuple(sorted(self._shed))

    def spent_s(self) -> float:
        return self._spent


#----- Flood alerter (aggregated owner alerts, opt-in via a registered sender)


class FloodAlerter:
    """Aggregates flood events so an alert listener is not spammed.

    Only waits >= ``min_wait_s`` qualify (smaller waits are routine).
    The first qualifying event after a quiet period is returned for
    immediate delivery; events inside the batch window accumulate and
    are flushed as one summary by the first event after the window.
    ``note`` returns the list of event dicts to publish now (0, 1 or 2).
    Delivery itself is the registered sender's job (see below), so
    this class stays synchronous and testable.
    """

    def __init__(self, min_wait_s: float = 10.0, batch_window_s: float = 600.0) -> None:
        self._min_wait = float(min_wait_s)
        self._window = float(batch_window_s)
        self._last_sent = 0.0
        self._pending: list = []

    def note(self, client_index: int, wait_s: float, title: str = "",
             now: Optional[float] = None) -> list:
        if wait_s < self._min_wait:
            return []
        now = time.time() if now is None else float(now)
        event = {
            "kind": "flood",
            "client_index": int(client_index),
            "wait_s": round(float(wait_s), 1),
            "title": title or "",
        }
        if now - self._last_sent >= self._window:
            out = []
            if self._pending:
                out.append(self._summary())
                self._pending = []
            out.append(event)
            self._last_sent = now
            return out
        self._pending.append(event)
        return []

    def _summary(self) -> Dict[str, Any]:
        return {
            "kind": "flood_summary",
            "count": len(self._pending),
            "max_wait_s": max(e["wait_s"] for e in self._pending),
            "clients": sorted({e["client_index"] for e in self._pending}),
        }

    def pending_count(self) -> int:
        return len(self._pending)


_FLOOD_ALERTER = FloodAlerter()
_FLOOD_SENDER = None


def set_flood_sender(fn) -> None:
    """Register the async callable that delivers flood alert events.

    Registered by a bot plugin at startup; with no sender registered,
    note_flood still aggregates but publish_flood_event drops events.
    """
    global _FLOOD_SENDER
    _FLOOD_SENDER = fn


def get_flood_sender():
    return _FLOOD_SENDER


def note_flood(client_index: int, wait_s: float, title: str = "") -> list:
    """Feed one flood event; returns event dicts to publish now."""
    return _FLOOD_ALERTER.note(client_index, wait_s, title)


async def publish_flood_event(event: Dict[str, Any]) -> None:
    """Deliver one alert event via the registered sender (best-effort)."""
    fn = _FLOOD_SENDER
    if fn is None:
        return
    try:
        await fn(event)
    except Exception:
        pass


#----- Metrics


@dataclass
class BufferMetrics:
    """Process-wide counters, surfaced on the settings page."""

    chunks_fetched: int = 0
    chunks_failed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    floods_seen: int = 0
    workers_shed: int = 0
    stall_timeouts: int = 0
    streams_completed: int = 0
    streams_failed: int = 0
    _started: float = field(default_factory=time.time, repr=False)

    def incr(self, name: str, amount: int = 1) -> None:
        if name not in type(self).__dataclass_fields__ or name.startswith("_"):
            raise ValueError(f"unknown buffer metric: {name!r}")
        setattr(self, name, int(getattr(self, name)) + amount)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "chunks_fetched": self.chunks_fetched,
            "chunks_failed": self.chunks_failed,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "floods_seen": self.floods_seen,
            "workers_shed": self.workers_shed,
            "stall_timeouts": self.stall_timeouts,
            "streams_completed": self.streams_completed,
            "streams_failed": self.streams_failed,
            "uptime_s": int(time.time() - self._started),
        }


#----- Module state: current policy, shared cache, speed tracker, metrics

def _default_disk_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), ".chunk_cache")


_POLICY: BufferPolicy = build_policy(None)
_CACHE: ChunkCache = ChunkCache(
    _POLICY.cache_budget_bytes(),
    disk=DiskTier(_default_disk_root(), _POLICY.disk_cache_bytes()),
)
_SPEED: SpeedTracker = SpeedTracker()
_DC_SPEED: DcSpeedTable = DcSpeedTable()
METRICS = BufferMetrics()


def get_policy() -> BufferPolicy:
    return _POLICY


def refresh_policy(data: Optional[Dict[str, Any]] = None) -> BufferPolicy:
    """Rebuild the current policy from settings data and resize the cache."""
    global _POLICY
    _POLICY = build_policy(data)
    _CACHE.resize(_POLICY.cache_budget_bytes(), _POLICY.disk_cache_bytes())
    return _POLICY


def get_cache() -> ChunkCache:
    return _CACHE


def get_speed_tracker() -> SpeedTracker:
    return _SPEED


def get_dc_speed_table() -> DcSpeedTable:
    return _DC_SPEED


def note_dc_speed(client_index: int, dc_id: int, mbps: float) -> bool:
    """Feed one chunk fetch's throughput to the (client, DC) table."""
    return _DC_SPEED.observe(client_index, dc_id, mbps)


def note_stream_speed(mbps: float) -> bool:
    """Feed one finished stream's average throughput to the tracker."""
    return _SPEED.observe(mbps)


def snapshot() -> Dict[str, Any]:
    """Everything the settings page health block renders."""
    return {
        "policy": _POLICY.to_dict(),
        "speed_mbps": round(_SPEED.mbps(), 3),
        "speed_samples": _SPEED.samples(),
        "dc_speed_entries": _DC_SPEED.entries(),
        "cache": _CACHE.stats(),
        "metrics": METRICS.snapshot(),
    }
