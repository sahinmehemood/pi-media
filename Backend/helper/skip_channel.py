import asyncio
from asyncio import sleep as asleep

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

# Librarian capture bound (DESIGN §18 / Phase 5 DISP-6 item 5,
# OQ-2 ratified (a), Security S5-8): capture is a database-only
# operation, awaited on the funnel's live path — slower than this
# is a failure, not a wait. A timeout is treated exactly like a
# capture exception: logged, never raised, copy/delete untouched.
CAPTURE_TIMEOUT_S = 30


def is_skip_channel(message: Message) -> bool:
    skip = SettingsManager.current().skip_channel
    if not skip:
        return False
    ref = str(skip).strip()
    if ref.lstrip("@-").replace("-100", "").isdigit():
        return ref.replace("-100", "").lstrip("@") == str(message.chat.id).replace("-100", "")
    username = (getattr(message.chat, "username", None) or "").lower()
    return bool(username) and ref.lstrip("@").lower() == username


async def route_to_skip_channel(client: Client, message: Message, origin: str = "live-fail") -> None:
    #----- Librarian capture (DESIGN §5): the hook sits at the funnel's
    #----- entry, awaited, so the ledger row exists whether or not a
    #----- skip copy is ever created — and before the original can be
    #----- deleted below. Exception-isolated: a capture failure is
    #----- logged, never raised, and never alters copy/delete behaviour.
    try:
        from Backend.helper.librarian_store import capture_rejection

        await asyncio.wait_for(capture_rejection(message, origin),
                               timeout=CAPTURE_TIMEOUT_S)
    except Exception as e:
        LOGGER.error(f"[Librarian] Capture failed for message {message.id}: {e}")

    settings = SettingsManager.current()
    skip = settings.skip_channel
    if not skip:
        return

    skip_chat = int(skip) if str(skip).lstrip("-").replace("-100", "").isdigit() else skip

    copied = None
    try:
        copied = await message.copy(skip_chat)
    except FloodWait as e:
        await asleep(e.value)
        try:
            copied = await message.copy(skip_chat)
        except Exception as e2:
            LOGGER.error(f"[SkipChannel] Copy failed for message {message.id}: {e2}")
            return
    except Exception as e:
        LOGGER.error(f"[SkipChannel] Could not copy message {message.id} to skip channel: {e}")
        return

    if copied is not None:
        try:
            from Backend.helper.librarian_store import set_skip_locator

            await set_skip_locator(message.chat.id, message.id, copied.chat.id, copied.id)
        except Exception as e:
            LOGGER.error(f"[Librarian] Skip-locator update failed for message {message.id}: {e}")

    if settings.delete_on_metadata_fail:
        try:
            from Backend.helper.task_manager import delete_message
            await delete_message(message.chat.id, message.id)
        except Exception as e:
            LOGGER.warning(f"[SkipChannel] Could not delete original message {message.id}: {e}")
