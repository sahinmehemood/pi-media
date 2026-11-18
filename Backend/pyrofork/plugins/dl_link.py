"""DM file-to-link: get a personal /dl streaming link for any file in DM.

Send (or forward) a document/video to the bot in a private chat and it
replies with a ready-to-use /dl link; reply to any older file with /dl
to get its link; reply with /check to run a staged pre-flight of the
whole link path (access, userbot, message resolution, file properties,
and a live probe fetch through the buffer engine). The link points at
the message through the existing
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

import asyncio
import time
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


async def _probe_first_chunk(file_id, bot_id: int, msg_id: int):
    """Time-to-first-chunk through the real buffer engine -> (ms, mbps).

    Runs one 1 MiB part through BufferedByteStreamer on the userbot —
    the exact path a DM link streams through — then abandons the
    generator, exactly like a player disconnecting early.
    """
    from Backend.helper.buffered_streamer import BufferedByteStreamer
    from Backend.pyrofork.bot import USERBOT_CLIENT_INDEX

    streamer = BufferedByteStreamer(botmod.Userbot, USERBOT_CLIENT_INDEX)
    chunk_size = 1024 * 1024
    gen = await streamer.prefetch_stream(
        file_id=file_id, client_index=USERBOT_CLIENT_INDEX,
        offset=0, first_part_cut=0, last_part_cut=chunk_size,
        part_count=1, chunk_size=chunk_size, prefetch=1, parallelism=1,
        stream_id=None, meta={"title": "link-doctor probe", "doctor": True},
        request=None, chat_id=bot_id, message_id=msg_id,
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

    await message.reply_text("\n".join(lines), quote=True,
                             parse_mode=enums.ParseMode.HTML,
                             disable_web_page_preview=True)


#----- /check: reply to any file in this chat to pre-flight its link path
@Client.on_message(filters.command("check") & filters.private, group=11)
async def dl_check_command(client: Client, message: Message):
    target = message.reply_to_message
    if target is None or media_of(target) is None:
        await message.reply_text(
            "Reply to a file in this chat with /check and I'll pre-flight it: "
            "access, userbot, message resolution, file properties, and a live "
            "probe fetch through the buffer engine.",
            quote=True,
        )
        return
    try:
        await run_check(client, message, target)
    except Exception as e:
        LOGGER.warning(f"[DL] /check failed for message {target.id}: {e}")
