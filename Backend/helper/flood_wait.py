"""Centralized FloodWait handling for every Telegram client in the project.

Problem before this module: ``try/except FloodWait`` + regex parsing of the
wait duration was duplicated across 9+ files (announcer, global_search,
link_checker, scan_manager, session_auth, skip_channel, task_manager,
api_routes, custom_dl).  One logical concern, nine edit sites — Shotgun Surgery.

Seam: this module is the single place that decides what to do when Telegram
says "slow down".  Every caller crosses the same small interface:

    decision = await handler.handle(error, client_index)
    # decision.retry / decision.abort / decision.wait_seconds

    handler.record_success(client_index)   # decay penalty after a good call
    penalty = handler.penalty(client_index)  # workers to shed for this client

Everything behind the seam — parsing wait seconds from the error, tracking
per-client consecutive-flood counts, computing the worker-shedding penalty,
adding jitter, enforcing max retries — lives inside the implementation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from pyrogram.errors import FloodWait

logger = logging.getLogger("flood_wait")


@dataclass(frozen=True)
class FloodWaitDecision:
    """Outcome of asking the handler what to do about a flood error."""

    retry: bool          # True = retry after waiting, False = give up
    wait_seconds: float  # how long to sleep before retrying (0 if no wait)
    abort: bool          # True = stop trying entirely
    reason: str          # human-readable line for logs


class FloodWaitHandler:
    """Central flood-wait handler with per-client penalty tracking.

    Interface is intentionally small: ``handle``, ``record_success``,
    ``penalty``, ``reset``, ``is_flood``.  All complexity (string parsing,
    jitter, backoff, penalty decay) is hidden inside.
    """

    def __init__(self, max_retries: int = 5, max_penalty: int = 4) -> None:
        self.max_retries = max_retries
        self.max_penalty = max_penalty
        # client_index -> how many workers to shed (0..max_penalty)
        self._penalties: dict[int, int] = {}
        # client_index -> consecutive flood-wait count
        self._flood_counts: dict[int, int] = {}
        # client_index -> unix time of last successful call
        self._last_success: dict[int, float] = {}

    #----- Main seam --------------------------------------------------------

    async def handle(
        self,
        error: Exception,
        client_index: int,
        attempt: int = 0,
    ) -> FloodWaitDecision:
        """Inspect a flood-wait error and return a decision.

        * Parses wait seconds from pyrogram's ``FloodWait.value`` or from the
          raw error string.
        * Tracks consecutive floods per client; aborts past ``max_retries``.
        * Raises the per-client penalty (worker shedding) on each flood.
        * Adds increasing jitter per attempt so retries don't thundering-herd.
        """
        wait = self._parse_wait(error)

        if wait is None:
            return FloodWaitDecision(
                retry=False,
                wait_seconds=0.0,
                abort=True,
                reason=f"unrecognized error for client {client_index}: {error}",
            )

        self._flood_counts[client_index] = self._flood_counts.get(client_index, 0) + 1

        if self._flood_counts[client_index] > self.max_retries:
            return FloodWaitDecision(
                retry=False,
                wait_seconds=0.0,
                abort=True,
                reason=(
                    f"max flood retries ({self.max_retries}) exceeded "
                    f"for client {client_index}"
                ),
            )

        # Shed a worker for this client on each consecutive flood.
        if self._penalties.get(client_index, 0) < self.max_penalty:
            self._penalties[client_index] = self._penalties.get(client_index, 0) + 1

        jitter = 0.5 + (attempt * 0.5)
        wait_total = wait + jitter

        logger.warning(
            "FloodWait client=%s wait=%.0fs jitter=%.1fs penalty=%d attempt=%d",
            client_index,
            wait,
            jitter,
            self._penalties[client_index],
            attempt,
        )

        return FloodWaitDecision(
            retry=True,
            wait_seconds=wait_total,
            abort=False,
            reason=(
                f"FloodWait {wait:.0f}s + jitter {jitter:.1f}s "
                f"for client {client_index}"
            ),
        )

    def record_success(self, client_index: int) -> None:
        """Call after a successful Telegram API call.

        Decays the penalty by one and clears the consecutive-flood count so
        a client that recovers gradually gets its workers back.
        """
        if self._penalties.get(client_index, 0) > 0:
            self._penalties[client_index] -= 1
        self._flood_counts.pop(client_index, None)
        self._last_success[client_index] = time.time()

    def penalty(self, client_index: int) -> int:
        """Workers to shed for ``client_index`` (0..max_penalty)."""
        return self._penalties.get(client_index, 0)

    def note_flood_wait(self, client_index: int) -> None:
        """Record a flood without the full decision flow.

        Increments the consecutive-flood count and sheds one worker for the
        client.  Used by callers that already parsed the error themselves.
        """
        self._flood_counts[client_index] = self._flood_counts.get(client_index, 0) + 1
        if self._penalties.get(client_index, 0) < self.max_penalty:
            self._penalties[client_index] = self._penalties.get(client_index, 0) + 1
        logger.warning("FloodWait penalty for client=%s -> %d", client_index, self._penalties[client_index])

    def reset(self, client_index: Optional[int] = None) -> None:
        """Reset penalty/flood state for one client, or all clients."""
        if client_index is None:
            self._penalties.clear()
            self._flood_counts.clear()
            self._last_success.clear()
        else:
            self._penalties.pop(client_index, None)
            self._flood_counts.pop(client_index, None)
            self._last_success.pop(client_index, None)

    def is_flood(self, error: Exception) -> bool:
        """Quick predicate: is this error a flood-wait?"""
        if isinstance(error, FloodWait):
            return True
        return self._parse_wait(error) is not None

    #----- Internals --------------------------------------------------------

    @staticmethod
    def _parse_wait(error: Exception) -> Optional[float]:
        """Extract wait seconds from any flood-style error.

        Tries ``FloodWait.value`` first, then two regex fallbacks on the
        string form.
        """
        value = getattr(error, "value", None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass

        err_str = str(error)
        for pattern in (r"wait of (\d+) second", r"(\d+)\s*second"):
            m = re.search(pattern, err_str, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        return None


#----- Process-wide singleton: one handler for every client ----------------
handler = FloodWaitHandler()


#----- Convenience wrapper -------------------------------------------------

async def with_flood_retry(
    call,
    client_index: int,
    max_attempts: int = 5,
    on_give_up=None,
):
    """Run ``call()`` (a coroutine factory), retrying on FloodWait.

    Usage::

        result = await with_flood_retry(
            lambda: client.send(GetFile(...)),
            client_index=idx,
        )

    The handler is consulted on every flood; if it says abort, ``on_give_up``
    is called (if provided) and the last error is raised.
    """
    last_error: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return await call()
        except FloodWait as e:
            last_error = e
            decision = await handler.handle(e, client_index, attempt)
            if decision.abort:
                break
            await asyncio.sleep(decision.wait_seconds)
        except Exception as e:
            # Non-flood errors propagate immediately; record success so the
            # penalty decays (the call reached Telegram and got an answer).
            handler.record_success(client_index)
            raise
    # If we get here we exhausted retries.
    handler.record_success(client_index)  # stop penalizing after we give up
    if on_give_up is not None:
        try:
            on_give_up(last_error)
        except Exception:
            logger.exception("on_give_up callback failed")
    if last_error is not None:
        raise last_error
    raise RuntimeError("with_flood_retry exited without result or error")
