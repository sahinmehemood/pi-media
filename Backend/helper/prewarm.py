"""Pre-Warm engine core — policy, warm planning, budgets.

The brain of the Pre-Warm plugin (Wave 2 Phase B, see
``docs/wave-2/DESIGN.md``). Standard library only, like stream_buffer,
so the whole module is testable without Telegram or the web stack.
The Telegram-facing runtime (triggers, the warm runner) lives in
``Backend/pyrofork/plugins/prewarm_hooks.py`` and drives this core.

What pre-warming does: it fetches the head (and tail) chunks of a
file into the shared chunk cache *before* anyone presses play, so
the stream starts instantly and the first seek is free. It is an
opportunistic background optimisation with hard safety budgets —
it must never compete with a real stream or drain the server.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

MiB = 1024 * 1024
CHUNK_SIZE = 1024 * 1024  # the protocol chunk size media_streamer uses

# head_mb / tail_mb: how much of the file's start/end to cache.
# full_next: warm the next episode (almost) whole instead of head+tail.
# ingest_head: also warm the head of new auth-channel posts.
PROFILES: Dict[str, Dict[str, Any]] = {
    "lite":       {"head_mb": 8,  "tail_mb": 0,  "full_next": False, "ingest_head": False},
    "balanced":   {"head_mb": 24, "tail_mb": 8,  "full_next": False, "ingest_head": True},
    "aggressive": {"head_mb": 48, "tail_mb": 16, "full_next": True,  "ingest_head": True},
    "smart":      {"head_mb": 24, "tail_mb": 8,  "full_next": False, "ingest_head": True},
}

DEFAULT_PROFILE = "smart"

RANGES = {
    "prewarm_mb_per_file": (16, 4096),
    "prewarm_episodes_ahead": (1, 2),
    "prewarm_trigger_pct": (10, 95),
    "prewarm_daily_budget_mb": (0, 102400),
}


def _as_int(value: Any, default: int) -> int:
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


def _clamp_key(data: Dict[str, Any], key: str, default: int) -> int:
    low, high = RANGES[key]
    return max(low, min(high, _as_int(data.get(key), default)))


@dataclass(frozen=True)
class PrewarmPolicy:
    """Immutable effective configuration for the Pre-Warm plugin."""

    enabled: bool = False
    profile: str = DEFAULT_PROFILE
    head_mb: int = 24
    tail_mb: int = 8
    full_next: bool = False
    ingest_head: bool = True
    mb_per_file: int = 256
    episodes_ahead: int = 1
    trigger_pct: int = 70
    daily_budget_mb: int = 2048
    only_charging: bool = False

    @property
    def smart(self) -> bool:
        return self.profile == "smart"


def build_policy(data: Optional[Dict[str, Any]] = None) -> PrewarmPolicy:
    """Resolve a PrewarmPolicy from raw settings data (all clamped)."""
    data = data or {}
    profile = str(data.get("prewarm_profile") or DEFAULT_PROFILE).strip().lower()
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    base = PROFILES[profile]
    return PrewarmPolicy(
        enabled=bool(data.get("prewarm_enabled", False)),
        profile=profile,
        head_mb=base["head_mb"],
        tail_mb=base["tail_mb"],
        full_next=base["full_next"],
        ingest_head=base["ingest_head"],
        mb_per_file=_clamp_key(data, "prewarm_mb_per_file", 256),
        episodes_ahead=_clamp_key(data, "prewarm_episodes_ahead", 1),
        trigger_pct=_clamp_key(data, "prewarm_trigger_pct", 70),
        daily_budget_mb=_clamp_key(data, "prewarm_daily_budget_mb", 2048),
        only_charging=bool(data.get("prewarm_only_charging", False)),
    )


def warm_plan(file_size: int, policy: PrewarmPolicy,
              chunk_size: int = CHUNK_SIZE) -> List[Tuple[int, int]]:
    """Chunk-aligned (offset, part_count) ranges to fetch for a file.

    full_next warms from the start up to the per-file cap; otherwise
    the head range plus — when the file is big enough that head and
    tail don't overlap — a tail range ending exactly at EOF.
    """
    if file_size <= 0 or chunk_size <= 0:
        return []

    def parts(nbytes: int) -> int:
        return max(1, -(-nbytes // chunk_size))

    if policy.full_next:
        return [(0, parts(min(file_size, policy.mb_per_file * MiB)))]

    head_bytes = min(policy.head_mb * MiB, file_size)
    plan = [(0, parts(head_bytes))]
    tail_bytes = policy.tail_mb * MiB
    if tail_bytes and file_size > head_bytes + tail_bytes:
        tail_start = file_size - tail_bytes
        aligned = tail_start - (tail_start % chunk_size)
        plan.append((aligned, parts(file_size - aligned)))
    return plan


def plan_mb(plan: List[Tuple[int, int]], chunk_size: int = CHUNK_SIZE) -> float:
    """Upper-bound MiB a warm plan will fetch (for budget checks)."""
    return sum(count for _, count in plan) * chunk_size / MiB


#----- Charging state (best-effort; the server is a phone)


def charging_state(read: Optional[Callable[[str], str]] = None) -> Optional[bool]:
    """True/False when the battery status is readable, else None.

    Reads /sys/class/power_supply/battery/status. On the Narzo under
    Termux this may be blocked (SELinux) — None means "unknown", and
    callers treat unknown as allowed (a gate that can never open
    would silently disable the feature).
    """
    if read is None:
        def read(path: str) -> str:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    try:
        status = read("/sys/class/power_supply/battery/status").strip().lower()
    except Exception:
        return None
    if status in ("charging", "full"):
        return True
    if status in ("discharging", "not charging"):
        return False
    return None


def mem_available_mb(read: Optional[Callable[[str], str]] = None) -> Optional[int]:
    """MemAvailable from /proc/meminfo in MiB, or None when unreadable."""
    if read is None:
        def read(path: str) -> str:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    try:
        for line in read("/proc/meminfo").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


#----- Planner: queue, dedupe, daily budget


@dataclass
class WarmItem:
    chat_id: int
    msg_id: int
    size: int          # file size in bytes (0 when unknown)
    est_mb: float      # planned fetch size for budget checks
    reason: str = ""   # next-episode | pooled | ingest
    series_key: str = ""  # normalised series id for smart per-series caps

    @property
    def key(self) -> Tuple[int, int]:
        return (self.chat_id, self.msg_id)


class WarmPlanner:
    """Bookkeeping for pending warms. Pure state; no I/O, no clock
    except the day-roll check driven by the caller's ``today``.

    Invariants:
    - A (chat_id, msg_id) is offered at most once per day (queued,
      in-progress or completed all count).
    - The daily budget is a hard stop: an item whose estimate would
      exceed the remaining budget is refused (0 budget = disabled).
    - Smart profile: at most one next-episode warm per series per day.
    """

    def __init__(self) -> None:
        self._queue: Deque[WarmItem] = deque()
        self._seen: Set[Tuple[int, int]] = set()
        self._series_today: Set[str] = set()
        self._used_mb = 0.0
        self._warmed_today = 0
        self._day: Optional[date] = None

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._seen.clear()
            self._series_today.clear()
            self._used_mb = 0.0
            self._warmed_today = 0

    def restore_usage(self, used_mb: float, today: date) -> None:
        """Seed today's spent budget (persisted across restarts)."""
        self._roll(today)
        self._used_mb = max(self._used_mb, float(used_mb))

    def offer(self, item: WarmItem, policy: PrewarmPolicy, today: date) -> bool:
        self._roll(today)
        if not policy.enabled or policy.daily_budget_mb <= 0:
            return False
        if item.key in self._seen:
            return False
        if policy.smart and item.reason == "next-episode" and item.series_key:
            if item.series_key in self._series_today:
                return False
        if self._used_mb + item.est_mb > policy.daily_budget_mb:
            return False
        self._seen.add(item.key)
        if policy.smart and item.reason == "next-episode" and item.series_key:
            self._series_today.add(item.series_key)
        self._queue.append(item)
        return True

    def next(self) -> Optional[WarmItem]:
        if self._queue:
            return self._queue.popleft()
        return None

    def requeue(self, item: WarmItem) -> None:
        self._queue.appendleft(item)

    def complete(self, item: WarmItem, actual_mb: float) -> None:
        self._used_mb += max(0.0, float(actual_mb))
        self._warmed_today += 1

    def stats(self, today: Optional[date] = None) -> Dict[str, Any]:
        if today is not None:
            self._roll(today)
        return {
            "queued": len(self._queue),
            "used_mb_today": round(self._used_mb, 1),
            "warmed_today": self._warmed_today,
        }
