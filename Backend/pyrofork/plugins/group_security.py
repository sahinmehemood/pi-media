from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait
from pyrogram.types import ChatMemberUpdated

from Backend import db
from Backend.config import Telegram
from Backend.helper.flood_wait import run_with_flood_retry
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Kick joiners of the subscription group who lack an active subscription
@Client.on_chat_member_updated()
async def on_user_join(client: Client, chat_member_updated: ChatMemberUpdated):
    if not SettingsManager.current().subscription:
        return
    if chat_member_updated.chat.id != SettingsManager.current().subscription_group_id:
        return

    new_status = chat_member_updated.new_chat_member.status if chat_member_updated.new_chat_member else None
    if new_status != ChatMemberStatus.MEMBER:
        return

    user = chat_member_updated.new_chat_member.user

    #----- Never kick the owner or configured approvers
    if user.id == Telegram.OWNER_ID or user.id in (SettingsManager.current().approver_ids or []):
        return

    db_user = await db.get_user(user.id)
    if db.is_subscription_active(db_user):
        return

    try:
        await run_with_flood_retry(
            client, client.ban_chat_member, chat_member_updated.chat.id, user.id,
            what=f"kick unauthorized joiner {user.id}",
        )
        await run_with_flood_retry(
            client, client.unban_chat_member, chat_member_updated.chat.id, user.id,
            what=f"unban kicked joiner {user.id}",
        )
        LOGGER.info(f"Kicked unauthorized user {user.id} from group {chat_member_updated.chat.id}")
        try:
            await run_with_flood_retry(
                client, client.send_message, user.id,
                "❌ <b>Access Denied</b>\n\n"
                "You were removed from the private group because you do not have an active subscription.\n"
                "Please press /start in this bot to purchase or renew your subscription.",
                what=f"denial notice to {user.id}",
            )
        except FloodWait as e:
            LOGGER.warning(f"Access-denied notice to {user.id} delayed by FloodWait: {e}")
    except FloodWait as e:
        LOGGER.error(f"Failed to kick unauthorized user {user.id} (FloodWait): {e}")
    except Exception as e:
        LOGGER.error(f"Failed to kick/notify unauthorized user {user.id}: {e}")
