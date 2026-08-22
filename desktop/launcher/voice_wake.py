"""Wake-word detection — VAD-gated short clip + Whisper verify (not continuous dictation)."""
from __future__ import annotations

import time
from typing import Callable

from desktop.launcher.voice_audio import FRAME_SAMPLES, VoiceActivityDetector
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_paths import wake_model_path
from desktop.launcher.voice_stt import WhisperSTT
from desktop.launcher.voice_wake_verify import verify_wake_transcript


class VadWhisperWakeDetector:
    """
    Ambient wake pipeline:
      mic → VAD speech segment (≤ ~2.5 s) → faster-whisper on clip → wake phrase match

    Whisper is NOT run continuously — only once per bounded speech burst.
    """

    def __init__(self) -> None:
        self._vad = VoiceActivityDetector(aggressiveness=2)
        self._cooldown_until = 0.0

    def _is_wake_transcript(self, text: str) -> bool:
        return verify_wake_transcript(text, allow_here_mishear=True)

    def run_loop(
        self,
        *,
        on_wake: Callable[[float], None],
        stop_check: Callable[[], bool],
        stream_frames: Callable[[Callable[[bytes], None], Callable[[], bool]], None],
    ) -> None:
        pcm_buffer = bytearray()
        in_speech = False
        speech_started = 0.0
        max_speech_s = 2.8
        min_speech_s = 0.35
        silence_frames = 0
        silence_limit = 12

        def handle_frame(pcm: bytes) -> None:
            nonlocal in_speech, speech_started, silence_frames
            if time.time() < self._cooldown_until:
                return

            if self._vad.is_speech(pcm):
                if not in_speech:
                    in_speech = True
                    speech_started = time.time()
                    pcm_buffer.clear()
                    silence_frames = 0
                pcm_buffer.extend(pcm)
                silence_frames = 0
                if time.time() - speech_started > max_speech_s:
                    self._maybe_fire(bytes(pcm_buffer), on_wake)
                    in_speech = False
                    pcm_buffer.clear()
                return

            if not in_speech:
                return

            pcm_buffer.extend(pcm)
            silence_frames += 1
            if silence_frames >= silence_limit:
                duration = time.time() - speech_started
                if duration >= min_speech_s:
                    self._maybe_fire(bytes(pcm_buffer), on_wake)
                in_speech = False
                pcm_buffer.clear()
                silence_frames = 0

        stream_frames(frame_callback=handle_frame, stop_check=stop_check)

    def _maybe_fire(self, pcm: bytes, on_wake: Callable[[float], None]) -> None:
        if len(pcm) < FRAME_SAMPLES * 2:
            return
        try:
            result = WhisperSTT.get().transcribe_pcm(pcm)
            text = (result.get("text") or "").strip()
            vlog("wake_clip_transcript", text=text, ms=result.get("latency_ms"))
            if text and self._is_wake_transcript(text):
                self._cooldown_until = time.time() + 2.5
                conf = result.get("confidence") or 0.75
                on_wake(float(conf))
        except Exception as e:
            vlog("wake_clip_error", error=str(e))


class WakeDetector:
    """Facade — embedding refs, custom OWW onnx, or VAD+Whisper verify fallback."""

    def __init__(self, threshold: float = 0.45) -> None:
        self._threshold = threshold
        self._backend = None
        self._mode = "vad_whisper"
        self._vad_whisper = VadWhisperWakeDetector()

    def ensure_loaded(self) -> None:
        import json

        from desktop.launcher.voice_paths import voice_models_dir

        wake_path = wake_model_path()
        meta_path = wake_path.with_suffix(".json")
        meta: dict = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        embed_npz = voice_models_dir() / "hey_ira_embeddings.npz"
        if embed_npz.is_file() and meta.get("type") == "embedding_references":
            try:
                from desktop.launcher.voice_wake_embed import EmbeddingWakeDetector

                self._backend = EmbeddingWakeDetector(margin_threshold=meta.get("margin_threshold"))
                self._backend.ensure_loaded()
                self._mode = "embedding"
                vlog("wake_backend", mode="embedding")
                return
            except Exception as e:
                vlog("wake_embed_fallback", error=str(e))

        if wake_path.is_file() and meta_path.is_file():
            if meta.get("type") not in ("embedding_threshold_bootstrap", "embedding_references"):
                try:
                    from desktop.launcher.voice_wake_oww import OpenWakeWordDetector

                    self._backend = OpenWakeWordDetector(threshold=self._threshold)
                    self._backend.ensure_loaded()
                    self._mode = "openwakeword"
                    vlog("wake_backend", mode="openwakeword")
                    return
                except Exception as e:
                    vlog("oww_load_fallback", error=str(e))

        WhisperSTT.get().ensure_loaded()
        self._backend = self._vad_whisper
        self._mode = "vad_whisper"
        vlog("wake_backend", mode="vad_whisper")

    def run_loop(
        self,
        *,
        on_wake: Callable[[float], None],
        stop_check: Callable[[], bool],
        stream_frames: Callable[[Callable[[bytes], None], Callable[[], bool]], None],
    ) -> None:
        if self._backend is None:
            self.ensure_loaded()
        assert self._backend is not None
        self._backend.run_loop(on_wake=on_wake, stop_check=stop_check, stream_frames=stream_frames)
