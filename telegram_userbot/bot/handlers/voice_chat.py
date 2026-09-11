"""Private control-bot handlers for the hosted account's Voice Chat."""

from __future__ import annotations

import html
import re
import shutil
import tempfile
from pathlib import Path

from pytgcalls.exceptions import NoActiveGroupCall
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
from utils.message_ui import reply_html
import database.mongo as db


_VOICE_COMMAND_RE = re.compile(
    r"^\s*[./](?P<command>"
    r"vcjoin|vcstatus|vcstop|vcleave|join|leave|leaveall|leaveplay|"
    r"play|pause|resume|queue|clearqueue|volume|level|bass|mute|unmute|"
    r"startrecord|stoprecord|speedtest"
    r")"
    r"(?:@[A-Za-z0-9_]+)?"
    r"(?:\s+(?P<args>.*?))?\s*$",
    re.IGNORECASE,
)


async def _voice_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve only the hosted client bound to this registered control group."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return None
    if message.chat is None or message.chat.type not in {"group", "supergroup"}:
        return None

    manager = context.bot_data.get("manager")
    owner_id = await db.get_voice_owner(message.chat_id)
    if owner_id is None or manager is None:
        return None
    if user.id != owner_id:
        try:
            member = await context.bot.get_chat_member(message.chat_id, user.id)
        except Exception:
            return None
        if member.status not in {"administrator", "creator"}:
            return None
    hosted = manager.get_client(owner_id)
    if hosted is None or not hosted.is_running():
        return None
    return getattr(hosted.client, "_voice_chat_manager", None)


def _command(message) -> tuple[str, str] | None:
    match = _VOICE_COMMAND_RE.match(message.text or "")
    if match is None:
        return None
    return match.group("command").lower(), (match.group("args") or "").strip()


def _reply_media(message):
    reply = message.reply_to_message
    if reply is None:
        return None
    for attribute in ("audio", "voice", "video", "document"):
        media = getattr(reply, attribute, None)
        if media is None:
            continue
        if attribute == "document" and not str(
            getattr(media, "mime_type", "") or ""
        ).startswith("audio/"):
            continue
        return media, reply
    return None


async def _download_reply_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Path, str, str]:
    message = update.effective_message
    media_info = _reply_media(message)
    if media_info is None:
        raise ValueError(
            "Reply to an audio, voice, or video message with .play."
        )

    media, reply = media_info
    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise ValueError("The replied media has no downloadable audio file.")

    filename = (
        getattr(media, "file_name", None)
        or getattr(media, "title", None)
        or f"voice-chat-{reply.message_id}.audio"
    )
    filename = Path(str(filename)).name or f"voice-chat-{reply.message_id}.audio"
    temp_dir = Path(tempfile.mkdtemp(prefix="control-vc-"))
    destination = temp_dir / filename
    try:
        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=destination)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("Telegram returned an empty audio file.")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    title = (
        getattr(media, "title", None)
        or getattr(media, "file_name", None)
        or "Telegram audio"
    )
    return destination, str(title), f"control-bot-message:{reply.message_id}"


async def _reply_error(message, exc: Exception) -> None:
    if isinstance(exc, NoActiveGroupCall):
        text = "❌ No active Voice Chat found."
    else:
        text = f"❌ {html.escape(str(exc))}"
    await reply_html(message, text)


async def voice_chat_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    parsed = _command(message)
    if parsed is None:
        return

    voice = await _voice_manager(update, context)
    # Ignore commands outside the registered control group or from users who
    # are not the owner/group administrator.
    if voice is None:
        return

    command, args = parsed
    try:
        if command in {"vcjoin", "join"}:
            text = await voice.join_target(args)
        elif command in {"vcstatus"}:
            text = await voice.control_status()
        elif command == "vcstop":
            text = await voice.stop_playback(args or None)
        elif command in {"vcleave", "leave"}:
            text = await voice.leave(args or None)
        elif command == "leaveall":
            text = await voice.leave_all()
        elif command == "leaveplay":
            text = await voice.leave_playback_only(args or None)
        elif command == "play":
            if args:
                raise ValueError(
                    "Reply to an audio, voice, or video message with .play."
                )
            path, title, source = await _download_reply_audio(update, context)
            try:
                async def notify_playback_complete() -> None:
                    await context.bot.send_message(
                        chat_id=message.chat_id,
                        text=f"✅ Playback finished: {title}",
                    )

                text = await voice.enqueue_file(
                    path,
                    title,
                    source,
                    on_complete=notify_playback_complete,
                )
            finally:
                shutil.rmtree(path.parent, ignore_errors=True)
        elif command == "pause":
            text = await voice.pause(args or None)
        elif command == "resume":
            text = await voice.resume(args or None)
        elif command == "queue":
            text = await voice.queue_text(args or None)
        elif command == "clearqueue":
            text = await voice.clear_queue(args or None)
        elif command == "volume":
            try:
                value = int(args)
            except ValueError as exc:
                raise ValueError("Usage: .volume <0-100000000>") from exc
            text = await voice.change_volume(value, None)
        elif command == "level":
            parts = args.split()
            if not parts:
                raise ValueError("Usage: /level <1-25> [target_chat_id]")
            text = await voice.set_level(int(parts[0]), parts[1] if len(parts) > 1 else None)
        elif command == "bass":
            parts = args.split()
            if not parts:
                raise ValueError("Usage: /bass <0-15> [target_chat_id]")
            text = await voice.set_bass(int(parts[0]), parts[1] if len(parts) > 1 else None)
        elif command == "mute":
            text = await voice.set_mute(True, args or None)
        elif command == "unmute":
            text = await voice.set_mute(False, args or None)
        elif command == "startrecord":
            path = await voice.start_recording(args or None)
            text = f"🔴 Recording started: <code>{html.escape(path.name)}</code>"
        elif command == "stoprecord":
            path = await voice.stop_recording()
            if path is None:
                text = "ℹ️ No recording is in progress."
            else:
                await context.bot.send_audio(
                    chat_id=message.chat_id,
                    audio=path,
                    caption=f"Recorded source audio for VC relay {message.chat_id}.",
                )
                text = "✅ Recording stopped and uploaded."
        elif command == "speedtest":
            text = await voice.speedtest()
        else:  # pragma: no cover - guarded by the command regex
            return
        await reply_html(message, text)
    except Exception as exc:
        await _reply_error(message, exc)


def build_voice_chat_handler() -> MessageHandler:
    """Match only VC commands sent to a registered control group."""
    return MessageHandler(
        filters.ChatType.GROUPS
        & filters.TEXT
        & filters.Regex(_VOICE_COMMAND_RE),
        voice_chat_command,
    )