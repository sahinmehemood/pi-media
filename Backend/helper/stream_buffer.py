"""Streaming buffer: read-ahead depth, worker count, shared chunk cache.

Before this module the streamer derived its read-ahead from the number of bot
clients (``ceil(clients / 5)``, clamped 1..5). With a single client that is a
single 1 MiB chunk — roughly one second of 1080p — which cannot absorb jitter
on a mobile link, and there was no cache at all, so every seek re-downloaded
from Telegram.

This module makes the buffer an explicit policy with three knobs:

* read-ahead — how many 1 MiB chunks may sit queued ahead of the HTTP writer
* workers    — how many ``GetFile`` requests may be in flight per stream
* cache      — a byte-capped LRU of recently fetched chunks, shared by every
               stream, so seeks, back-seeks and a second viewer of the same
               file are served from RAM instead of Telegram

Every size is validated against the device's physical RAM (``device_ram_bytes``)
so a bad value can never take the phone down. The phone reports ~5.6 GB, so the
20% budget leaves roughly 1.1 GB of headroom — far above the default profile.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from Backend.logger import LOGGER

MiB = 1024 * 1024

#----- Named profiles: the only choice a normal user has to make
PROFILES: Dict[str, Dict[str, int]] = {
    "conservative": {"readahead_mb": 6, "workers": 2, "cache_mb": 24},
    "balanced": {"readahead_mb": 12, "workers": 3, "cache_mb": 48},
    "aggressive": {"readahead_mb": 24, "workers": 5, "cache_mb": 96},
}
DEFAULT_PROFILE = "balanced"

MIN_READAHEAD_MB = 2
MAX_READAHEAD_MB = 128
MIN_WORKERS = 1
MAX_WORKERS = 8
MAX_CACHE_MB = 512
MIN_STALL_TIMEOUT = 15.0
MAX_STALL_TIMEOUT = 600.0
DEFAULT_STALL_TIMEOUT = 90.0

#----- Never let buffering claim more than this share of physical memory
RAM_BUDGET_SHARE = 0.20

#----- A userbot is a single session; past 4 parallel GetFile it earns FloodWait
USERBOT_MAX_WORKERS = 4

#----- How many FloodWaits in a row may shave one worker off every stream
FLOOD_PENALTY_MAX = 4

_CACHE_KEY = Tuple[int, int, int]


#----- Clamp helpers -------------------------------------------------------

def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def device_ram_bytes() -> int:
    """Physical RAM as reported by the kernel, with a conservative fallback."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 2048 * MiB


def ram_budget_bytes() -> int:
    return max(8 * MiB, int(device_ram_bytes() * RAM_BUDGET_SHARE))


#----- Policy --------------------------------------------------------------

class BufferPolicy:
    """Immutable, already-validated buffer decision for one stream."""

    __slots__ = (
        "enabled", "adaptive", "profile", "readahead_bytes", "workers",
        "cache_bytes", "stall_timeout", "clamped", "max_readahead_bytes",
    )

    def __init__(
        self,
        enabled: bool,
        adaptive: bool,
        profile: str,
        readahead_bytes: int,
        workers: int,
        cache_bytes: int,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
        clamped: bool = False,
        max_readahead_bytes: int = MAX_READAHEAD_MB * MiB,
    ) -> None:
        self.enabled = bool(enabled)
        self.adaptive = bool(adaptive)
        self.profile = profile
        self.readahead_bytes = max(0, int(readahead_bytes))
        self.workers = max(MIN_WORKERS, int(workers))
        self.cache_bytes = max(0, int(cache_bytes))
        self.stall_timeout = float(stall_timeout)
        self.clamped = bool(clamped)
        self.max_readahead_bytes = max(MiB, int(max_readahead_bytes))

    @property
    def readahead_mb(self) -> float:
        return round(self.readahead_bytes / MiB, 2)

    @property
    def cache_mb(self) -> float:
        return round(self.cache_bytes / MiB, 2)

    def depth_chunks(self, chunk_size: int = MiB) -> int:
        """Queue length in chunks, never less than one full chunk."""
        if chunk_size <= 0:
            return 1
        return max(1, math.ceil(self.readahead_bytes / chunk_size))

    def _replace(self, **changes: Any) -> "BufferPolicy":
        values = {
            "enabled": self.enabled,
            "adaptive": self.adaptive,
            "profile": self.profile,
            "readahead_bytes": self.readahead_bytes,
            "workers": self.workers,
            "cache_bytes": self.cache_bytes,
            "stall_timeout": self.stall_timeout,
            "clamped": self.clamped,
            "max_readahead_bytes": self.max_readahead_bytes,
        }
        values.update(changes)
        return BufferPolicy(**values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "adaptive": self.adaptive,
            "profile": self.profile,
            "readahead_mb": self.readahead_mb,
            "workers": self.workers,
            "cache_mb": self.cache_mb,
            "stall_timeout": self.stall_timeout,
            "clamped": self.clamped,
        }


def build_policy(data: Optional[Dict[str, Any]] = None) -> BufferPolicy:
    """Validate raw settings into a policy, applying the device RAM ceiling."""
    if data is None:
        try:
            from Backend.helper.settings_manager import SettingsManager

            data = SettingsManager.current().to_dict()
        except Exception:
            data = {}

    enabled = bool(data.get("buffer_enabled", True))
    adaptive = bool(data.get("buffer_adaptive", True))

    profile = str(data.get("buffer_profile") or DEFAULT_PROFILE).strip().lower()
    if profile not in PROFILES and profile != "custom":
        profile = DEFAULT_PROFILE

    if profile == "custom":
        readahead_mb = _clamp_int(
            data.get("buffer_readahead_mb"),
            PROFILES[DEFAULT_PROFILE]["readahead_mb"],
            MIN_READAHEAD_MB,
            MAX_READAHEAD_MB,
        )
        workers = _clamp_int(
            data.get("buffer_workers"),
            PROFILES[DEFAULT_PROFILE]["workers"],
            MIN_WORKERS,
            MAX_WORKERS,
        )
        cache_mb = _clamp_int(
            data.get("buffer_cache_mb"),
            PROFILES[DEFAULT_PROFILE]["cache_mb"],
            0,
            MAX_CACHE_MB,
        )
    else:
        chosen = PROFILES[profile]
        readahead_mb = chosen["readahead_mb"]
        workers = chosen["workers"]
        cache_mb = chosen["cache_mb"]

    stall_timeout = _clamp_float(
        data.get("buffer_stall_timeout"),
        DEFAULT_STALL_TIMEOUT,
        MIN_STALL_TIMEOUT,
        MAX_STALL_TIMEOUT,
    )

    budget = ram_budget_bytes()
    clamped = False
    requested = readahead_mb * MiB + cache_mb * MiB
    if requested > budget:
        #----- Shrink proportionally, but keep at least one usable chunk
        scale = budget / float(requested)
        readahead_mb = max(MIN_READAHEAD_MB, int(readahead_mb * scale))
        cache_mb = max(0, int(cache_mb * scale))
        clamped = True

    readahead_bytes = readahead_mb * MiB
    max_readahead = max(MiB, min(MAX_READAHEAD_MB * MiB, int(budget * 0.5)))

    policy = BufferPolicy(
        enabled=enabled,
        adaptive=adaptive,
        profile=profile,
        readahead_bytes=readahead_bytes,
        workers=workers,
        cache_bytes=cache_mb * MiB,
        stall_timeout=stall_timeout,
        clamped=clamped,
        max_readahead_bytes=max_readahead,
    )
    if clamped:
        LOGGER.warning(
            "Stream buffer clamped to the device RAM budget: read-ahead %.1f MiB, cache %.1f MiB",
            policy.readahead_mb,
            policy.cache_mb,
        )
    return policy


#----- Speed window (feeds the adaptive read-ahead) ------------------------

class _SpeedWindow:
    """EWMA of measured stream throughput, in Mbps."""

    __slots__ = ("_mbps", "_samples")

    def __init__(self) -> None:
        self._mbps = 0.0
        self._samples = 0

    def observe(self, mbps: float) -> None:
        try:
            value = float(mbps)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        value = min(value, 10_000.0)
        if self._samples == 0:
            self._mbps = value
        else:
            self._mbps = 0.7 * self._mbps + 0.3 * value
        self._samples = min(self._samples + 1, 1000)

    @property
    def mbps(self) -> float:
        return self._mbps

    @property
    def samples(self) -> int:
        return self._samples

    def multiplier(self) -> float:
        """Deepen a slow link, relax a fast one; never trust one sample."""
        if self._samples < 2:
            return 1.0
        if self._mbps < 1.5:
            return 2.0
        if self._mbps < 4.0:
            return 1.5
        if self._mbps > 25.0:
            return 0.75
        return 1.0


#----- Metrics ------------------------------------------------------------

class BufferMetrics:
    """Cheap process-wide counters, exposed on the admin stats endpoint."""

    __slots__ = ("started_ts", "counters")

    _NAMES = (
        "chunks_fetched",
        "chunks_cached_write",
        "chunks_failed",
        "cache_hits",
        "cache_misses",
        "cache_evictions",
        "flood_waits",
        "stall_timeouts",
        "streams_finished",
    )

    def __init__(self) -> None:
        self.started_ts = time.time()
        self.counters: Dict[str, int] = {name: 0 for name in self._NAMES}

    def incr(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    @property
    def uptime_s(self) -> int:
        return int(time.time() - self.started_ts)


#----- Shared chunk cache --------------------------------------------------

class ChunkCache:
    """Byte-capped LRU of full 1 MiB chunks, shared by all streams."""

    __slots__ = ("budget_bytes", "_entries", "_bytes")

    def __init__(self, budget_bytes: int = 0) -> None:
        self.budget_bytes = max(0, int(budget_bytes))
        self._entries: "OrderedDict[_CACHE_KEY, bytes]" = OrderedDict()
        self._bytes = 0

    @property
    def size_bytes(self) -> int:
        return self._bytes

    @property
    def entries(self) -> int:
        return len(self._entries)

    def get(self, key: _CACHE_KEY) -> Optional[bytes]:
        data = self._entries.get(key)
        if data is None:
            METRICS.incr("cache_misses")
            return None
        self._entries.move_to_end(key)
        METRICS.incr("cache_hits")
        return data

    def put(self, key: _CACHE_KEY, data: bytes) -> None:
        if not data or self.budget_bytes <= 0:
            return
        size = len(data)
        if size > self.budget_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= len(previous)
        self._entries[key] = data
        self._bytes += size
        METRICS.incr("chunks_cached_write")
        self._evict()

    def _evict(self) -> None:
        while self._bytes > self.budget_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= len(evicted)
            METRICS.incr("cache_evictions")

    def resize(self, budget_bytes: int) -> None:
        self.budget_bytes = max(0, int(budget_bytes))
        if self.budget_bytes == 0:
            self.clear()
        else:
            self._evict()

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def stats(self) -> Dict[str, Any]:
        hits = METRICS.counters.get("cache_hits", 0)
        misses = METRICS.counters.get("cache_misses", 0)
        total = hits + misses
        return {
            "budget_mb": round(self.budget_bytes / MiB, 1),
            "used_mb": round(self._bytes / MiB, 1),
            "chunks": len(self._entries),
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 3) if total else 0.0,
        }


#----- Process-wide singletons --------------------------------------------

METRICS = BufferMetrics()
SPEED = _SpeedWindow()

_policy: Optional[BufferPolicy] = None
_cache: Optional[ChunkCache] = None
_flood_penalty = 0


def get_policy() -> BufferPolicy:
    global _policy
    if _policy is None:
        _policy = build_policy()
    return _policy


def get_shared_cache(create: bool = True) -> Optional[ChunkCache]:
    global _cache
    if _cache is None and create:
        _cache = ChunkCache(get_policy().cache_bytes)
    return _cache


def refresh_policy() -> BufferPolicy:
    """Rebuild from the current settings and resize the cache in place."""
    global _policy
    _policy = build_policy()
    cache = get_shared_cache(create=False)
    if cache is not None:
        cache.resize(_policy.cache_bytes)
    if _policy.cache_bytes == 0 and cache is not None:
        cache.clear()
    LOGGER.info(
        "Stream buffer: %s profile, read-ahead %.1f MiB, %d worker(s), cache %.1f MiB, adaptive=%s",
        _policy.profile,
        _policy.readahead_mb,
        _policy.workers,
        _policy.cache_mb,
        _policy.adaptive,
    )
    return _policy


def note_flood_wait() -> None:
    """A FloodWait means we asked for too much at once: shed a worker."""
    global _flood_penalty
    METRICS.incr("flood_waits")
    if _flood_penalty < FLOOD_PENALTY_MAX:
        _flood_penalty += 1
        LOGGER.warning("Stream buffer: FloodWait — reducing parallel workers to %d", max(1, get_policy().workers - _flood_penalty))


def note_stall() -> None:
    METRICS.incr("stall_timeouts")


def record_stream_speed(mbps: float) -> None:
    """Feed a finished stream's throughput back into the adaptive window."""
    METRICS.incr("streams_finished")
    global _flood_penalty
    SPEED.observe(mbps)
    if _flood_penalty and SPEED.mbps >= 4.0:
        _flood_penalty = max(0, _flood_penalty - 1)


def stream_policy(userbot: bool = False) -> BufferPolicy:
    """Policy for one new stream: adaptive depth plus the FloodWait penalty."""
    base = get_policy()
    if not base.enabled:
        return base

    readahead = base.readahead_bytes
    if base.adaptive:
        readahead = int(readahead * SPEED.multiplier())
    readahead = max(MIN_READAHEAD_MB * MiB, min(readahead, base.max_readahead_bytes))

    workers = max(MIN_WORKERS, base.workers - _flood_penalty)
    if userbot:
        workers = min(workers, USERBOT_MAX_WORKERS)

    return base._replace(readahead_bytes=readahead, workers=workers)


def cache_key(media_id: Any, chunk_size: int, chunk_index: int) -> Optional[_CACHE_KEY]:
    try:
        return (int(media_id), int(chunk_size), int(chunk_index))
    except (TypeError, ValueError):
        return None


def chunk_index(offset: int, chunk_size: int) -> int:
    """Absolute chunk number for a byte offset.

    Streams start at an arbitrary (chunk-aligned) offset, so the per-stream
    sequence number must never be used as a cache key — two streams of the same
    file starting at different points would otherwise alias onto chunk 0.
    """
    if chunk_size <= 0:
        return 0
    return max(0, int(offset)) // int(chunk_size)


def snapshot() -> Dict[str, Any]:
    """Everything the admin panel needs to explain the current buffer."""
    policy = get_policy()
    effective = stream_policy()
    cache = get_shared_cache(create=False)
    cache_stats = cache.stats() if cache is not None else {
        "budget_mb": 0, "used_mb": 0, "chunks": 0, "hits": 0, "misses": 0, "hit_rate": 0.0,
    }
    return {
        "enabled": policy.enabled,
        "profile": policy.profile,
        "adaptive": policy.adaptive,
        "readahead_mb": policy.readahead_mb,
        "effective_readahead_mb": round(effective.readahead_bytes / MiB, 2),
        "workers": policy.workers,
        "effective_workers": effective.workers,
        "cache_mb": policy.cache_mb,
        "clamped": policy.clamped,
        "ram_budget_mb": round(ram_budget_bytes() / MiB, 1),
        "measured_mbps": round(SPEED.mbps, 2),
        "samples": SPEED.samples,
        "flood_penalty": _flood_penalty,
        "cache": cache_stats,
        "counters": dict(METRICS.counters),
        "uptime_s": METRICS.uptime_s,
    }
