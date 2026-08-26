"""
AICA voice engine v2 — faster-whisper + openWakeWord + native TTS.

Orchestrates mic capture, wake, STT, and events for pywebview.
Legacy System.Speech only when AICA_VOICE_BACKEND=legacy (explicit).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from desktop.launcher.voice_audio import MicCapture
from desktop.launcher.voice_diag import vdiag
from desktop.launcher.voice_intents import intent_ui_hints, match_intent
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
        self._init_error: str | None = None
        self._ui_mode: str = "org"

    def _legacy_backend(self) -> LegacySpeechBackend:
        if self._legacy is None:
            self._legacy = LegacySpeechBackend()
        return self._legacy

    def _queue(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append({"event": event, "payload": payload, "ts": time.time()})

    def _drain_events(self) -> None:
        """Drop stale events (e.g. wake 'ended') so they cannot abort a new command session."""
        with self._lock:
            self._events.clear()

    def poll_events(self) -> list[dict[str, Any]]:
        if _use_legacy():
            return self._legacy_backend().poll_events()
        with self._lock:
            if not self._events:
                return []
            out = self._events[:]
            self._events.clear()
            return out

    def voice_backend(self) -> str:
        if _use_legacy():
            return self._legacy_backend().voice_backend()
        return self.BACKEND_NAME

    def mic_available(self) -> dict[str, Any]:
        if _use_legacy():
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
        if self._init_error:
            mic["init_error"] = self._init_error
        return mic

    def speak_response(self, text: str) -> dict[str, Any]:
        if _use_legacy():
            return {"ok": False, "error": "native_tts_unavailable_in_legacy_mode"}
        return self._tts.speak(text)

    def speak_response_async(self, text: str) -> dict[str, Any]:
        """Fire-and-forget short ack (navigation). Does not block the UI thread."""
        if _use_legacy():
            return {"ok": False, "error": "native_tts_unavailable_in_legacy_mode"}
        from desktop.launcher.voice_diag import vdiag

        out = self._tts.speak_async(text)
        vdiag(
            "TTS_NAV_ACK",
            ok=bool(out.get("ok")),
            chars=len(str(text or "")),
            async_mode=True,
        )
        return out

    def cancel_speak(self) -> dict[str, Any]:
        if _use_legacy():
            return {"ok": True, "cancelled": False}
        from desktop.launcher.voice_diag import vdiag

        out = self._tts.cancel()
        vdiag("TTS_CANCEL", cancelled=bool(out.get("cancelled")))
        return out

    def warm_up(self) -> dict[str, Any]:
        """Lazy-load models (first mic use). Does not permanently disable modern voice."""
        if _use_legacy():
            return {"ok": True, "backend": "legacy"}
        t0 = time.perf_counter()
        try:
            WhisperSTT.get().ensure_loaded()
            if self._wake is None:
                self._wake = WakeDetector()
            self._wake.ensure_loaded()
            tts_info = self._tts.ensure_loaded()
            self._init_error = None
            elapsed = time.perf_counter() - t0
            vlog(
                "voice_warm_up_ok",
                seconds=round(elapsed, 2),
                tts_engine=tts_info.get("engine"),
                tts_ok=bool(tts_info.get("ok")),
            )
            return {
                "ok": True,
                "backend": self.BACKEND_NAME,
                "warm_up_s": round(elapsed, 2),
                "tts": tts_info,
            }
        except Exception as e:
            self._init_error = str(e)
            vlog("voice_warm_up_failed", error=str(e), fallback="none")
            return {"ok": False, "error": str(e), "backend": self.BACKEND_NAME}

    def _release_wake_mic(self, timeout_s: float = 2.0) -> None:
        """Stop ambient wake and wait until the mic lock is free for click-to-talk."""
        self._wake_stop.set()
        thr = self._wake_thread
        if thr and thr.is_alive() and thr is not threading.current_thread():
            thr.join(timeout=timeout_s)
        # Brief settle so sounddevice releases the device handle.
        time.sleep(0.08)
        with self._lock:
            if self._mode == "wake":
                self._mode = "idle"

    def _prepare_command_whisper(self, timeout_s: float = 45.0) -> None:
        """
        Pause new wake verifies, stop ambient mic, wait for in-flight wake Whisper
        and any other transcription to finish before command capture/STT.
        """
        vdiag("COMMAND_LISTEN_START")
        if self._wake is not None:
            self._wake.pause_verify()
        self._release_wake_mic()
        vdiag("COMMAND_LISTEN_WAITING")
        if self._wake is not None:
            ok = self._wake.wait_verify_idle(timeout=timeout_s)
            vdiag("COMMAND_LISTEN_WAKE_VERIFY_IDLE", ok=ok)
        idle = WhisperSTT.get().wait_until_idle(timeout=timeout_s)
        vdiag("COMMAND_LISTEN_WHISPER_IDLE", ok=idle)

    def start_voice_listen(
        self,
        silence_ms: int = 1400,
        hold_ms: int = 20000,
        ui_mode: str = "org",
    ) -> dict[str, Any]:
        if _use_legacy():
            return self._legacy_backend().start_voice_listen(silence_ms, hold_ms)

        mode = (ui_mode or "org").strip().lower()
        if mode not in ("pos", "org", "weigh"):
            mode = "org"
        self._ui_mode = mode
        vdiag("COMMAND_UI_MODE", ui_mode=self._ui_mode)

        warm = self.warm_up()
        if not warm.get("ok"):
            # Do NOT silently fall back to System.Speech for normal operation.
            vlog("modern_listen_blocked", reason=warm.get("error"))
            return {
                "ok": False,
                "error": warm.get("error") or "modern_voice_init_failed",
                "backend": self.BACKEND_NAME,
            }

        # Never run command Whisper concurrently with wake Whisper.
        self._prepare_command_whisper()
        # Drop wake teardown events so JS never treats them as an empty command result.
        self._drain_events()

        with self._lock:
            if self._mode == "command" and self._command_thread and self._command_thread.is_alive():
                return {"ok": True, "already": True}
            self._command_stop.clear()
            self._mode = "command"

        # Align with successful modern live benchmark capture defaults.
        max_sec = min(14.0, max(8.0, hold_ms / 1000.0))
        silence = max(900, min(1800, int(silence_ms or 1400)))
        pre_speech_ms = 6000
        listen_ui_mode = self._ui_mode

        def run_command() -> None:
            t_cmd0 = time.perf_counter()
            t_capture_ms = 0.0
            t_whisper_ms = 0.0
            t_intent_ms = 0.0
            self._queue(
                "started",
                {
                    "backend": self.BACKEND_NAME,
                    "mode": "command",
                    "silence_ms": silence,
                    "pre_speech_timeout_ms": pre_speech_ms,
                },
            )
            try:
                vdiag(
                    "COMMAND_CAPTURE_START",
                    silence_ms=silence,
                    max_seconds=max_sec,
                    pre_speech_timeout_ms=pre_speech_ms,
                )
                t_cap0 = time.perf_counter()
                pcm = self._mic.record_until_silence(
                    max_seconds=max_sec,
                    silence_ms=silence,
                    pre_roll_ms=300,
                    vad_aggressiveness=1,
                    min_utterance_ms=2800,
                    pre_speech_timeout_ms=pre_speech_ms,
                    stop_check=self._command_stop.is_set,
                )
                t_capture_ms = (time.perf_counter() - t_cap0) * 1000.0
                vdiag(
                    "COMMAND_CAPTURE_END",
                    pcm_bytes=len(pcm or b""),
                    duration_ms=round(len(pcm or b"") / 32.0, 1),
                    wall_ms=round(t_capture_ms, 1),
                )
                if self._command_stop.is_set():
                    self._queue(
                        "ended",
                        {
                            "transcript": "",
                            "mode": "command",
                            "backend": self.BACKEND_NAME,
                            "error": "cancelled",
                        },
                    )
                    return
                if not pcm:
                    vlog("command_empty_pcm")
                    self._queue(
                        "ended",
                        {
                            "transcript": "",
                            "mode": "command",
                            "backend": self.BACKEND_NAME,
                            "error": "no_speech",
                        },
                    )
                    return
                # Capture finished — UI should leave "Listening…" immediately.
                self._queue(
                    "processing",
                    {
                        "mode": "command",
                        "backend": self.BACKEND_NAME,
                        "pcm_bytes": len(pcm),
                    },
                )
                vdiag("COMMAND_TRANSCRIBE_START", pcm_bytes=len(pcm))
                t_w0 = time.perf_counter()
                result = WhisperSTT.get().transcribe_pcm(pcm, context="command")
                t_whisper_ms = (time.perf_counter() - t_w0) * 1000.0
                vdiag(
                    "COMMAND_TRANSCRIBE_RETURN",
                    text=result.get("text") or "",
                    latency_ms=result.get("latency_ms"),
                    wall_ms=round(t_whisper_ms, 1),
                )
                text = result.get("text") or ""
                if text:
                    self._queue("partial", {"text": text})
                    self._queue("final", {"text": text})
                    t_i0 = time.perf_counter()
                    intent = match_intent(text, ui_mode=listen_ui_mode)
                    t_intent_ms = (time.perf_counter() - t_i0) * 1000.0
                    payload = {
                        "transcript": text,
                        "mode": "command",
                        "backend": self.BACKEND_NAME,
                        "confidence": result.get("confidence"),
                        "latency_ms": result.get("latency_ms"),
                        "pcm_bytes": len(pcm),
                    }
                    if intent:
                        payload["intent"] = intent.intent.name
                        payload["intent_path"] = intent.intent.path
                        payload["intent_speak"] = intent.intent.speak
                        payload["intent_score"] = intent.score
                        ui = intent_ui_hints(intent.intent.name)
                        if ui:
                            payload["intent_ui"] = ui
                    total_ms = (time.perf_counter() - t_cmd0) * 1000.0
                    vdiag(
                        "COMMAND_LATENCY_BREAKDOWN",
                        total_ms=round(total_ms, 1),
                        capture_ms=round(t_capture_ms, 1),
                        whisper_ms=round(t_whisper_ms, 1),
                        intent_ms=round(t_intent_ms, 1),
                        other_ms=round(
                            total_ms - t_capture_ms - t_whisper_ms - t_intent_ms, 1
                        ),
                        text=text,
                        intent=(intent.intent.name if intent else None),
                    )
                    self._queue("ended", payload)
                else:
                    total_ms = (time.perf_counter() - t_cmd0) * 1000.0
                    vdiag(
                        "COMMAND_LATENCY_BREAKDOWN",
                        total_ms=round(total_ms, 1),
                        capture_ms=round(t_capture_ms, 1),
                        whisper_ms=round(t_whisper_ms, 1),
                        intent_ms=0.0,
                        other_ms=round(total_ms - t_capture_ms - t_whisper_ms, 1),
                        text="",
                        intent=None,
                    )
                    self._queue(
                        "ended",
                        {
                            "transcript": "",
                            "mode": "command",
                            "backend": self.BACKEND_NAME,
                            "pcm_bytes": len(pcm),
                            "error": "unrecognized",
                        },
                    )
            except Exception as e:
                vlog("command_listen_error", error=str(e))
                self._queue("error", {"message": str(e), "backend": self.BACKEND_NAME})
                self._queue(
                    "ended",
                    {
                        "transcript": "",
                        "mode": "command",
                        "backend": self.BACKEND_NAME,
                        "error": "listen_error",
                    },
                )
            finally:
                with self._lock:
                    self._mode = "idle"

        self._command_thread = threading.Thread(target=run_command, daemon=True)
        self._command_thread.start()
        return {
            "ok": True,
            "backend": self.BACKEND_NAME,
            "mode": "command",
            "pre_speech_timeout_ms": pre_speech_ms,
            "silence_ms": silence,
        }

    def start_wake_listen(self) -> dict[str, Any]:
        if _use_legacy():
            return self._legacy_backend().start_wake_listen()

        warm = self.warm_up()
        if not warm.get("ok"):
            vlog("modern_wake_blocked", reason=warm.get("error"))
            return {
                "ok": False,
                "error": warm.get("error") or "modern_voice_init_failed",
                "backend": self.BACKEND_NAME,
            }

        # Brief wait so a finishing nav/chat ack does not bleed into ambient wake
        # (no long dead zone — max ~2.5s).
        try:
            self._tts.wait_idle(timeout=2.5)
        except Exception:
            pass

        self._command_stop.set()
        if self._command_thread and self._command_thread.is_alive():
            self._command_thread.join(timeout=1.5)

        if self._wake is None:
            self._wake = WakeDetector()

        if self._wake_thread and self._wake_thread.is_alive():
            return {"ok": True, "already": True, "backend": self.BACKEND_NAME}

        self._wake_stop.clear()
        self._wake.resume_verify()
        self._drain_events()
        with self._lock:
            self._mode = "wake"

        def on_wake(score: float) -> None:
            self._queue("wake", {"score": score, "backend": self.BACKEND_NAME})
            self.stop_wake_listen()

        def run_wake() -> None:
            self._queue("started", {"backend": self.BACKEND_NAME, "mode": "wake"})
            vdiag("WAKE_DETECTION", phase="loop_start")
            try:
                self._wake.run_loop(
                    on_wake=on_wake,
                    stop_check=self._wake_stop.is_set,
                    stream_frames=self._mic.stream_frames,
                )
            except Exception as e:
                vlog("wake_loop_error", error=str(e))
                self._queue("error", {"message": str(e), "backend": self.BACKEND_NAME})
            finally:
                # Allow in-flight verify to finish without enqueueing more.
                self._wake.pause_verify()
                self._queue("ended", {"transcript": "", "mode": "wake", "backend": self.BACKEND_NAME})
                with self._lock:
                    if self._mode == "wake":
                        self._mode = "idle"

        self._wake_thread = threading.Thread(target=run_wake, daemon=True)
        self._wake_thread.start()
        return {"ok": True, "backend": self.BACKEND_NAME, "mode": "wake"}

    def stop_wake_listen(self) -> dict[str, Any]:
        if _use_legacy():
            return self._legacy_backend().stop_wake_listen()
        if self._wake is not None:
            self._wake.pause_verify()
        self._wake_stop.set()
        with self._lock:
            if self._mode == "wake":
                self._mode = "idle"
        return {"ok": True, "backend": self.BACKEND_NAME}

    def stop_voice_listen(self) -> dict[str, Any]:
        if _use_legacy():
            return self._legacy_backend().stop_voice_listen()
        self._command_stop.set()
        return {"ok": True, "transcript": "", "backend": self.BACKEND_NAME}

    def cancel_voice_listen(self) -> dict[str, Any]:
        if _use_legacy():
            return self._legacy_backend().cancel_voice_listen()
        self._command_stop.set()
        self._release_wake_mic(timeout_s=1.5)
        return {"ok": True, "backend": self.BACKEND_NAME}

    def dispose(self) -> None:
        self.cancel_voice_listen()
        self._tts.dispose()
