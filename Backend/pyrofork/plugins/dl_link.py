"""DM file-to-link: get a personal /dl streaming link for any file in DM.

Send (or forward) a document/video to the bot in a private chat and it
replies with a ready-to-use /dl link; reply to any older file with /dl
to get its link; reply with /check to run a staged pre-flight of the
whole link path (access, link path, file properties, and a live probe
fetch through the buffer engine).

Two link modes, chosen by the `dl_channel_id` setting:

* Pool mode (a DL Channel is configured) — the bot copies the file
  into that channel (a server-side copy: no download, no re-upload)
  and the link points at the channel post, so the whole bot pool
  streams it exactly like a library file and the Userbot session is
  never used. Copies are deduplicated by file_unique_id (the mapping
  lives in the tracking DB, collection `dl_link_map`). Pool mode is
  strict: a failed copy reports why and NEVER falls back to the
  userbot.
* Userbot mode (no DL Channel) — the link points at the DM message
  through the existing "global" stream route, which serves it via the
  Userbot session — the userbot account is a party to the DM, so it
  can read the file — and through the streaming buffer engine.
  Nothing is indexed and nothing is posted to any channel.

Access mirrors /start: in owner-only mode only the owner gets links;
in subscription mode any user holding a valid API token does. The link
itself is protected by that same personal token.

Setup helper: while no DL Channel is configured, forwarding a channel
post to the bot in DM replies with that channel's id (for Settings →
DL Channel) instead of a link. Once a DL Channel IS configured,
forwards go back to being link requests — except a forward from the
DL Channel itself, which links the original channel post directly
(it is already pooled; no copy needed). Replying /dl to any forwarded
file always issues its link.

This file is a pyrofork plugin (auto-loaded from this package by the
primary bot client). It adds no edits to any upstream file. The
`dl_channel_id` setting field and the DL Channel's row in the Tools →
Bot Admin Manager channel list are small seams in the integration
patch (settings_manager.py, settings.html, api_routes.py) — plugins
cannot register settings fields or tool rows.

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
  name+size), never assumed equal to the bot-side id. Pool mode needs
  neither: channel posts have one id, seen identically by every bot.

The /dl menu entry is NOT registered here: upstream's
setup_bot_commands() wipes and rewrites the bot command list at every
startup, so the entry lives in that list (Backend/helper/pyro.py, a
one-line seam in the integration patch). The handler is the plugin;
the menu is upstream's to own.
"""

import asyncio
import time
from html import escape
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


def pool_payload(channel_id: int, msg_id: int) -> dict:
    """Encoded /dl payload for a file pooled in the DL channel.

    The /dl route's non-global branch rebuilds the full channel id as
    -100<chat_id>, so the payload carries the id without that prefix —
    the same convention library (database) links use.
    """
    raw = str(int(channel_id))
    if raw.startswith("-100"):
        raw = raw[4:]
    return {"chat_id": int(raw), "msg_id": int(msg_id)}


def _dl_channel_id():
    """The configured DL channel id (settings key dl_channel_id), or None.

    The Settings property already returns int|None; stay defensive so
    older snapshots or raw strings also parse.
    """
    raw = getattr(SettingsManager.current(), "dl_channel_id", None)
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw or None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _forward_channel(message):
    """(chat_id, origin_msg_id, title) when `message` was forwarded from
    a channel, else None. Handles both the pyrofork forward_origin
    object and the legacy forward_from_chat fields."""
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) or getattr(message, "forward_from_chat", None)
    if chat is None or getattr(chat, "id", None) is None:
        return None
    ctype = getattr(chat, "type", None)
    channel_type = getattr(getattr(enums, "ChatType", None), "CHANNEL", None)
    if channel_type is not None:
        is_channel = ctype == channel_type
    else:
        is_channel = str(getattr(ctype, "value", ctype)) == "channel"
    if not is_channel:
        return None
    origin_msg_id = getattr(origin, "message_id", None) or getattr(
        message, "forward_from_message_id", None)
    return int(chat.id), origin_msg_id, getattr(chat, "title", None)


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


async def _scan_userbot_view(bot_id: int, target: Message):
    """The id of `target`'s file in the userbot's view of the DM, or None.

    Bot-side and user-side message ids in a DM can differ (observed live:
    the bot's msg 11 was the userbot's msg 21832). So scan the userbot's
    recent history of this chat and match the file itself —
    file_unique_id first, then name+size.
    """
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return None
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
        return None
    return fallback


async def _resolve_userbot_msg_id(bot_id: int, target: Message) -> int:
    """_scan_userbot_view with the bot-side id as the last-resort fallback
    (e.g. a file older than the scan window)."""
    found = await _scan_userbot_view(bot_id, target)
    return found if found is not None else target.id


#----- Pool mode: dedupe store (tracking DB, best-effort)
def _dedupe_coll():
    try:
        tracking = (getattr(db, "dbs", None) or {}).get("tracking")
        return tracking["dl_link_map"] if tracking is not None else None
    except Exception:
        return None


async def _dedupe_get(unique_id):
    """The stored DL-channel post for this file_unique_id, or None."""
    coll = _dedupe_coll()
    if coll is None or not unique_id:
        return None
    try:
        return await coll.find_one({"_id": unique_id})
    except Exception:
        return None


async def _dedupe_put(unique_id, msg_id) -> None:
    coll = _dedupe_coll()
    if coll is None or not unique_id:
        return
    try:
        await coll.update_one(
            {"_id": unique_id},
            {"$set": {"msg_id": int(msg_id), "ts": time.time()}},
            upsert=True,
        )
    except Exception:
        pass  # dedupe is an optimisation; a miss just copies again


async def _pool_copy(client: Client, target: Message, dl_id: int):
    """(channel_msg_id, already_pooled) — copy `target` into the DL channel.

    The primary bot performs the copy: it received the DM and is an
    admin in the DL channel. Telegram duplicates the media server-side —
    no download, no re-upload, no userbot involvement.
    """
    unique = media_unique_id(target)
    doc = await _dedupe_get(unique)
    if doc is not None:
        return int(doc["msg_id"]), True
    copied = await client.copy_message(
        chat_id=dl_id, from_chat_id=target.chat.id, message_id=target.id
    )
    await _dedupe_put(unique, copied.id)
    return int(copied.id), False


def _copy_error_hint(exc: Exception) -> str:
    up = str(exc).upper()
    if "ADMIN" in up or "RIGHTS" in up or "CHAT_WRITE_FORBIDDEN" in up:
        return ("the bot is not an admin in the DL Channel. Open Tools → "
                "Bot Admin Manager in the web UI, apply, then try again.")
    if "CHANNEL_PRIVATE" in up or "CHANNEL_INVALID" in up or "PEER_ID_INVALID" in up:
        return ("the bot cannot access the DL Channel. Check the channel ID in "
                "Settings and that the bot was added to the channel.")
    return f"{type(exc).__name__}: {exc}"


async def _send_pool_link(client: Client, message: Message, target: Message,
                          token: str, dl_id: int) -> None:
    """Pool-mode link: copy into the DL channel, link the channel post.

    Strictly pool-only: a failed copy reports why and NEVER falls back
    to the userbot — that is the point of configuring the channel.
    """
    media = media_of(target)
    if media is None:
        return
    name, size = media
    try:
        msg_id, pooled = await _pool_copy(client, target, dl_id)
    except Exception as e:
        await message.reply_text(
            "⚠️ <b>Couldn't pool this file</b>\n\n"
            f"{escape(_copy_error_hint(e))}\n\n"
            "Your Userbot was <b>not</b> used — pool mode never falls back to it.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return
    # Pre-Warm trigger: a file just landed in the DL Channel.
    try:
        from Backend.pyrofork.plugins.prewarm_hooks import offer_pooled

        offer_pooled(dl_id, msg_id, size)
    except Exception:
        pass
    encoded = await encode_string(pool_payload(dl_id, msg_id))
    base_url = SettingsManager.current().base_url
    link = build_link(base_url, token, encoded, name)
    bots = 1 + len(getattr(botmod, "multi_clients", None) or {})
    await message.reply_text(
        "🔗 <b>Your stream / download link</b>\n\n"
        f"📄 <code>{escape(name)}</code> ({human_size(size)})\n\n"
        f"<code>{link}</code>\n\n"
        f"{'Already in' if pooled else 'Copied to'} your DL Channel — served by the "
        f"bot pool ({bots} bot{'s' if bots != 1 else ''}), userbot not used. "
        "Works in browsers and players (VLC, MX Player, mpv). "
        "Keep it private — it carries your personal token.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def send_link(client: Client, message: Message, target: Message) -> None:
    """Reply to `message` with the /dl link for the media in `target`."""
    user = message.from_user
    user_id = user.id if user else message.chat.id
    user_name = (user.first_name or user.username or f"User {user_id}") if user else f"User {user_id}"

    token = await resolve_token(user_id, user_name)
    if token is None:
        return  # not authorised: stay silent, exactly like /start does

    dl_id = _dl_channel_id()
    if dl_id is not None:
        await _send_pool_link(client, message, target, token, dl_id)
        return

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


async def _reply_channel_id(client: Client, message: Message, fwd) -> None:
    """Setup helper: a forwarded channel post reveals that channel's id."""
    user = message.from_user
    user_id = user.id if user else message.chat.id
    user_name = (user.first_name or user.username or f"User {user_id}") if user else f"User {user_id}"
    if await resolve_token(user_id, user_name) is None:
        return  # not authorised: stay silent, exactly like /dl does
    chat_id, _origin_msg_id, title = fwd
    dl_id = _dl_channel_id()
    if dl_id is not None and int(dl_id) == int(chat_id):
        note = "This is your configured DL Channel."
    else:
        note = ("Paste this ID into Settings → DL Channel to make it your "
                "DM Link pool channel.")
    await message.reply_text(
        "📣 <b>Channel ID</b>\n\n"
        f"{('<b>' + escape(title) + '</b>\n') if title else ''}"
        f"<code>{chat_id}</code>\n\n"
        f"{note}\n"
        "No link was created for this forwarded file — reply to it with /dl "
        "if you wanted one.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_dl_post_link(client: Client, message: Message, dl_id: int,
                             origin_msg_id: int) -> None:
    """Link a file forwarded FROM the DL Channel: the original post is
    already pooled, so the link points straight at it — no copy."""
    user = message.from_user
    user_id = user.id if user else message.chat.id
    user_name = (user.first_name or user.username or f"User {user_id}") if user else f"User {user_id}"
    token = await resolve_token(user_id, user_name)
    if token is None:
        return  # not authorised: stay silent, exactly like /dl does
    media = media_of(message)
    if media is None:
        return
    name, size = media
    # Remember the mapping so DMing the same file later dedupes to this post.
    await _dedupe_put(media_unique_id(message), origin_msg_id)
    # Pre-Warm trigger: this pooled post is being linked right now.
    try:
        from Backend.pyrofork.plugins.prewarm_hooks import offer_pooled

        offer_pooled(dl_id, origin_msg_id, size)
    except Exception:
        pass
    encoded = await encode_string(pool_payload(dl_id, origin_msg_id))
    base_url = SettingsManager.current().base_url
    link = build_link(base_url, token, encoded, name)
    bots = 1 + len(getattr(botmod, "multi_clients", None) or {})
    await message.reply_text(
        "🔗 <b>Your stream / download link</b>\n\n"
        f"📄 <code>{escape(name)}</code> ({human_size(size)})\n\n"
        f"<code>{link}</code>\n\n"
        f"Already in your DL Channel — served by the bot pool "
        f"({bots} bot{'s' if bots != 1 else ''}), userbot not used. "
        "Works in browsers and players (VLC, MX Player, mpv). "
        "Keep it private — it carries your personal token.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


#----- Auto-link: any document/video sent to the bot in DM gets its link.
#----- Channel forwards are special only while they carry setup meaning:
#----- no DL Channel yet -> the channel-id helper (id discovery); a forward
#----- from the DL Channel itself -> a direct link to the pooled original.
#----- Forwards from any other channel are ordinary link requests.
@Client.on_message(filters.private & (filters.document | filters.video), group=11)
async def dl_auto_link(client: Client, message: Message):
    try:
        fwd = _forward_channel(message)
        if fwd is not None:
            chat_id, origin_msg_id, _title = fwd
            dl_id = _dl_channel_id()
            if dl_id is None:
                await _reply_channel_id(client, message, fwd)
                return
            if int(chat_id) == int(dl_id):
                if origin_msg_id:
                    await _send_dl_post_link(client, message, dl_id, int(origin_msg_id))
                else:
                    await _reply_channel_id(client, message, fwd)
                return
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


#----- /check: Link Doctor — pre-flight diagnostics for a DM file
PROBE_TIMEOUT_S = 20.0


async def _peer_check(client: Client) -> bool:
    """Can the userbot resolve this bot as a peer right now?"""
    userbot = getattr(botmod, "Userbot", None)
    username = getattr(getattr(client, "me", None), "username", None)
    if userbot is None or not username:
        return False
    for method in ("get_chat", "get_users"):
        try:
            await getattr(userbot, method)(username)
            return True
        except Exception:
            continue
    return False


async def _probe_first_chunk(file_id, chat_id: int, msg_id: int,
                             client=None, client_index: int = 0):
    """Time-to-first-chunk through the real buffer engine -> (ms, mbps).

    Runs one 1 MiB part through BufferedByteStreamer on `client` (the
    userbot by default; a pool bot for DL-channel files) — the exact
    path the link streams through — then abandons the generator,
    exactly like a player disconnecting early.
    """
    from Backend.helper.buffered_streamer import BufferedByteStreamer
    from Backend.pyrofork.bot import USERBOT_CLIENT_INDEX

    if client is None:
        client = botmod.Userbot
        client_index = USERBOT_CLIENT_INDEX
    streamer = BufferedByteStreamer(client, client_index)
    chunk_size = 1024 * 1024
    gen = await streamer.prefetch_stream(
        file_id=file_id, client_index=client_index,
        offset=0, first_part_cut=0, last_part_cut=chunk_size,
        part_count=1, chunk_size=chunk_size, prefetch=1, parallelism=1,
        stream_id=None, meta={"title": "link-doctor probe", "doctor": True},
        request=None, chat_id=chat_id, message_id=msg_id,
    )
    start = time.monotonic()
    try:
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=PROBE_TIMEOUT_S)
        ms = (time.monotonic() - start) * 1000.0
        mbps = (len(chunk) * 8) / max(ms / 1000.0, 1e-9) / 1_000_000
        return ms, mbps
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass


async def _check_pool_stages(client: Client, target: Message, dl_id: int, lines):
    """DL-channel stages for /check -> (failures, degraded, probe_ms)."""
    failures, degraded, probe_ms = [], [], None
    lines.append(f"✓ DL Channel: configured (<code>{dl_id}</code>) — "
                 "pool mode, userbot not used")

    bot_id = client.me.id if getattr(client, "me", None) else None
    if not bot_id:
        bot_id = (await client.get_me()).id

    # The bot must be an admin in the channel: only admins can post copies.
    try:
        member = await client.get_chat_member(dl_id, bot_id)
        status = str(getattr(getattr(member, "status", ""), "value",
                             getattr(member, "status", "")))
        if status in ("administrator", "creator", "owner"):
            lines.append("✓ Bot: admin in the DL Channel (can post copies)")
        elif status in ("member", "restricted"):
            lines.append("⚠ Bot: in the DL Channel but not an admin — copies will "
                         "fail. Fix: Tools → Bot Admin Manager → Apply")
            degraded.append("bot not admin in DL channel")
        else:
            lines.append(f"✗ Bot: status in the DL Channel is '{status or 'unknown'}'")
            failures.append("bot not in DL channel")
    except Exception as e:
        lines.append(f"✗ Bot: cannot access the DL Channel ({type(e).__name__}: {e}) — "
                     "check the ID in Settings and that the bot was added")
        failures.append("DL channel access")

    doc = await _dedupe_get(media_unique_id(target))
    pooled_msg = int(doc["msg_id"]) if doc else None
    if pooled_msg is not None:
        lines.append(f"✓ Copy: already pooled as post {pooled_msg}")
    else:
        lines.append("• Copy: not pooled yet — the first /dl copies it into the channel")

    file_id = None
    if pooled_msg is not None:
        try:
            from Backend.helper.pyro import get_file_ids

            file_id = await get_file_ids(client, dl_id, pooled_msg)
            lines.append(f"✓ File: {human_size(file_id.file_size)} · DC{file_id.dc_id}"
                         f" · {getattr(file_id, 'mime_type', '') or 'unknown type'}")
        except Exception as e:
            lines.append(f"✗ File properties: {type(e).__name__}: {e}")
            failures.append("file lookup")

    if file_id is not None:
        try:
            probe_ms, probe_mbps = await _probe_first_chunk(
                file_id, dl_id, pooled_msg, client=client, client_index=0)
            lines.append(f"✓ Probe: first chunk in {probe_ms:.0f} ms (~{probe_mbps:.0f} Mbps)")
        except asyncio.TimeoutError:
            lines.append(f"✗ Probe: no data within {PROBE_TIMEOUT_S:.0f}s — a pool "
                         "session is likely flood-penalised or the DC is unreachable")
            failures.append("probe timeout")
        except Exception as e:
            lines.append(f"✗ Probe: {type(e).__name__}: {e}")
            failures.append("probe fetch")

    bots = 1 + len(getattr(botmod, "multi_clients", None) or {})
    lines.append(f"Pool: {bots} bot{'s' if bots != 1 else ''} serve pooled links")
    return failures, degraded, probe_ms


def _finish_check(lines, failures, degraded, probe_ms) -> str:
    """Append the buffer line + verdict to a /check report; return the text."""
    try:
        from Backend.helper import stream_buffer as sb

        pol = sb.get_policy()
        snap = sb.METRICS.snapshot()
        lines.append(f"Buffer: {pol.profile} · workers {pol.workers} · "
                     f"per-client cap {pol.per_client_cap} · cache {pol.cache_mb} MB · "
                     f"floods {snap['floods_seen']} · stalls {snap['stall_timeouts']}")
    except Exception:
        pass

    if failures:
        verdict = f"<b>Verdict: broken</b> — failing stage: {', '.join(failures)}."
    elif degraded:
        verdict = f"<b>Verdict: degraded</b> — {', '.join(degraded)}."
    elif probe_ms is not None and probe_ms < 500:
        verdict = "<b>Verdict: healthy</b> — should start instantly."
    elif probe_ms is not None and probe_ms < 2000:
        verdict = "<b>Verdict: healthy</b> — normal start expected."
    elif probe_ms is not None:
        verdict = "<b>Verdict: slow</b> — link works, but expect a slow start."
    else:
        verdict = "<b>Verdict: unknown</b> — probe did not run."
    lines.append(f"\n{verdict}")
    return "\n".join(lines)


async def run_check(client: Client, message: Message, target: Message) -> None:
    """Reply to `message` with a staged health report for `target`'s file."""
    user = message.from_user
    user_id = user.id if user else message.chat.id
    user_name = (user.first_name or user.username or f"User {user_id}") if user else f"User {user_id}"

    token = await resolve_token(user_id, user_name)
    if token is None:
        return  # not authorised: stay silent, exactly like /dl does

    media = media_of(target)
    if media is None:
        return
    name, _size = media
    lines = [f"🩺 <b>Link check</b> — <code>{name}</code>", "✓ Access: token valid"]
    failures = []
    degraded = []

    dl_id = _dl_channel_id()
    if dl_id is not None:
        failures, degraded, probe_ms = await _check_pool_stages(
            client, target, dl_id, lines)
        await message.reply_text(
            _finish_check(lines, failures, degraded, probe_ms),
            quote=True, parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True)
        return

    if botmod.Userbot is None:
        lines.append("✗ Userbot: not configured — DM links cannot stream")
        lines.append("\n<b>Verdict: broken</b> — no userbot on this server.")
        await message.reply_text("\n".join(lines), quote=True,
                                 parse_mode=enums.ParseMode.HTML)
        return
    ub_name = getattr(getattr(botmod.Userbot, "me", None), "username", None)
    lines.append(f"✓ Userbot: configured{f' (@{ub_name})' if ub_name else ''}")

    if await _peer_check(client):
        lines.append("✓ Peer: userbot can reach this bot")
    else:
        lines.append("✗ Peer: userbot cannot resolve this bot")
        failures.append("peer resolution")

    bot_id = client.me.id if getattr(client, "me", None) else None
    if not bot_id:
        bot_id = (await client.get_me()).id

    found = await _scan_userbot_view(bot_id, target)
    if found is not None:
        lines.append(f"✓ Message: found in the userbot's view (id {found})")
        msg_id = found
    else:
        lines.append("⚠ Message: not matched in the userbot's recent view — "
                     "the link would carry the bot-side id, which is unreliable")
        degraded.append("message not matched in userbot view")
        msg_id = target.id

    file_id = None
    try:
        from Backend.helper.pyro import get_file_ids

        file_id = await get_file_ids(botmod.Userbot, bot_id, msg_id)
        lines.append(f"✓ File: {human_size(file_id.file_size)} · DC{file_id.dc_id}"
                     f" · {getattr(file_id, 'mime_type', '') or 'unknown type'}")
    except Exception as e:
        lines.append(f"✗ File properties: {type(e).__name__}: {e}")
        failures.append("file lookup")

    probe_ms = None
    if file_id is not None:
        try:
            probe_ms, probe_mbps = await _probe_first_chunk(file_id, bot_id, msg_id)
            lines.append(f"✓ Probe: first chunk in {probe_ms:.0f} ms (~{probe_mbps:.0f} Mbps)")
        except asyncio.TimeoutError:
            lines.append(f"✗ Probe: no data within {PROBE_TIMEOUT_S:.0f}s — the session "
                         "is likely flood-penalised or the DC is unreachable")
            failures.append("probe timeout")
        except Exception as e:
            lines.append(f"✗ Probe: {type(e).__name__}: {e}")
            failures.append("probe fetch")

    await message.reply_text(
        _finish_check(lines, failures, degraded, probe_ms),
        quote=True, parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True)


#----- /check: reply to any file in this chat to pre-flight its link path
@Client.on_message(filters.command("check") & filters.private, group=11)
async def dl_check_command(client: Client, message: Message):
    target = message.reply_to_message
    if target is None or media_of(target) is None:
        await message.reply_text(
            "Reply to a file in this chat with /check and I'll pre-flight it: "
            "access, the link path (DL Channel pool or userbot), file "
            "properties, and a live probe fetch through the buffer engine.",
            quote=True,
        )
        return
    try:
        await run_check(client, message, target)
    except Exception as e:
        LOGGER.warning(f"[DL] /check failed for message {target.id}: {e}")


#----- Flood alerts (Wave 2): the buffer engine aggregates FloodWaits in
#----- stream_buffer.FloodAlerter; this plugin registers the sender that
#----- delivers them to the owner. The toggle is read live at send time,
#----- so enabling/disabling in Settings needs no restart. With the
#----- toggle off (default) the sender returns immediately.
async def _flood_alert_sender(event) -> None:
    try:
        if not SettingsManager.current().flood_alert_enabled:
            return
        owner = getattr(Telegram, "OWNER_ID", 0) or 0
        if not owner:
            return

        def _who(idx) -> str:
            return f"Bot {idx + 1}" if idx >= 0 else "Userbot"

        if event.get("kind") == "flood_summary":
            clients = ", ".join(_who(c) for c in event.get("clients", [])) or "the pool"
            text = (
                "⚠️ <b>FloodWait summary</b>\n"
                f"{event.get('count')} more waits in the last 10 min "
                f"(max {event.get('max_wait_s')}s) — {clients}. "
                "The engine is routing around them."
            )
        else:
            title = str(event.get("title") or "a stream")[:60]
            text = (
                f"⚠️ <b>FloodWait</b> — {_who(event.get('client_index', 0))} must wait "
                f"{event.get('wait_s')}s (while streaming {escape(title)}). "
                "The engine shed the load to the other bots; no action needed."
            )
        await botmod.StreamBot.send_message(
            owner, text, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        LOGGER.warning(f"[DL] flood alert send failed: {e}")


try:
    from Backend.helper import stream_buffer as _stream_buffer

    _stream_buffer.set_flood_sender(_flood_alert_sender)
except Exception as _e:  # pragma: no cover - registration must never break the plugin
    LOGGER.warning(f"[DL] flood alert sender registration failed: {_e}")
