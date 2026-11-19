"""Resolve a streamed file's chat_id to a human-readable source.

Stream records (live registry and persisted analytics) carry only the
numeric chat_id of the Telegram post being streamed. This module turns
that into a label — the channel's title when known, a type badge for
the DL Channel / DM-Global path, or the raw id as a last resort.

Pure and stdlib-only: the context (channel titles + which configured
group each channel belongs to) is built from the live settings by
``build_source_context`` and passed in, so the module is testable
without the web stack or a database.
"""

from typing import Any, Dict, Optional

_KIND_BY_LIST = (
    ("auth", "auth_channels"),
    ("global_search", "global_search_channels"),
    ("manual", "manual_channels"),
    ("anime", "anime_channels"),
)
_KIND_BY_SINGLE = (
    ("announce", "announcement_channel"),
    ("skip", "skip_channel"),
)


def _as_chat_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_source_context(settings: Any) -> Dict[str, Any]:
    """Snapshot the settings data classify_source needs."""
    kinds: Dict[int, str] = {}
    for kind, attr in _KIND_BY_LIST:
        for raw in (getattr(settings, attr, None) or []):
            cid = _as_chat_int(raw)
            if cid is not None:
                kinds[cid] = kind
    for kind, attr in _KIND_BY_SINGLE:
        cid = _as_chat_int(getattr(settings, attr, None))
        if cid is not None:
            kinds[cid] = kind
    dl = getattr(settings, "dl_channel_id", None)
    return {
        "titles": dict(getattr(settings, "channel_titles", None) or {}),
        "dl_channel_id": dl if isinstance(dl, int) else _as_chat_int(dl),
        "kinds": kinds,
    }


def classify_source(chat_id: Any, context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Return {"label": str, "kind": str} for a stream's chat_id.

    kind is one of: dl, auth, global_search, manual, anime, announce,
    skip, global (a DM/userbot stream), channel (titled but ungrouped),
    unknown.
    """
    cid = _as_chat_int(chat_id)
    if cid is None:
        return {"label": "Unknown", "kind": "unknown"}
    if cid > 0:
        # A positive id is a user, not a channel: a DM / global stream
        # served by the userbot.
        return {"label": "DM · Global", "kind": "global"}
    context = context or {}
    titles = context.get("titles") or {}
    title = titles.get(str(cid)) or titles.get(cid)
    dl = context.get("dl_channel_id")
    if dl is not None and cid == dl:
        return {"label": title or "DL Channel", "kind": "dl"}
    kind = (context.get("kinds") or {}).get(cid, "")
    if title:
        return {"label": title, "kind": kind or "channel"}
    if kind:
        return {"label": kind.replace("_", " ").capitalize() + " channel", "kind": kind}
    return {"label": str(cid), "kind": "unknown"}
