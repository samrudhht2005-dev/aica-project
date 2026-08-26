"""Focused tests for Piper TTS path resolution and cancel generation IDs."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class PiperPathTests(unittest.TestCase):
    def test_piper_paths_resolve_under_repo_models(self) -> None:
        from desktop.launcher.voice_paths import (
            piper_espeak_data_dir,
            piper_voice_config_path,
            piper_voice_model_path,
        )

        model = piper_voice_model_path()
        config = piper_voice_config_path()
        self.assertTrue(str(model).endswith("en_US-amy-medium.onnx"))
        self.assertTrue(str(config).endswith("en_US-amy-medium.onnx.json"))
        self.assertEqual(config, Path(str(model) + ".json"))
        # Model should exist in this workspace after integration copy.
        self.assertTrue(model.is_file(), f"missing model: {model}")
        self.assertTrue(config.is_file(), f"missing config: {config}")
        espeak = piper_espeak_data_dir()
        self.assertTrue(espeak.is_dir(), f"missing espeak data: {espeak}")


class NativeTtsCancelTests(unittest.TestCase):
    def test_cancel_bumps_generation_and_stops(self) -> None:
        from desktop.launcher.voice_tts import NativeTTS

        tts = NativeTTS()
        tts._engine = "piper"
        tts._piper = object()  # sentinel — speak paths mocked
        tts._busy.set()

        with patch("sounddevice.stop") as stop_mock:
            out = tts.cancel()
        self.assertTrue(out.get("ok"))
        self.assertTrue(out.get("cancelled"))
        self.assertEqual(tts._speak_gen, 1)
        self.assertFalse(tts._busy.is_set())
        stop_mock.assert_called()

    def test_speak_async_supersedes_previous_gen(self) -> None:
        from desktop.launcher import voice_tts as vt

        tts = vt.NativeTTS()
        calls: list[int] = []

        def fake_load():
            tts._engine = "piper"
            tts._piper = MagicMock()
            return {"ok": True, "engine": "piper"}

        def fake_synth(text: str):
            import numpy as np

            return np.zeros(2205, dtype=np.float32), 22050

        def fake_play(audio, sample_rate, gen, *, blocking):
            calls.append(gen)

        tts.ensure_loaded = fake_load  # type: ignore[method-assign]
        tts._synthesize_piper_float32 = fake_synth  # type: ignore[method-assign]
        tts._play_float32 = fake_play  # type: ignore[method-assign]

        r1 = tts.speak_async("Sure, opening sales history.")
        r2 = tts.speak_async("Opening the warehouse.")
        self.assertTrue(r1.get("ok"))
        self.assertTrue(r2.get("ok"))
        self.assertEqual(r1.get("gen"), 1)
        self.assertEqual(r2.get("gen"), 2)
        self.assertEqual(calls, [1, 2])

        # Cancel invalidates further playback of old gens.
        tts.cancel()
        self.assertEqual(tts._speak_gen, 3)


class SoftAckRoutingContractTests(unittest.TestCase):
    """Lightweight contract: softAck prefers native async when desktop APIs exist."""

    def test_assistant_softack_mentions_native_async(self) -> None:
        src = (ROOT / "frontend" / "static" / "assistant.js").read_text(encoding="utf-8")
        self.assertIn("speak_response_async", src)
        self.assertIn("function softAck", src)
        # softAck must attempt native path before speechSynthesis-only return.
        idx_soft = src.index("function softAck")
        chunk = src[idx_soft : idx_soft + 900]
        self.assertIn("speak_response_async", chunk)
        self.assertIn("hasNativeTts", chunk)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
