"""External bots for the Bot Admin Manager.

The Bot Admin Manager promotes the app's own streaming clients to admin
in the channels they serve. Some bots must be channel admins WITHOUT
ever becoming streaming clients — above all the Hermes Media Manager,
which is librarian-only and holds no token in this app at all.

This module is the pure registry logic for those "external" bots. An
external bot is just a public identity (user id + username + display
name) persisted in the ``bot_admin_extra_bots`` setting. There is no
token to store: the connected userbot session adds and promotes bots
purely by user id, exactly as it does for the pool bots.

Keeping the registry token-free is the safety property: an external bot
can never be picked up by the streaming client pool, because the pool is
built from tokens and this registry has none.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def sanitize_bots(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a raw settings value into a clean external-bot list.

    Drops junk entries, coerces user ids to int, dedupes by user id
    (first entry wins) and preserves order.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(item.get("user_id"))
        except (TypeError, ValueError):
            continue
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        out.append({
            "user_id": uid,
            "username": str(item.get("username") or "").lstrip("@"),
            "name": str(item.get("name") or ""),
        })
    return out


def display_name(bot: Dict[str, Any]) -> str:
    if bot.get("name"):
        return str(bot["name"])
    if bot.get("username"):
        return f"@{bot['username']}"
    return f"Bot {bot.get('user_id')}"


def to_manager_entry(bot: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one registry entry like a managed-bot entry for scan/apply."""
    return {
        "client_id": None,
        "user_id": int(bot["user_id"]),
        "username": bot.get("username") or "",
        "name": display_name(bot),
        "is_main": False,
        "external": True,
    }


def external_entries(raw: Any, managed_ids) -> List[Dict[str, Any]]:
    """Manager-shaped external entries, excluding ids the pool already has."""
    managed = set()
    for x in managed_ids or ():
        try:
            managed.add(int(x))
        except (TypeError, ValueError):
            continue
    return [to_manager_entry(b) for b in sanitize_bots(raw)
            if int(b["user_id"]) not in managed]


def upsert_bot(raw: Any, entry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """Add ``entry`` to the registry; returns (new_list, added?)."""
    bots = sanitize_bots(raw)
    clean = sanitize_bots([entry])
    if not clean:
        return bots, False
    entry = clean[0]
    if any(b["user_id"] == entry["user_id"] for b in bots):
        return bots, False
    return bots + [entry], True


def remove_bot(raw: Any, user_id: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """Remove one bot by user id; returns (new_list, removed?)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return sanitize_bots(raw), False
    bots = sanitize_bots(raw)
    kept = [b for b in bots if b["user_id"] != uid]
    return kept, len(kept) != len(bots)
