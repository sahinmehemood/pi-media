"""Centralized FloodWait handling for every Telegram client in the project.

Problem before this module: ``try/except FloodWait`` + regex parsing of the
wait duration was duplicated across 9+ files (announcer, global_search,
link_checker, scan_manager, session_auth, skip_channel, task_manager,
api_routes, custom_dl).  One logical concern, nine edit sites — Shotgun Surgery.

Seam: this module is the single place that decides what to do when Telegram
says "slow down".  Every caller crosses the same small interface:

    decision = await handler.handle(error, client)
    # client can be a pyrogram Client object or an int client_index
    # decision.abort / decision.wait_seconds / decision.reason

    handler.record_success(client_index)   # decay penalty after a good call
    penalty = handler.penalty(client_index)  # workers to shed for this client

Everything behind the seam — parsing wait seconds from the error, tracking
per-client consecutive-flood counts, computing the worker-shedding penalty,
adding jitter, enforcing max retries, resolving client indices — lives inside
the implementation.

Retry helper
------------
Most call sites only need "call this coroutine, and if Telegram floods us,
wait and try again".  ``run_with_flood_retry`` gives every caller the *same*
bounded, cancellation-safe retry loop instead of each file rolling its own
(one-shot retry, bare ``sleep(e.value)``, or an unbounded ``while True``):

    result = await run_with_flood_retry(StreamBot, client.send_message, chat, "hi")

* FloodWait  -> central decision: wait (with jitter) and retry, or abort.
* other exceptions -> propagate immediately to the caller.
* success    -> ``record_success`` decays the client penalty.

The loop is bounded by ``FloodWaitHandler.max_retries`` and by
``FloodWaitHandler.max_wait``: a FloodWait longer than ``max_wait`` aborts
instead of blocking a coroutine for hours (Telegram can ask for 22h+).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar, Union

from pyrogram.errors import FloodWait

logger = logging.getLogger("flood_wait")

#----- Client index conventions --------------------------------------------
# -1 means the StreamBot primary client (it is not registered in multi_clients).
PRIMARY_CLIENT = -1

#----- Default ceiling for a single flood wait ------------------------------
# Telegram can return waits of hours (e.g. 22h for a hard flood).  Blocking a
# web request or a scan worker for that long is never useful: the operation
# will be stale by the time the wait ends.  Callers get a clean abort instead.
DEFAULT_MAX_WAIT_SECONDS = 300.0

_T = TypeVar("_T")


@dataclass(frozen=True)
class FloodWaitDecision:
    """Outcome of asking the handler what to do about a flood error."""

    abort: bool          # True = stop trying entirely
    wait_seconds: float  # how long to sleep before retrying (0 if no wait)
    reason: str          # human-readable line for logs


class FloodWaitHandler:
    """Central flood-wait handler with per-client penalty tracking.

    Interface is intentionally small: ``handle``, ``record_success``,
    ``penalty``, ``client_index_of``.  All complexity (string parsing,
    jitter, backoff, penalty decay, client-index resolution) is hidden inside.
    """

    def __init__(
        self,
        max_retries: int = 5,
        max_penalty: int = 4,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        self.max_retries = max_retries
        self.max_penalty = max_penalty
        self.max_wait = float(max_wait)
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
        client: Union[int, object],
    ) -> FloodWaitDecision:
        """Inspect a flood-wait error and return a decision.

        ``client`` may be an int client_index or a pyrogram Client object.
        When a Client object is passed, its index is resolved automatically
        (see ``client_index_of``).

        * Parses wait seconds from pyrogram's ``FloodWait.value`` or from the
          raw error string.
        * Tracks consecutive floods per client; aborts past ``max_retries``.
        * Aborts when a single wait exceeds ``max_wait`` (never block a
          coroutine for hours on a stale operation).
        * Raises the per-client penalty (worker shedding) on each flood.
        * Adds increasing jitter per attempt so retries don't thundering-herd.
        """
        client_index = self.client_index_of(client)
        wait = self._parse_wait(error)

        if wait is None:
            return FloodWaitDecision(
                abort=True,
                wait_seconds=0.0,
                reason=f"unrecognized error for client {client_index}: {error}",
            )

        self._record_flood(client_index)

        if self._flood_counts[client_index] > self.max_retries:
            return FloodWaitDecision(
                abort=True,
                wait_seconds=0.0,
                reason=(
                    f"max flood retries ({self.max_retries}) exceeded "
                    f"for client {client_index}"
                ),
            )

        if self.max_wait > 0 and wait > self.max_wait:
            return FloodWaitDecision(
                abort=True,
                wait_seconds=0.0,
                reason=(
                    f"FloodWait {wait:.0f}s for client {client_index} exceeds "
                    f"max_wait {self.max_wait:.0f}s — aborting instead of blocking"
                ),
            )

        self._shed_worker(client_index)

        attempt = self._flood_counts[client_index]
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
            abort=False,
            wait_seconds=wait_total,
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

    def client_index_of(self, client: Union[int, object]) -> int:
        """Resolve a client to its index.

        Accepts an int (returned as-is) or a pyrogram Client object (looked
        up in the shared registry; falls back to PRIMARY_CLIENT = -1, i.e.
        the StreamBot primary client).
        """
        if isinstance(client, int):
            return client
        from Backend.pyrofork.bot import multi_clients

        for idx, c in multi_clients.items():
            if c is client:
                return idx
        return PRIMARY_CLIENT

    def _record_flood(self, client_index: int) -> None:
        """Increment the consecutive-flood count for the client."""
        self._flood_counts[client_index] = self._flood_counts.get(client_index, 0) + 1

    def _shed_worker(self, client_index: int) -> None:
        """Shed one worker for the client if under the max penalty."""
        if self._penalties.get(client_index, 0) < self.max_penalty:
            self._penalties[client_index] = self._penalties.get(client_index, 0) + 1

    def note_flood_wait(self, client_index: int) -> None:
        """Record a flood without the full decision flow.

        Increments the consecutive-flood count and sheds one worker for the
        client.  Used by callers that already parsed the error themselves.
        """
        self._record_flood(client_index)
        self._shed_worker(client_index)
        logger.warning(
            "FloodWait penalty for client=%s -> %d",
            client_index,
            self._penalties[client_index],
        )

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
flood_handler = FloodWaitHandler()


async def run_with_flood_retry(
    client: Union[int, object],
    fn: Callable[..., Awaitable[_T]],
    *args,
    attempts: Optional[int] = None,
    what: str = "Telegram call",
    **kwargs,
) -> _T:
    """Call ``fn(*args, **kwargs)`` with centralized FloodWait retry.

    The single retry loop every caller should use instead of rolling its own:

    * ``FloodWait`` -> ``flood_handler.handle`` decides wait-vs-abort; on wait
      we sleep (with jitter) and retry the same call.
    * Any other exception -> propagates to the caller immediately.
    * Success -> ``flood_handler.record_success`` decays the client penalty.

    Bounded by ``attempts`` (defaults to ``handler.max_retries + 1``) and by
    ``handler.max_wait``, so it can never loop forever or block for hours.
    Raises the last ``FloodWait`` when attempts are exhausted.
    """
    client_index = flood_handler.client_index_of(client)
    max_attempts = attempts if attempts is not None else flood_handler.max_retries + 1
    max_attempts = max(1, int(max_attempts))

    last_error: Optional[FloodWait] = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn(*args, **kwargs)
        except FloodWait as e:
            decision = await flood_handler.handle(e, client_index)
            if decision.abort:
                logger.error("%s aborted after FloodWait: %s", what, decision.reason)
                raise
            logger.warning(
                "%s FloodWait (attempt %d/%d): %s — retrying",
                what, attempt, max_attempts, decision.reason,
            )
            last_error = e
            await asyncio.sleep(decision.wait_seconds)
            continue

        flood_handler.record_success(client_index)
        return result

    # Exhausted attempts without a hard abort (e.g. max_wait=0 custom handler).
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{what}: no attempt was made")
