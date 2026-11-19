"""Behavioural tests for the Bot Admin Manager external-bot registry.

The registry (Backend/helper/bot_admin_external.py) is pure logic: it
persists only a bot's public identity (user id / username / name) so an
external bot — e.g. the Hermes Media Manager — can be promoted as a
channel admin by the userbot without ever becoming a streaming client.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module


def load_helper():
    return load_module("bot_admin_external_under_test",
                       "Backend/helper/bot_admin_external.py")


MM = {"user_id": 9069374582, "username": "mediamanagerthehdbot",
      "name": "Media Manager (Hermes)"}


def test_sanitize_drops_junk_and_dedupes():
    h = load_helper()
    raw = [
        dict(MM),
        {"user_id": "9069374582", "username": "@mediamanagerthehdbot"},  # dup
        {"user_id": "not-a-number"},
        {"name": "no id at all"},
        "garbage",
        {"user_id": -5},
        {"user_id": 42, "username": "otherbot", "name": ""},
    ]
    out = h.sanitize_bots(raw)
    assert [b["user_id"] for b in out] == [9069374582, 42], out
    assert out[0]["username"] == "mediamanagerthehdbot"  # @ stripped
    assert out[1]["name"] == ""
    assert h.sanitize_bots(None) == []
    assert h.sanitize_bots({"user_id": 1}) == []


def test_entries_shape_and_no_token():
    h = load_helper()
    entries = h.external_entries([MM], managed_ids=set())
    assert len(entries) == 1
    e = entries[0]
    assert e["user_id"] == 9069374582
    assert e["username"] == "mediamanagerthehdbot"
    assert e["name"] == "Media Manager (Hermes)"
    assert e["external"] is True
    assert e["is_main"] is False
    assert e["client_id"] is None
    # The safety property: an external registration carries no credential
    # material of any kind, so the streaming pool can never pick it up.
    for key in e:
        assert "token" not in key and "session" not in key, key


def test_external_excluded_when_pool_has_same_id():
    h = load_helper()
    entries = h.external_entries([MM], managed_ids={9069374582})
    assert entries == []


def test_display_name_fallbacks():
    h = load_helper()
    assert h.display_name({"user_id": 1, "username": "", "name": "Named"}) == "Named"
    assert h.display_name({"user_id": 1, "username": "somebot", "name": ""}) == "@somebot"
    assert h.display_name({"user_id": 7, "username": "", "name": ""}) == "Bot 7"


def test_upsert_and_remove_roundtrip():
    h = load_helper()
    bots, added = h.upsert_bot([], MM)
    assert added and bots == [MM]
    bots2, added2 = h.upsert_bot(bots, dict(MM))
    assert not added2 and bots2 == bots
    bots3, added3 = h.upsert_bot(bots, {"user_id": 42, "username": "x", "name": ""})
    assert added3 and [b["user_id"] for b in bots3] == [9069374582, 42]
    kept, removed = h.remove_bot(bots3, "9069374582")
    assert removed and [b["user_id"] for b in kept] == [42]
    kept2, removed2 = h.remove_bot(kept, 999)
    assert not removed2 and kept2 == kept
    _, removed3 = h.remove_bot(kept, "junk")
    assert not removed3
