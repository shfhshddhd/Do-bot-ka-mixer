import struct
import unittest

from telegram_userbot.voice.audio import AudioSettings, Pcm16Processor


class VoiceAudioTests(unittest.TestCase):
    def test_default_settings_forward_exact_bytes(self):
        payload = struct.pack("<hhhh", 1, -2, 300, -400)
        self.assertEqual(Pcm16Processor().process(payload), payload)

    def test_mute_preserves_frame_length(self):
        payload = bytes(range(12))
        result = Pcm16Processor(AudioSettings(muted=True)).process(payload)
        self.assertEqual(len(result), len(payload))
        self.assertEqual(result, bytes(len(payload)))

    def test_gain_is_clamped_to_pcm16(self):
        payload = struct.pack("<hh", 30000, -30000)
        result = Pcm16Processor(AudioSettings(level=25)).process(payload)
        self.assertEqual(struct.unpack("<hh", result), (32767, -32768))

    def test_settings_are_bounded(self):
        settings = AudioSettings(level=999, bass=-4).normalized()
        self.assertEqual(settings.level, 25)
        self.assertEqual(settings.bass, 0)


if __name__ == "__main__":
    unittest.main()