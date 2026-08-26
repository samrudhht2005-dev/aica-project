"""Focused checks: embedding Stage-2 must accept Whisper 'Here.' mishear."""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock


class WakeHereMishearTests(unittest.TestCase):
    def test_standalone_here_helpers(self) -> None:
        from desktop.launcher.voice_wake_verify import (
            is_standalone_here_mishear,
            verify_wake_transcript,
        )

        self.assertTrue(is_standalone_here_mishear("Here."))
        self.assertTrue(is_standalone_here_mishear("here"))
        self.assertFalse(is_standalone_here_mishear("higher"))
        self.assertFalse(is_standalone_here_mishear("come here"))
        self.assertTrue(verify_wake_transcript("Here.", allow_here_mishear=True))
        self.assertFalse(verify_wake_transcript("Here.", allow_here_mishear=False))

    def _run_whisper_only(self, text: str, margin: float = -0.01):
        from desktop.launcher.voice_wake_verify import try_wake_whisper_only

        pcm = b"\x00\x00" * int(16000 * 0.9)
        fake_stt = MagicMock()
        fake_stt.transcribe_pcm.return_value = {
            "text": text,
            "confidence": 0.82,
            "latency_ms": 12,
        }
        fake_mod = types.ModuleType("desktop.launcher.voice_stt")
        fake_cls = MagicMock()
        fake_cls.get.return_value = fake_stt
        fake_mod.WhisperSTT = fake_cls
        prev = sys.modules.get("desktop.launcher.voice_stt")
        sys.modules["desktop.launcher.voice_stt"] = fake_mod
        try:
            return try_wake_whisper_only(pcm, margin=margin)
        finally:
            if prev is None:
                sys.modules.pop("desktop.launcher.voice_stt", None)
            else:
                sys.modules["desktop.launcher.voice_stt"] = prev

    def test_try_wake_whisper_only_accepts_here(self) -> None:
        fired, score, method, text = self._run_whisper_only("Here.")
        self.assertTrue(fired)
        self.assertEqual(method, "whisper_verify_here")
        self.assertEqual(text, "Here.")
        self.assertAlmostEqual(score, 0.82)

    def test_try_wake_whisper_only_rejects_unrelated(self) -> None:
        fired, _, method, _ = self._run_whisper_only("mamacita")
        self.assertFalse(fired)
        self.assertEqual(method, "none")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
