"""Registration of one private VC control group per hosted account."""

from __future__ import annotations

import html
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import database.mongo as db
from utils.message_ui import reply_html, reply_text

logger = logging.getLogger(__name__)

SETUP_GUIDE = (
    "🔐 <b>Private VC control group setup</b>\n\n"
    "1. Create your own private Telegram group.\n"
    "2. Add this control bot to that group.\n"
    "3. Give it administrator permission to manage voice chats.\n"
    "4. Send the group's numeric chat ID here.\n\n"
    "Usage:\n"
    "<code>/privategroupvcsetup &lt;control_group_chat_id&gt;</code>\n"
    "By default that same group is the source/personal VC.\n\n"
    "To use a separate source VC, pass both IDs:\n"
    "<code>/privategroupvcsetup &lt;control_group_id&gt; &lt;source_vc_group_id&gt;</code>"
)


async def privategroupvcsetup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    manager = context.bot_data["manager"]
    if message is None or user is None:
        return
    if not manager.is_hosted(user.id):
        await reply_text(message, "❌ Host your Telegram account first with /host.")
        return
    if not context.args:
        await reply_html(message, SETUP_GUIDE)
        return

    try:
        control_chat_id = int(context.args[0])
        source_chat_id = int(context.args[1]) if len(context.args) > 1 else control_chat_id
    except ValueError:
        await reply_html(message, SETUP_GUIDE)
        return

    if control_chat_id >= 0 or source_chat_id >= 0:
        await reply_text(message, "❌ Group chat IDs must be negative integers.")
        return

    existing_owner = await db.get_voice_owner(control_chat_id)
    if existing_owner is not None and existing_owner != user.id:
        await reply_text(message, "❌ That control group is already registered to another hosted account.")
        return

    try:
        control_chat = await context.bot.get_chat(control_chat_id)
        if control_chat.type not in {"group", "supergroup"}:
            raise ValueError("The supplied ID is not a group or supergroup.")
        if getattr(control_chat, "username", None):
            raise ValueError("The control group must be private and have no public username.")
        bot_user = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(control_chat_id, bot_user.id)
        if bot_member.status not in {"administrator", "creator"}:
            raise ValueError("The control bot must be an administrator in that group.")
        if bot_member.status == "administrator" and not getattr(
            bot_member, "can_manage_video_chats", False
        ):
            raise ValueError("The bot needs the 'Manage Voice Chats' administrator permission.")
    except Exception as exc:
        await reply_text(
            message,
            f"❌ Could not verify that group: {html.escape(str(exc))}\n"
            "Add the bot as an admin, then try again.",
        )
        return

    await db.set_voice_control(user.id, control_chat_id, source_chat_id)
    hosted = manager.get_client(user.id)
    if hosted is not None and hosted.voice_chat is not None:
        await hosted.voice_chat.configure(control_chat_id, source_chat_id)
    await reply_html(
        message,
        "✅ <b>Private VC control group registered.</b>\n\n"
        f"Control group: <code>{control_chat_id}</code>\n"
        f"Source/personal VC: <code>{source_chat_id}</code>\n\n"
        "Only you and administrators of that registered group can use "
        "<code>/join</code>, <code>/leave</code>, <code>/level</code>, "
        "<code>/bass</code>, <code>/mute</code>, <code>/unmute</code>, "
        "<code>/startrecord</code>, <code>/stoprecord</code>, and "
        "<code>/speedtest</code> there.",
    )


def build_private_vc_setup_handler() -> CommandHandler:
    return CommandHandler("privategroupvcsetup", privategroupvcsetup_command)