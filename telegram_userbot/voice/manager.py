"""Native PyTgCalls voice-chat relay for one hosted Telethon session.

There is exactly one ``PyTgCalls`` instance per hosted ``TelegramClient``.
The source call is joined once, decoded incoming speaker frames are received
through ``StreamFrames``, and the same frames are sent to every target call
with ``send_frame``.  This avoids a second Telegram session and avoids the
fragile PulseAudio/FFmpeg loop that the reference project used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from pytgcalls import PyTgCalls
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
from pytgcalls.types import (
    AudioQuality,
    Device,
    Direction,
    ExternalMedia,
    Frame,
    GroupCallConfig,
    MediaStream,
    RecordStream,
    StreamFrames,
)

import config
import database.mongo as db
from .audio import AudioSettings, Pcm16Processor

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL = 10


@dataclass
class RelaySession:
    target_chat_id: int
    settings: AudioSettings = field(default_factory=AudioSettings)
    processor: Pcm16Processor = field(default_factory=Pcm16Processor)
    watchdog: asyncio.Task | None = None
    reconnecting: bool = False

    def __post_init__(self) -> None:
        self.processor.update(self.settings)


class VoiceChatManager:
    """Owns all voice resources belonging to one hosted Telegram account."""

    def __init__(self, user_id: int, client) -> None:
        self.user_id = user_id
        self.client = client
        self.calls = PyTgCalls(client)
        self.control_chat_id: int | None = None
        self.source_chat_id: int | None = None
        self.sessions: dict[int, RelaySession] = {}
        self._source_joined = False
        self._started = False
        self._frame_lock = asyncio.Lock()
        self._recording_path: Path | None = None
        self._playback_tasks: set[asyncio.Task] = set()
        self._live_subscribers: dict[int, set[asyncio.Queue]] = {}
        self._live_active: set[int] = set()
        self._frame_handler = self._on_stream_frames

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.calls.start()
        self.calls.add_handler(self._frame_handler)
        voice_config = await db.get_voice_control(self.user_id)
        if voice_config:
            self.control_chat_id = int(voice_config["control_chat_id"])
            self.source_chat_id = int(voice_config["source_chat_id"])
            for item in await db.get_voice_sessions(self.user_id):
                try:
                    target_id = int(item["target_chat_id"])
                    await self.join_target(target_id, persist=False)
                    session = self.sessions[target_id]
                    session.settings = AudioSettings(
                        level=item.get("level", 5),
                        bass=item.get("bass", 0),
                        muted=item.get("muted", False),
                    ).normalized()
                    session.processor.update(session.settings)
                except Exception:
                    logger.exception(
                        "Could not restore VC relay target %s for user %s",
                        item.get("target_chat_id"),
                        self.user_id,
                    )

    async def shutdown(self) -> None:
        """Stop watchdogs, recording, calls, and frame forwarding."""
        if not self._started:
            return
        for session in list(self.sessions.values()):
            if session.watchdog:
                session.watchdog.cancel()
        for task in self._playback_tasks:
            task.cancel()
        await asyncio.gather(
            *(
                [session.watchdog for session in self.sessions.values() if session.watchdog]
                + list(self._playback_tasks)
            ),
            return_exceptions=True,
        )
        self._playback_tasks.clear()
        self.sessions.clear()
        self._live_subscribers.clear()
        self._live_active.clear()
        if self._recording_path is not None:
            self._recording_path = None
        for chat_id in list(self._active_call_ids()):
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                logger.debug("Call %s was already gone during shutdown", chat_id)
        self._source_joined = False
        self.calls.remove_handler(self._frame_handler)
        self._started = False
        await db.clear_voice_sessions(self.user_id)

    async def configure(self, control_chat_id: int, source_chat_id: int | None = None) -> None:
        """Bind the manager to one control group and its source VC group."""
        source_chat_id = int(source_chat_id or control_chat_id)
        control_chat_id = int(control_chat_id)
        if self.sessions and self.source_chat_id != source_chat_id:
            await self.leave_all(persist=False)
        self.control_chat_id = control_chat_id
        self.source_chat_id = source_chat_id

    def is_configured(self) -> bool:
        return self.control_chat_id is not None and self.source_chat_id is not None

    def _active_call_ids(self) -> set[int]:
        ids = set(self.sessions)
        if self._source_joined and self.source_chat_id is not None:
            ids.add(self.source_chat_id)
        return ids

    @staticmethod
    def _media_stream() -> MediaStream:
        return MediaStream(
            ExternalMedia.AUDIO,
            AudioQuality.STUDIO,
            video_flags=MediaStream.Flags.IGNORE,
        )

    async def _join_call(self, chat_id: int) -> None:
        await self.calls.play(
            int(chat_id),
            self._media_stream(),
            GroupCallConfig(auto_start=False),
        )

    async def _ensure_source(self) -> None:
        if not self.is_configured():
            raise RuntimeError("Run /privategroupvcsetup before using VC relay commands.")
        if self.source_chat_id in self.sessions:
            raise RuntimeError("The control/source group cannot also be a relay target.")
        if not self._source_joined:
            await self._join_call(self.source_chat_id)
            self._source_joined = True

    async def join_target(self, target: int | str, *, persist: bool = True) -> str:
        try:
            target_id = int(str(target).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Usage: /join <target_group_chat_id>") from exc
        if target_id >= 0:
            raise ValueError("Use a group or supergroup chat ID, usually beginning with -100.")
        if self.source_chat_id == target_id:
            raise ValueError("The target VC must be different from the source/personal VC.")
        existing = self.sessions.get(target_id)
        if existing is not None:
            return f"✅ Relay to <code>{target_id}</code> is already active."

        await self._ensure_source()
        session = RelaySession(target_id)
        try:
            await self._join_call(target_id)
        except Exception:
            if not self.sessions:
                await self._leave_source()
            raise
        self.sessions[target_id] = session
        session.watchdog = asyncio.create_task(self._watchdog(session))
        if persist:
            await self._persist_sessions()
        return (
            f"✅ Joined source VC <code>{self.source_chat_id}</code> and target VC "
            f"<code>{target_id}</code>.\n"
            "🔊 Live source audio is now forwarded to the target."
        )

    async def _leave_source(self) -> None:
        if not self._source_joined or self.source_chat_id is None:
            return
        try:
            await self.calls.leave_call(self.source_chat_id)
        except (NoActiveGroupCall, NotInCallError):
            pass
        except Exception:
            logger.debug("Source call was already gone", exc_info=True)
        self._source_joined = False

    async def leave(self, target: int | str | None = None, *, persist: bool = True) -> str:
        target_id = self._resolve_target(target)
        if target_id is None:
            return "ℹ️ No active target VC session."
        session = self.sessions.pop(target_id, None)
        if session is None:
            return f"ℹ️ No active relay for <code>{target_id}</code>."
        if session.watchdog:
            session.watchdog.cancel()
        try:
            await self.calls.leave_call(target_id)
        except Exception:
            logger.debug("Target call %s was already gone", target_id)
        if not self.sessions:
            await self._leave_source()
        if persist:
            await self._persist_sessions()
        return f"👋 Left target VC <code>{target_id}</code> and cleaned its relay."

    async def leave_all(self, *, persist: bool = True) -> str:
        count = len(self.sessions)
        for target_id in list(self.sessions):
            await self.leave(target_id, persist=False)
        await self._leave_source()
        if persist:
            await self._persist_sessions()
        return f"👋 Left all active target VCs ({count} total)."

    async def leave_playback_only(self, target: int | str | None = None) -> str:
        # The native relay has one incoming source and one outgoing stream per
        # target. Leaving a target is the exact playback-only operation.
        return await self.leave(target)

    def _resolve_target(self, target: int | str | None) -> int | None:
        if target is not None and str(target).strip():
            try:
                return int(str(target).strip())
            except ValueError as exc:
                raise ValueError("The chat ID must be an integer.") from exc
        if len(self.sessions) == 1:
            return next(iter(self.sessions))
        return None

    async def _persist_sessions(self) -> None:
        await db.save_voice_sessions(
            self.user_id,
            [
                {
                    "target_chat_id": target_id,
                    "level": session.settings.level,
                    "bass": session.settings.bass,
                    "muted": session.settings.muted,
                }
                for target_id, session in self.sessions.items()
            ],
        )

    async def _on_stream_frames(self, _calls, update: StreamFrames) -> None:
        if (
            not self._source_joined
            or self.source_chat_id is None
            or update.chat_id != self.source_chat_id
            or update.direction is not Direction.INCOMING
            or update.device is not Device.SPEAKER
        ):
            return
        async with self._frame_lock:
            sessions = list(self.sessions.values())
            for frame in update.frames:
                for queue in list(self._live_subscribers.get(update.chat_id, ())):
                    if not queue.full():
                        queue.put_nowait(frame.frame)
                await asyncio.gather(
                    *(
                        self._send_frame(session, frame)
                        for session in sessions
                    ),
                    return_exceptions=True,
                )

    async def _send_frame(self, session: RelaySession, frame: Frame) -> None:
        data = session.processor.process(frame.frame)
        try:
            await self.calls.send_frame(
                session.target_chat_id,
                Device.MICROPHONE,
                data,
                frame.info,
            )
        except Exception:
            logger.debug(
                "Could not forward one source frame to target %s",
                session.target_chat_id,
                exc_info=True,
            )

    async def set_level(self, level: int, target: int | str | None = None) -> str:
        target_id = self._resolve_target(target)
        if target_id is None or target_id not in self.sessions:
            raise ValueError("Specify a target ID when more than one relay is active.")
        if not 1 <= int(level) <= 25:
            raise ValueError("Level must be between 1 and 25.")
        session = self.sessions[target_id]
        session.settings.level = int(level)
        session.processor.update(session.settings)
        await self._persist_sessions()
        return f"🔊 Level set to <b>{level}/25</b> for <code>{target_id}</code>."

    async def set_bass(self, bass: int, target: int | str | None = None) -> str:
        target_id = self._resolve_target(target)
        if target_id is None or target_id not in self.sessions:
            raise ValueError("Specify a target ID when more than one relay is active.")
        if not 0 <= int(bass) <= 15:
            raise ValueError("Bass must be between 0 and 15.")
        session = self.sessions[target_id]
        session.settings.bass = int(bass)
        session.processor.update(session.settings)
        await self._persist_sessions()
        return f"🎚 Bass set to <b>{bass}/15</b> for <code>{target_id}</code>."

    async def set_mute(self, muted: bool, target: int | str | None = None) -> str:
        target_id = self._resolve_target(target)
        if target_id is None or target_id not in self.sessions:
            raise ValueError("Specify a target ID when more than one relay is active.")
        session = self.sessions[target_id]
        session.settings.muted = bool(muted)
        session.processor.update(session.settings)
        await self._persist_sessions()
        return (
            f"{'🔇 Muted' if muted else '🔊 Unmuted'} target "
            f"<code>{target_id}</code>."
        )

    async def control_status(self) -> str:
        source = self.source_chat_id or "not configured"
        if not self.sessions:
            return f"ℹ️ Source VC: <code>{source}</code>\nNo target relays are active."
        targets = "\n".join(
            f"• <code>{target}</code> — level {item.settings.level}, "
            f"bass {item.settings.bass}, "
            f"{'muted' if item.settings.muted else 'live'}"
            for target, item in self.sessions.items()
        )
        return f"🎙 Source VC: <code>{source}</code>\n{targets}"

    async def start_recording(self, target: int | str | None = None) -> Path:
        if self.source_chat_id is None or not self._source_joined:
            raise RuntimeError("Join a target VC first so the source VC is active.")
        if self._recording_path is not None:
            raise RuntimeError("A recording is already in progress.")
        target_id = self._resolve_target(target)
        if target is not None and target_id not in self.sessions:
            raise ValueError("That target relay is not active.")
        record_dir = Path(getattr(config, "VOICE_RECORDINGS_DIR", "/tmp/telegram-vc-recordings"))
        record_dir.mkdir(parents=True, exist_ok=True)
        path = record_dir / f"vc_{self.user_id}_{int(time.time())}.mp3"
        await self.calls.record(
            self.source_chat_id,
            RecordStream(audio=path, audio_parameters=AudioQuality.STUDIO),
        )
        self._recording_path = path
        return path

    async def stop_recording(self) -> Path | None:
        path = self._recording_path
        if path is None:
            return None
        self._recording_path = None
        # Rejoining the source call flushes PyTgCalls' recorder cleanly and
        # restores the external-audio source used by the relay.
        targets = list(self.sessions)
        await self._leave_source()
        if targets:
            await self._ensure_source()
        await asyncio.sleep(0.2)
        return path if path.exists() else None

    async def pause(self, target: int | str | None = None) -> str:
        target_id = self._resolve_target(target)
        if target_id is None:
            raise ValueError("Specify a target ID when more than one relay is active.")
        await self.calls.pause(target_id)
        return f"⏸️ Paused target playback <code>{target_id}</code>."

    async def resume(self, target: int | str | None = None) -> str:
        target_id = self._resolve_target(target)
        if target_id is None:
            raise ValueError("Specify a target ID when more than one relay is active.")
        await self.calls.resume(target_id)
        return f"▶️ Resumed target playback <code>{target_id}</code>."

    async def queue_text(self, _target: int | str | None = None) -> str:
        return "ℹ️ Native VC relay has no queued file playback."

    async def clear_queue(self, _target: int | str | None = None) -> str:
        return "✅ Playback queue is already clear."

    async def change_volume(
        self,
        value_or_chat_id: int,
        target_or_value: int | str | None = None,
    ) -> str:
        # Keep the existing .volume command useful while routing all audio
        # controls through the same per-target PCM processor.
        if target_or_value is not None and int(value_or_chat_id) in self.sessions:
            target = value_or_chat_id
            value = int(target_or_value)
        else:
            target = None
            value = int(value_or_chat_id)
        mapped = max(1, min(25, round(value / 4_000_000) or 1))
        return await self.set_level(mapped, target)

    @property
    def state(self):
        """Compatibility view consumed by the existing Mini App."""
        target_id = self._resolve_target(None)
        if target_id is None:
            return None
        session = self.sessions[target_id]
        return SimpleNamespace(
            chat_id=target_id,
            chat_title=str(target_id),
            current=None,
            queue=[],
            volume=session.settings.level * 4_000_000,
            muted=session.settings.muted,
            live_active=target_id in self._live_active,
        )

    def live_snapshot(self, chat_id: int | None) -> dict:
        return {
            "active": bool(chat_id in self._live_active if chat_id is not None else False),
            "mic_enabled": bool(chat_id in self._live_active if chat_id is not None else False),
            "push_to_talk": False,
            "push_active": False,
            "frames": 0,
            "bytes": 0,
            "started_at": None,
            "last_frame_at": None,
        }

    def subscribe_receive(self, chat_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._live_subscribers.setdefault(int(chat_id), set()).add(queue)
        return queue

    async def send_live_frame(self, chat_id: int, data: bytes) -> None:
        await self.calls.send_frame(int(chat_id), Device.MICROPHONE, data)

    async def start_live(self, chat_id: int) -> None:
        if int(chat_id) not in self.sessions:
            raise RuntimeError("Join an active target VC first.")
        self._live_active.add(int(chat_id))

    async def stop_live(self, chat_id: int) -> None:
        self._live_active.discard(int(chat_id))

    def set_live_controls(self, chat_id: int, **_controls) -> None:
        if int(chat_id) not in self.sessions:
            raise RuntimeError("Join an active target VC first.")

    async def enqueue_file(
        self,
        path: Path,
        title: str,
        source: str,
        on_complete=None,
    ) -> str:
        """Play one replied Telegram audio file, then restore live relay."""
        target_id = self._resolve_target(None)
        if target_id is None:
            raise ValueError("Join exactly one target VC before using .play.")
        playback_dir = Path(getattr(config, "VOICE_RECORDINGS_DIR", "/tmp")) / "playback"
        playback_dir.mkdir(parents=True, exist_ok=True)
        saved_path = playback_dir / f"{self.user_id}-{int(time.time() * 1000)}-{path.name}"
        shutil.copy2(path, saved_path)
        task = asyncio.create_task(
            self._play_file(
                target_id,
                saved_path,
                title,
                on_complete,
            )
        )
        self._playback_tasks.add(task)
        task.add_done_callback(self._playback_tasks.discard)
        return f"▶️ Queued <b>{title}</b> for target <code>{target_id}</code>."

    async def _play_file(self, target_id: int, path: Path, title: str, on_complete) -> None:
        try:
            await self.calls.play(
                target_id,
                MediaStream(path, AudioQuality.STUDIO, video_flags=MediaStream.Flags.IGNORE),
                GroupCallConfig(auto_start=False),
            )
            duration = await self._media_duration(path)
            await asyncio.sleep(max(duration, 1.0) + 0.25)
            if target_id in self.sessions:
                await self.calls.play(
                    target_id,
                    self._media_stream(),
                    GroupCallConfig(auto_start=False),
                )
            if on_complete is not None:
                await on_complete()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Playback failed for %s", path)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    async def _media_duration(path: Path) -> float:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        try:
            return float(stdout.decode().strip())
        except (ValueError, UnicodeDecodeError):
            return 1.0

    async def speedtest(self) -> str:
        def run() -> dict:
            import speedtest

            tester = speedtest.Speedtest()
            tester.get_best_server()
            return {
                "ping": tester.results.ping,
                "download": tester.download() / 1_000_000,
                "upload": tester.upload() / 1_000_000,
                "server": tester.results.server.get("sponsor", "Unknown"),
            }

        result = await asyncio.to_thread(run)
        return (
            "📡 <b>Speed test</b>\n"
            f"Server: {result['server']}\n"
            f"Ping: {result['ping']:.2f} ms\n"
            f"Download: {result['download']:.2f} Mbps\n"
            f"Upload: {result['upload']:.2f} Mbps"
        )

    async def _watchdog(self, session: RelaySession) -> None:
        while session.target_chat_id in self.sessions:
            await asyncio.sleep(RECONNECT_INTERVAL)
            if session.target_chat_id not in self.sessions or session.reconnecting:
                continue
            try:
                await self.calls.time(session.target_chat_id, Direction.OUTGOING)
                if self.source_chat_id is not None:
                    await self.calls.time(self.source_chat_id, Direction.INCOMING)
            except Exception:
                session.reconnecting = True
                try:
                    if session.target_chat_id in self.sessions:
                        await self.calls.leave_call(session.target_chat_id)
                    if self.source_chat_id is not None:
                        await self._leave_source()
                    await self._ensure_source()
                    await self._join_call(session.target_chat_id)
                    logger.info("Reconnected VC relay target %s", session.target_chat_id)
                except Exception:
                    logger.exception("VC relay reconnect failed for %s", session.target_chat_id)
                finally:
                    session.reconnecting = False