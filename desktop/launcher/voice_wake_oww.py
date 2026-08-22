"""Optional openWakeWord detector when a real hey_ira.onnx is present."""
from __future__ import annotations

import time
from typing import Callable

import numpy as np

from desktop.launcher.voice_audio import SAMPLE_RATE, VoiceActivityDetector
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import wake_model_path
from desktop.launcher.voice_wake_util import ensure_oww_backbone

OWW_FRAME_SAMPLES = 1280
OWW_FRAME_BYTES = OWW_FRAME_SAMPLES * 2


class OpenWakeWordDetector:
    def __init__(self, threshold: float = 0.45) -> None:
        self._threshold = threshold
        self._model = None
        self._vad = VoiceActivityDetector(aggressiveness=2)

    def ensure_loaded(self) -> None:
        if self._model is not None:
            return
        ensure_oww_backbone()
        from openwakeword.model import Model

        wake = wake_model_path()
        if not wake.is_file():
            raise FileNotFoundError(wake)
        self._model = Model(wakeword_models=[str(wake)], inference_framework="onnx")
        vlog("oww_custom_loaded", model=str(wake))

    def score_frame(self, pcm16: bytes) -> float:
        if len(pcm16) < OWW_FRAME_BYTES:
            return 0.0
        chunk = pcm16[-OWW_FRAME_BYTES:]
        audio = np.frombuffer(chunk, dtype=np.int16)
        pred = self._model.predict(audio)
        return float(max(pred.values())) if pred else 0.0

    def run_loop(
        self,
        *,
        on_wake: Callable[[float], None],
        stop_check: Callable[[], bool],
        stream_frames: Callable[[Callable[[bytes], None], Callable[[], bool]], None],
    ) -> None:
        buffer = bytearray()
        cooldown_until = 0.0
        consecutive_hits = 0

        def handle_frame(pcm: bytes) -> None:
            nonlocal cooldown_until, consecutive_hits
            if time.time() < cooldown_until:
                return
            buffer.extend(pcm)
            max_bytes = SAMPLE_RATE * 2 * 2
            if len(buffer) > max_bytes:
                del buffer[: len(buffer) - max_bytes]
            if not self._vad.is_speech(pcm):
                consecutive_hits = 0
                return
            while len(buffer) >= OWW_FRAME_BYTES:
                frame = bytes(buffer[:OWW_FRAME_BYTES])
                del buffer[:OWW_FRAME_BYTES]
                score = self.score_frame(frame)
                if score >= self._threshold:
                    consecutive_hits += 1
                else:
                    consecutive_hits = max(0, consecutive_hits - 1)
                if consecutive_hits >= 2:
                    cooldown_until = time.time() + 2.0
                    consecutive_hits = 0
                    buffer.clear()
                    on_wake(score)
                    break

        stream_frames(frame_callback=handle_frame, stop_check=stop_check)
