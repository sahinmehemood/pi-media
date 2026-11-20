"""Librarian re-index bridge — DESIGN §4 seam 2, Phase 5 DISP-3.

The fix engine must never import ``Backend.pyrofork.plugins.receiver``:
that module is a Pyrogram plugin with import-time side effects (it
starts ``process_file`` at import). Instead ``receiver.py`` registers
its ``reindex_one_post`` implementation here at import, and the engine
calls this bridge. Registration happens when the app loads its
plugins — always before the API can serve a fix.

Contract (phase5-plan DISP-3 item 2, pinned so Workstreams A and B
build in parallel against it)::

    reindex_one_post(client, message, *, placement=None,
                     expected_ids=None, series_ids=None,
                     future_timeout=120)
        -> {"state": "indexed" | "duplicate" | "metadata_failed"
                     | "error",
            "ids": (imdb_id, tmdb_id, media_type) | None,
            "filed": {"season": int, "episode": int}
                     | {"absolute": int} | None,
            "error": str | None}

``ids`` is the ``db.get_media_ids_by_part`` triple — the repo's
identity is the triple; no single id exists. ``future_timeout`` is
carried for contract completeness: the engine awaits the bridge call
under ``asyncio.wait_for(..., timeout=120)`` and a timeout is the
caller's ``edited_unindexed`` path, not a seam state; the registered
implementation awaits its completion future directly.

With nothing registered the bridge returns the pinned ``error``
result — an honest error, never an exception into the engine (the
engine maps ``error`` -> ``edit_failed``). Every call returns a
fresh result dict: callers may mutate what they receive.

Stdlib only at top level (the store/gateway import discipline).
"""

from __future__ import annotations

_registered = None


def register(fn) -> None:
    """Store the seam implementation (called by ``receiver.py`` at
    import). ``register(None)`` clears the registration."""
    global _registered
    _registered = fn


def _not_registered_result() -> dict:
    return {"state": "error", "ids": None, "filed": None,
            "error": "re-index seam not registered"}


async def reindex_one_post(client, message, *, placement=None,
                           expected_ids=None, series_ids=None,
                           future_timeout=120) -> dict:
    """Delegate to the registered seam implementation, or return
    the pinned ``error`` result when none is registered."""
    fn = _registered
    if fn is None:
        return _not_registered_result()
    return await fn(client, message, placement=placement,
                    expected_ids=expected_ids, series_ids=series_ids,
                    future_timeout=future_timeout)
