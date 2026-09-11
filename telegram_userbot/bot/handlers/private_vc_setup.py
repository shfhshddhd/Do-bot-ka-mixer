"""Registration and lifecycle controls for one private VC setup per hosted account."""

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
    "Create a private group, add this bot as an administrator with Manage Voice Chats, "
    "add the hosted Telegram account, and start its Voice Chat.\n\n"
    "Usage:\n"
    "<code>/privategroupvcsetup &lt;private_group_chat_id&gt;</code>\n"
    "The same group is the source VC and command/control group.\n\n"
    "Legacy separate-source form:\n"
    "<code>/privategroupvcsetup &lt;control_group_id&gt; &lt;source_vc_group_id&gt;</code>\n\n"
    "Remove the setup without unhosting with <code>/privategroupvcunlink</code>."
)

PRIVATE_COMMANDS = (
    "<b>Private VC commands</b>\n"
    "<code>/join &lt;target_group_id&gt;</code> — join source and target VCs\n"
    "<code>/leave [target_group_id]</code> — leave one private relay\n"
    "<code>/leaveall</code> — leave every private target and clean forwarding\n"
    "<code>/leaveplay [target_group_id]</code> — stop one private relay\n"
    "<code>/level &lt;1-25&gt; [target_group_id]</code> — set target gain\n"
    "<code>/bass &lt;0-15&gt; [target_group_id]</code> — set target bass\n"
    "<code>/mute</code> / <code>/unmute</code> — private forwarding audio\n"
    "<code>/startrecord</code> / <code>/stoprecord</code> — record source audio\n"
    "<code>/speedtest</code> — run a non-blocking speed test\n"
    "<code>/privategroupvcunlink</code> — remove setup and clean its VCs"
)


async def _resolve_hosted_group(client, chat_id: int, label: str):
    entity = await client.get_entity(int(chat_id))
    if getattr(entity, "broadcast", False) or not getattr(entity, "title", None):
        raise ValueError(f"The hosted account cannot use the {label} ID as a group.")
    return entity


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

    for chat_id, label in ((control_chat_id, "control"), (source_chat_id, "source")):
        owner = await db.get_voice_owner(chat_id)
        if owner is not None and owner != user.id:
            await reply_text(message, f"❌ That {label} group is already registered to another hosted account.")
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
        await _resolve_hosted_group(hosted.client, control_chat_id, "control group")
        await _resolve_hosted_group(hosted.client, source_chat_id, "source group")
    except Exception as exc:
        await reply_text(
            message,
            f"❌ Could not verify that group: {html.escape(str(exc))}\n"
            "Add the bot as an admin, then try again.",
        )
        return

    current = await db.get_voice_control(user.id)
    if current and (
        current["control_chat_id"] != control_chat_id
        or current["source_chat_id"] != source_chat_id
    ):
        if hosted.voice_chat is not None:
            await hosted.voice_chat.leave_all()
        await db.clear_voice_control(user.id)

    await db.set_voice_control(user.id, control_chat_id, source_chat_id)
    if hosted.voice_chat is not None:
        await hosted.voice_chat.configure(control_chat_id, source_chat_id)
    await reply_html(
        message,
        "✅ <b>Private VC control group registered.</b>\n\n"
        f"Control group: <code>{control_chat_id}</code>\n"
        f"Source/personal VC: <code>{source_chat_id}</code>\n\n"
        f"{PRIVATE_COMMANDS}\n\n"
        "Only you and administrators of that registered group can control it. "
        "The hosted account/session remains intact until you explicitly use /unhost.",
    )



async def privategroupvcunlink_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    manager = context.bot_data["manager"]
    if message is None or user is None:
        return
    hosted = manager.get_client(user.id)
    if hosted is None or not hosted.is_running():
        await reply_text(message, "❌ You do not have an active hosted account.")
        return
    current = await db.get_voice_control(user.id)
    if current is None:
        await reply_text(message, "ℹ️ No Private VC setup is registered.")
        return
    if hosted.voice_chat is not None:
        await hosted.voice_chat.leave_all()
    await db.clear_voice_control(user.id)
    await reply_html(
        message,
        "✅ <b>Private VC setup removed.</b>\n\n"
        f"Unlinked group: <code>{current['control_chat_id']}</code>\n"
        "Private source/target relays and saved configuration were cleaned up.\n"
        "Your hosted account, session, normal VC mode, and normal playback remain available.",
    )

def build_private_vc_setup_handler() -> CommandHandler:
    return CommandHandler("privategroupvcsetup", privategroupvcsetup_command)