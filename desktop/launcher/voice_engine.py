"""
AICA voice engine v2 — faster-whisper + openWakeWord + native TTS.

Orchestrates mic capture, wake, STT, and events for pywebview.
Falls back to LegacySpeechBackend when AICA_VOICE_BACKEND=legacy or on fatal load error.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from desktop.launcher.voice_audio import MicCapture
from desktop.launcher.voice_intents import detect_wake, match_intent
from desktop.launcher.voice_legacy import LegacySpeechBackend
from desktop.launcher.voice_log import vlog
from desktop.launcher.voice_stt import WhisperSTT
from desktop.launcher.voice_tts import NativeTTS
from desktop.launcher.voice_wake import WakeDetector


def _use_legacy() -> bool:
    return (os.environ.get("AICA_VOICE_BACKEND") or "").strip().lower() in (
        "legacy",
        "system-speech",
        "windows-system-speech",
    )


class ModernVoiceEngine:
    BACKEND_NAME = "aica-voice-v2"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._mic = MicCapture()
        self._tts = NativeTTS()
        self._wake: WakeDetector | None = None
        self._legacy: LegacySpeechBackend | None = None
        self._mode = "idle"  # idle | wake | command
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()
        self._command_stop = threading.Event()
        self._command_thread: threading.Thread | None = None
        self._disabled = False
        self._disable_reason: str | None = None

    def _legacy_backend(self) -> LegacySpeechBackend:
        if self._legacy is None:
            self._legacy = LegacySpeechBackend()
        return self._legacy

    def _queue(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append({"event": event, "payload": payload, "ts": time.time()})

    def poll_events(self) -> list[dict[str, Any]]:
        if self._should_use_legacy():
            return self._legacy_backend().poll_events()
        with self._lock:
            if not self._events:
                return []
            out = self._events[:]
            self._events.clear()
            return out

    def _should_use_legacy(self) -> bool:
        return _use_legacy() or self._disabled

    def voice_backend(self) -> str:
        if self._should_use_legacy():
            return self._legacy_backend().voice_backend()
        return self.BACKEND_NAME

    def mic_available(self) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().mic_available()
        mic = self._mic.mic_available()
        try:
            WhisperSTT.get().ensure_loaded()
            mic["whisper"] = "loaded"
        except Exception as e:
            mic["whisper"] = f"error:{e}"
        try:
            if self._wake is None:
                self._wake = WakeDetector()
            self._wake.ensure_loaded()
            mic["wake"] = "loaded"
        except Exception as e:
            mic["wake"] = f"error:{e}"
        mic["backend"] = self.BACKEND_NAME
        return mic

    def speak_response(self, text: str) -> dict[str, Any]:
        if self._should_use_legacy():
            return {"ok": False, "error": "native_tts_unavailable_in_legacy_mode"}
        return self._tts.speak(text)

    def warm_up(self) -> dict[str, Any]:
        """Lazy-load models (first mic use)."""
        if self._should_use_legacy():
            return {"ok": True, "backend": "legacy"}
        t0 = time.perf_counter()
        try:
            WhisperSTT.get().ensure_loaded()
            if self._wake is None:
                self._wake = WakeDetector()
            self._wake.ensure_loaded()
            elapsed = time.perf_counter() - t0
            vlog("voice_warm_up_ok", seconds=round(elapsed, 2))
            return {"ok": True, "backend": self.BACKEND_NAME, "warm_up_s": round(elapsed, 2)}
        except Exception as e:
            vlog("voice_warm_up_failed", error=str(e))
            self._disabled = True
            self._disable_reason = str(e)
            return {"ok": False, "error": str(e), "fallback": "legacy"}

    def start_voice_listen(self, silence_ms: int = 1800, hold_ms: int = 25000) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().start_voice_listen(silence_ms, hold_ms)

        warm = self.warm_up()
        if not warm.get("ok"):
            vlog("modern_listen_fallback", reason=warm.get("error"))
            return self._legacy_backend().start_voice_listen(silence_ms, hold_ms)

        self.stop_wake_listen()
        with self._lock:
            if self._mode == "command" and self._command_thread and self._command_thread.is_alive():
                return {"ok": True, "already": True}
            self._command_stop.clear()
            self._mode = "command"

        max_sec = max(8.0, hold_ms / 1000.0)
        silence = max(700, int(silence_ms))

        def run_command() -> None:
            self._queue("started", {"backend": self.BACKEND_NAME, "mode": "command"})
            try:
                pcm = self._mic.record_until_silence(
                    max_seconds=max_sec,
                    silence_ms=silence,
                    stop_check=self._command_stop.is_set,
                )
                if self._command_stop.is_set():
                    self._queue("ended", {"transcript": "", "mode": "command"})
                    return
                result = WhisperSTT.get().transcribe_pcm(pcm)
                text = result.get("text") or ""
                if text:
                    self._queue("partial", {"text": text})
                    self._queue("final", {"text": text})
                    intent = match_intent(text)
                    payload = {
                        "transcript": text,
                        "mode": "command",
                        "confidence": result.get("confidence"),
                        "latency_ms": result.get("latency_ms"),
                    }
                    if intent:
                        payload["intent"] = intent.intent.name
                        payload["intent_path"] = intent.intent.path
                        payload["intent_score"] = intent.score
                    self._queue("ended", payload)
                else:
                    self._queue("ended", {"transcript": "", "mode": "command"})
            except Exception as e:
                vlog("command_listen_error", error=str(e))
                self._queue("error", {"message": str(e)})
                self._queue("ended", {"transcript": "", "mode": "command"})
            finally:
                with self._lock:
                    self._mode = "idle"

        self._command_thread = threading.Thread(target=run_command, daemon=True)
        self._command_thread.start()
        return {"ok": True, "backend": self.BACKEND_NAME, "mode": "command"}

    def start_wake_listen(self) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().start_wake_listen()

        warm = self.warm_up()
        if not warm.get("ok"):
            return self._legacy_backend().start_wake_listen()

        self.cancel_voice_listen()
        if self._wake is None:
            self._wake = WakeDetector()

        if self._wake_thread and self._wake_thread.is_alive():
            return {"ok": True, "already": True}

        self._wake_stop.clear()
        with self._lock:
            self._mode = "wake"

        def on_wake(score: float) -> None:
            self._queue("wake", {"score": score, "backend": self.BACKEND_NAME})
            self.stop_wake_listen()

        def run_wake() -> None:
            self._queue("started", {"backend": self.BACKEND_NAME, "mode": "wake"})
            try:
                self._wake.run_loop(
                    on_wake=on_wake,
                    stop_check=self._wake_stop.is_set,
                    stream_frames=self._mic.stream_frames,
                )
            except Exception as e:
                vlog("wake_loop_error", error=str(e))
                self._queue("error", {"message": str(e)})
            finally:
                self._queue("ended", {"transcript": "", "mode": "wake"})
                with self._lock:
                    if self._mode == "wake":
                        self._mode = "idle"

        self._wake_thread = threading.Thread(target=run_wake, daemon=True)
        self._wake_thread.start()
        return {"ok": True, "backend": self.BACKEND_NAME, "mode": "wake"}

    def stop_wake_listen(self) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().stop_wake_listen()
        self._wake_stop.set()
        with self._lock:
            if self._mode == "wake":
                self._mode = "idle"
        return {"ok": True}

    def stop_voice_listen(self) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().stop_voice_listen()
        self._command_stop.set()
        return {"ok": True, "transcript": ""}

    def cancel_voice_listen(self) -> dict[str, Any]:
        if self._should_use_legacy():
            return self._legacy_backend().cancel_voice_listen()
        self._command_stop.set()
        self.stop_wake_listen()
        return {"ok": True}

    def dispose(self) -> None:
        self.cancel_voice_listen()
        self._tts.dispose()
