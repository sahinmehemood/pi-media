"""Behavioural tests for the Hermes Bot identity helpers.

The Hermes Bot (Media Manager) is registered in Settings with its
token; only the resolved identity is used at runtime so the Bot Admin
Manager can promote it as a channel admin. It is never a streaming
client — these helpers (Backend/helper/hermes_bot.py) are pure logic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module


def load_helper():
    return load_module("hermes_bot_under_test", "Backend/helper/hermes_bot.py")


MM = {"user_id": 9069374582, "username": "mediamanagerthehdbot",
      "name": "Media Manager (Hermes)"}


def test_sanitize_identity():
    h = load_helper()
    assert h.sanitize_identity(MM) == MM
    assert h.sanitize_identity({"user_id": "9069374582", "username": "@x"}) == {
        "user_id": 9069374582, "username": "x", "name": ""}
    assert h.sanitize_identity(None) is None
    assert h.sanitize_identity({}) is None
    assert h.sanitize_identity({"user_id": "junk"}) is None
    assert h.sanitize_identity({"user_id": -3}) is None
    assert h.sanitize_identity("9069374582") is None


def test_manager_entry_shape():
    h = load_helper()
    e = h.manager_entry(MM)
    assert e["user_id"] == 9069374582
    assert e["username"] == "mediamanagerthehdbot"
    assert e["name"] == "Media Manager (Hermes)"
    assert e["external"] is True and e["hermes"] is True
    assert e["is_main"] is False and e["client_id"] is None
    # The entry carries identity only — no credential material, so the
    # streaming pool (built from tokens) can never pick this bot up.
    for key in e:
        assert "token" not in key and "session" not in key, key
    assert h.manager_entry(None) is None
    assert h.manager_entry({}) is None


def test_manager_entry_name_fallbacks():
    h = load_helper()
    e = h.manager_entry({"user_id": 5, "username": "somebot", "name": ""})
    assert e["name"] == "@somebot"
    e2 = h.manager_entry({"user_id": 5, "username": "", "name": ""})
    assert e2["name"] == "Bot 5"
