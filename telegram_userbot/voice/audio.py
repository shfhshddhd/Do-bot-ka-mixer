"""Small, dependency-free PCM16 processor used by the native VC relay.

PyTgCalls exposes decoded speaker frames through ``StreamFrames``.  The
default path deliberately returns those bytes unchanged so music and speech
retain their original quality.  The optional controls apply a bounded gain
and a lightweight low-frequency boost to PCM16 stereo frames.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


def _clamp(value: float) -> int:
    return max(-32768, min(32767, int(round(value))))


@dataclass
class AudioSettings:
    level: int = 5
    bass: int = 0
    muted: bool = False

    def normalized(self) -> "AudioSettings":
        return AudioSettings(
            level=max(1, min(25, int(self.level))),
            bass=max(0, min(15, int(self.bass))),
            muted=bool(self.muted),
        )


class Pcm16Processor:
    """Process one target's audio without changing the relay's frame format."""

    def __init__(self, settings: AudioSettings | None = None) -> None:
        self.settings = (settings or AudioSettings()).normalized()
        self._low_left = 0.0
        self._low_right = 0.0

    def update(self, settings: AudioSettings) -> None:
        self.settings = settings.normalized()

    def process(self, data: bytes) -> bytes:
        settings = self.settings
        if settings.muted:
            return bytes(len(data))
        if settings.level == 5 and settings.bass == 0:
            return data
        if len(data) < 2 or len(data) % 2:
            # Never corrupt an unexpected frame; the native relay can still
            # forward it byte-for-byte.
            return data

        gain = settings.level / 5.0
        bass_gain = settings.bass / 15.0 * 0.75
        output = bytearray(len(data))
        samples = struct.iter_unpack("<h", data)
        for index, (sample,) in enumerate(samples):
            channel = index % 2
            previous = self._low_left if channel == 0 else self._low_right
            low = previous + 0.08 * (sample - previous)
            if channel == 0:
                self._low_left = low
            else:
                self._low_right = low
            processed = sample * gain + low * bass_gain
            struct.pack_into("<h", output, index * 2, _clamp(processed))
        return bytes(output)