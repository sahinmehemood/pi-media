"""Phase 5 Workstream B tests — the re-index seam (phase5-plan §10.3).

B-1 (this file's first landing): the ``librarian_reindex`` bridge —
unregistered -> the pinned ``error`` result; registered -> delegation
with kwargs intact; fresh result dicts per call. B-2 extends this
file with the receiver seam / handler / coordination tests against
production-shaped doubles (every stubbed name verified against the
real module it stands in for — a generous fake is a failed suite).

The bridge is loaded by file path (the repo harness pattern), so no
app stack is built.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module  # noqa: E402

bridge = load_module("Backend.helper.librarian_reindex",
                     "Backend/helper/librarian_reindex.py")


def _run(coro):
    return asyncio.run(coro)


def _reset_bridge():
    bridge.register(None)


# ------------------------------------------------------------------
# Bridge (DISP-3 items 1–2; plan §3.1)
# ------------------------------------------------------------------

def test_bridge_unregistered_returns_pinned_error():
    _reset_bridge()

    async def go():
        result = await bridge.reindex_one_post(object(), object())
        assert result == {"state": "error", "ids": None, "filed": None,
                          "error": "re-index seam not registered"}

    _run(go())


def test_bridge_unregistered_results_are_fresh_dicts():
    _reset_bridge()

    async def go():
        first = await bridge.reindex_one_post(object(), object())
        second = await bridge.reindex_one_post(object(), object())
        assert first is not second
        first["state"] = "mutated"
        first["error"] = "mutated"
        assert second == {"state": "error", "ids": None, "filed": None,
                          "error": "re-index seam not registered"}

    _run(go())


def test_bridge_delegates_with_kwargs_intact():
    _reset_bridge()
    seen = {}

    async def impl(client, message, *, placement=None, expected_ids=None,
                   series_ids=None, future_timeout=120):
        seen.update(client=client, message=message, placement=placement,
                    expected_ids=expected_ids, series_ids=series_ids,
                    future_timeout=future_timeout)
        return {"state": "indexed", "ids": ("tt1", 42, "tv"),
                "filed": {"season": 0, "episode": 7}, "error": None}

    bridge.register(impl)
    try:
        async def go():
            client, message = object(), object()
            placement = {"season": 0, "episode": 7}
            expected = {"imdb": "tt1", "tmdb": 42}
            series = {"tmdb": 42, "tvdb": 99}
            result = await bridge.reindex_one_post(
                client, message, placement=placement,
                expected_ids=expected, series_ids=series)
            assert result == {"state": "indexed", "ids": ("tt1", 42, "tv"),
                              "filed": {"season": 0, "episode": 7},
                              "error": None}
            assert seen["client"] is client and seen["message"] is message
            assert seen["placement"] is placement
            assert seen["expected_ids"] is expected
            assert seen["series_ids"] is series
            assert seen["future_timeout"] == 120  # DISP-3 default
            await bridge.reindex_one_post(client, message, future_timeout=30)
            assert seen["future_timeout"] == 30

        _run(go())
    finally:
        _reset_bridge()
