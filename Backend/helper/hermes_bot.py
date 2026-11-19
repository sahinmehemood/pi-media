"""Hermes Bot identity for the Bot Admin Manager.

The Hermes Bot (the Media Manager) is Yan's librarian bot: it manages
media captions in the source channels and must NEVER participate in
streaming. It is registered in Settings ("Hermes Bot" card) with its
token; on save the token is verified against Telegram (getMe) and the
resolved identity {user_id, username, name} is cached in the
``hermes_bot`` setting.

Only the identity is used at runtime: the Bot Admin Manager's userbot
promotes the bot as a channel admin by user id, exactly like the pool
bots. The token is never used to build a streaming client — the client
pool is constructed from ``multi_tokens`` only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def sanitize_identity(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce a raw value into {user_id, username, name} or None."""
    if not isinstance(raw, dict):
        return None
    try:
        uid = int(raw.get("user_id"))
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    return {
        "user_id": uid,
        "username": str(raw.get("username") or "").lstrip("@"),
        "name": str(raw.get("name") or ""),
    }


def display_name(identity: Dict[str, Any]) -> str:
    if identity.get("name"):
        return str(identity["name"])
    if identity.get("username"):
        return f"@{identity['username']}"
    return f"Bot {identity.get('user_id')}"


def manager_entry(identity: Any) -> Optional[Dict[str, Any]]:
    """Shape the Hermes Bot identity like a managed-bot entry for the
    Bot Admin Manager's scan/apply. Returns None when not registered."""
    clean = sanitize_identity(identity)
    if clean is None:
        return None
    return {
        "client_id": None,
        "user_id": clean["user_id"],
        "username": clean["username"],
        "name": display_name(clean),
        "is_main": False,
        "external": True,
        "hermes": True,
    }
