"""DM file-to-link: get a personal /dl streaming link for any file in DM.

Send (or forward) a document/video to the bot in a private chat and it
replies with a ready-to-use /dl link; reply to any older file with /dl
to get its link. The link points at the message through the existing
"global" stream route, which serves it via the Userbot session — the
userbot account is a party to the DM, so it can read the file — and
through the streaming buffer engine. Nothing is indexed and nothing is
posted to any channel.

Access mirrors /start: in owner-only mode only the owner gets links;
in subscription mode any user holding a valid API token does. The link
itself is protected by that same personal token.

This file is a pyrofork plugin (auto-loaded from this package by the
primary bot client). It adds no edits to any upstream file.

Two pieces of glue live here because they cannot be upstream edits:

* Peer warm-up — the global streamer reads the DM through the userbot
  session, and pyrogram raises PEER_ID_INVALID for a peer that session
  has never cached. Before the first link is issued, the userbot
  resolves this bot by username once, which stores the peer (with its
  access hash) in the userbot session; the streamer's numeric-id
  lookups then work, including after restarts.
* Message-id resolution — bot-side and user-side message ids in a DM
  can differ, so the payload's msg_id is resolved in the userbot's own
  view by matching the file in its chat history (file_unique_id, then
  name+size), never assumed equal to the bot-side id.

The /dl menu entry is NOT registered here: upstream's
setup_bot_commands() wipes and rewrites the bot command list at every
startup, so the entry lives in that list (Backend/helper/pyro.py, a
one-line seam in the integration patch). The handler is the plugin;
the menu is upstream's to own.
"""

from urllib.parse import quote

from pyrogram import Client, enums, filters
from pyrogram.types import Message

import Backend.pyrofork.bot as botmod
from Backend import db
from Backend.config import Telegram
from Backend.helper.encrypt import encode_string
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Media helpers (pure, unit-tested)
def media_of(message):
    """(file_name, file_size) for a document/video message, else None."""
    media = getattr(message, "document", None) or getattr(message, "video", None)
    if media is None:
        return None
    name = getattr(media, "file_name", None) or f"file_{message.id}"
    return name, int(getattr(media, "file_size", 0) or 0)


def link_payload(bot_id: int, msg_id: int) -> dict:
    """Encoded /dl payload: the DM message, via the global (userbot) route.

    chat_id is the bot's own user id: from the userbot account's point of
    view, its private chat with this bot is addressed by the bot's id.
    msg_id must be the id in the USERBOT's view of the chat — in bot DMs
    the two sides' message numbering can differ, so send_link resolves
    it by matching the file (see _resolve_userbot_msg_id).
    """
    return {"chat_id": int(bot_id), "msg_id": int(msg_id), "global": True}


def media_unique_id(message):
    """The file_unique_id of a document/video message, else None."""
    media = getattr(message, "document", None) or getattr(message, "video", None)
    return getattr(media, "file_unique_id", None) if media is not None else None


def build_link(base_url: str, token: str, encoded: str, name: str) -> str:
    return f"{base_url}/dl/{token}/{encoded}/{quote(name)}"


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


#----- Token resolution: the same gate /start uses
async def resolve_token(user_id: int, user_name: str):
    """The user's personal API token, or None when they may not have one."""
    settings = SettingsManager.current()
    if not settings.subscription:
        # Owner-only mode: a single personal token for the owner.
        if user_id != Telegram.OWNER_ID:
            return None
        doc = await db.add_api_token(name=user_name, user_id=user_id)
        return doc["token"] if doc else None
    doc = await db.get_api_token_by_user(user_id)
    if doc:
        return doc["token"]
    if user_id == Telegram.OWNER_ID:
        doc = await db.add_api_token(name=user_name, user_id=user_id)
        return doc["token"] if doc else None
    return None


#----- Runtime glue: userbot peer warm-up
_peer_warmed = False


async def _warm_userbot_peer(client: Client) -> None:
    """Let the userbot 'meet' this bot so the global streamer can read DMs.

    Once per process; best-effort — link issuing proceeds either way.
    """
    global _peer_warmed
    if _peer_warmed:
        return
    _peer_warmed = True
    userbot = getattr(botmod, "Userbot", None)
    username = getattr(getattr(client, "me", None), "username", None)
    if userbot is None or not username:
        return
    for method in ("get_chat", "get_users"):
        try:
            await getattr(userbot, method)(username)
            return
        except Exception:
            continue


async def _resolve_userbot_msg_id(bot_id: int, target: Message) -> int:
    """The id of `target`'s file in the userbot's view of the DM.

    Bot-side and user-side message ids in a DM can differ (observed live:
    the bot's msg 11 was a text message for the userbot, which answered
    the stream request with "No supported media found"). So scan the
    userbot's recent history of this chat and match the file itself —
    file_unique_id first, then name+size. Falls back to the bot-side id
    when nothing matches (e.g. a file older than the scan window).
    """
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return target.id
    unique = media_unique_id(target)
    media = media_of(target)
    fallback = None
    try:
        async for m in userbot.get_chat_history(bot_id, limit=100):
            if unique is not None and media_unique_id(m) == unique:
                return m.id
            if fallback is None and media is not None and media_of(m) == media:
                fallback = m.id
    except Exception:
        return target.id
    return fallback if fallback is not None else target.id


async def send_link(client: Client, message: Message, target: Message) -> None:
    """Reply to `message` with the /dl link for the media in `target`."""
    user = message.from_user
    user_id = user.id if user else message.chat.id
    user_name = (user.first_name or user.username or f"User {user_id}") if user else f"User {user_id}"

    token = await resolve_token(user_id, user_name)
    if token is None:
        return  # not authorised: stay silent, exactly like /start does

    if botmod.Userbot is None:
        await message.reply_text(
            "⚠️ Link streaming is unavailable: no Userbot is configured on this server.",
            quote=True,
        )
        return

    await _warm_userbot_peer(client)

    media = media_of(target)
    if media is None:
        return
    name, size = media

    bot_id = client.me.id if getattr(client, "me", None) else None
    if not bot_id:
        me = await client.get_me()
        bot_id = me.id

    msg_id = await _resolve_userbot_msg_id(bot_id, target)
    encoded = await encode_string(link_payload(bot_id, msg_id))
    base_url = SettingsManager.current().base_url
    link = build_link(base_url, token, encoded, name)

    await message.reply_text(
        "🔗 <b>Your stream / download link</b>\n\n"
        f"📄 <code>{name}</code> ({human_size(size)})\n\n"
        f"<code>{link}</code>\n\n"
        "Works in browsers and players (VLC, MX Player, mpv). "
        "Keep it private — it carries your personal token.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


#----- Auto-link: any document/video sent to the bot in DM gets its link
@Client.on_message(filters.private & (filters.document | filters.video), group=11)
async def dl_auto_link(client: Client, message: Message):
    try:
        await send_link(client, message, message)
    except Exception as e:
        LOGGER.warning(f"[DL] auto-link failed for message {message.id}: {e}")


#----- /dl: reply to any file in this chat to get (or re-get) its link
@Client.on_message(filters.command("dl") & filters.private, group=11)
async def dl_command(client: Client, message: Message):
    target = message.reply_to_message
    if target is None or media_of(target) is None:
        await message.reply_text(
            "Reply to a file in this chat with /dl and I'll give you its stream link.\n"
            "Tip: you don't need the command for new files — just send the file and "
            "I'll reply with the link automatically.",
            quote=True,
        )
        return
    try:
        await send_link(client, message, target)
    except Exception as e:
        LOGGER.warning(f"[DL] /dl failed for message {target.id}: {e}")
