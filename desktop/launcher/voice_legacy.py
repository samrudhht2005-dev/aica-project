"""Legacy System.Speech DictationGrammar backend (fallback)."""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from desktop.launcher.voice_log import vlog

WAKE_RE = re.compile(
    r"\b(hey|hay|hi|he)\s*[,.\-]?\s*(ira|aira|era|ara)\b|\bheira\b|\bhaira\b",
    re.I,
)

_SPEECH_DLL_CANDIDATES = (
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\WPF\System.Speech.dll",
    r"C:\Program Files\Reference Assemblies\Microsoft\Framework\v3.0\System.Speech.dll",
)


def _load_system_speech():
    import clr  # type: ignore

    last_err: Exception | None = None
    for path in _SPEECH_DLL_CANDIDATES:
        if not __import__("os").path.isfile(path):
            continue
        try:
            clr.AddReference(path)
            from System.Speech.Recognition import (  # type: ignore
                DictationGrammar,
                RecognizeMode,
                SpeechRecognitionEngine,
            )
            from System import TimeSpan  # type: ignore

            return SpeechRecognitionEngine, DictationGrammar, RecognizeMode, TimeSpan
        except Exception as e:
            last_err = e
            vlog("legacy_speech_load_failed", path=path, error=str(e))
    raise RuntimeError(f"Windows System.Speech unavailable: {last_err}")


class LegacySpeechBackend:
    """Original AICA 1.0.2 System.Speech implementation."""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self._listening = False
        self._buffer: list[str] = []
        self._events: list[dict[str, Any]] = []
        self._silence_timer: threading.Timer | None = None
        self._hold_timer: threading.Timer | None = None
        self._silence_ms = 1800
        self._hold_ms = 25000
        self._mode = "command"
        self._SpeechRecognitionEngine = None
        self._DictationGrammar = None
        self._RecognizeMode = None
        self._TimeSpan = None

    def voice_backend(self) -> str:
        return "windows-system-speech-legacy"

    def mic_available(self) -> dict[str, Any]:
        try:
            SpeechRecognitionEngine, *_ = _load_system_speech()
            eng = SpeechRecognitionEngine()
            info = {
                "ok": True,
                "device": "AVAILABLE",
                "recognizer": str(eng.RecognizerInfo.Description),
                "culture": str(eng.RecognizerInfo.Culture),
            }
            try:
                eng.SetInputToDefaultAudioDevice()
                info["input"] = "default_audio_device"
            except Exception as e:
                info["ok"] = False
                info["device"] = "NOT_AVAILABLE"
                info["error"] = str(e)
            eng.Dispose()
            return info
        except Exception as e:
            return {"ok": False, "device": "NOT_AVAILABLE", "error": str(e)}

    def poll_events(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._events:
                return []
            out = self._events[:]
            self._events.clear()
            return out

    def _queue(self, event: str, payload: dict[str, Any]) -> None:
        self._events.append({"event": event, "payload": payload, "ts": time.time()})

    def start_voice_listen(self, silence_ms: int = 1800, hold_ms: int = 25000) -> dict[str, Any]:
        with self._lock:
            self._silence_ms = int(silence_ms) if silence_ms else 1800
            self._hold_ms = int(hold_ms) if hold_ms else 25000
            if self._listening:
                if self._mode == "command":
                    return {"ok": True, "already": True}
                self._stop(commit=False)
            try:
                return self._start(mode="command")
            except Exception as e:
                self._queue("error", {"message": str(e)})
                return {"ok": False, "error": str(e)}

    def start_wake_listen(self) -> dict[str, Any]:
        with self._lock:
            if self._listening and self._mode == "wake":
                return {"ok": True, "already": True}
            if self._listening:
                self._stop(commit=False)
            try:
                return self._start(mode="wake")
            except Exception as e:
                self._queue("error", {"message": str(e)})
                return {"ok": False, "error": str(e)}

    def stop_wake_listen(self) -> dict[str, Any]:
        with self._lock:
            if self._listening and self._mode == "wake":
                self._stop(commit=False)
        return {"ok": True}

    def stop_voice_listen(self) -> dict[str, Any]:
        with self._lock:
            text = self._stop(commit=True)
        return {"ok": True, "transcript": text}

    def cancel_voice_listen(self) -> dict[str, Any]:
        with self._lock:
            self._stop(commit=False)
        return {"ok": True}

    def _start(self, mode: str) -> dict[str, Any]:
        (
            self._SpeechRecognitionEngine,
            self._DictationGrammar,
            self._RecognizeMode,
            self._TimeSpan,
        ) = _load_system_speech()
        eng = self._SpeechRecognitionEngine()
        eng.SetInputToDefaultAudioDevice()
        eng.LoadGrammar(self._DictationGrammar())
        if mode == "wake":
            eng.InitialSilenceTimeout = self._TimeSpan.FromSeconds(30)
            eng.BabbleTimeout = self._TimeSpan.FromSeconds(5)
            eng.EndSilenceTimeout = self._TimeSpan.FromMilliseconds(600)
        else:
            eng.InitialSilenceTimeout = self._TimeSpan.FromSeconds(12)
            eng.BabbleTimeout = self._TimeSpan.FromSeconds(4)
            eng.EndSilenceTimeout = self._TimeSpan.FromMilliseconds(1100)
        eng.SpeechRecognized += self._on_recognized
        eng.SpeechHypothesized += self._on_hypothesized
        eng.RecognizeCompleted += self._on_completed
        self._engine = eng
        self._buffer = []
        self._events = []
        self._mode = mode
        self._listening = True
        eng.RecognizeAsync(self._RecognizeMode.Multiple)
        self._queue("started", {"backend": self.voice_backend(), "mode": mode})
        if mode == "command":
            self._arm_hold()
        return {"ok": True, "backend": self.voice_backend(), "mode": mode}

    def _stop(self, commit: bool) -> str:
        self._cancel_timers()
        text = " ".join(self._buffer).strip()
        self._buffer = []
        was = self._listening
        mode = self._mode
        self._listening = False
        eng = self._engine
        self._engine = None
        if eng is not None:
            try:
                eng.RecognizeAsyncCancel()
            except Exception:
                pass
            try:
                eng.Dispose()
            except Exception:
                pass
        if was:
            if commit and text and mode == "command":
                self._queue("final", {"text": text})
            self._queue("ended", {"transcript": text if commit else "", "mode": mode})
        return text if commit else ""

    def _arm_silence(self) -> None:
        if self._silence_timer:
            self._silence_timer.cancel()
        self._silence_timer = threading.Timer(self._silence_ms / 1000.0, self._silence_fire)
        self._silence_timer.daemon = True
        self._silence_timer.start()

    def _arm_hold(self) -> None:
        if self._hold_timer:
            self._hold_timer.cancel()
        self._hold_timer = threading.Timer(self._hold_ms / 1000.0, self._hold_fire)
        self._hold_timer.daemon = True
        self._hold_timer.start()

    def _cancel_timers(self) -> None:
        for t in (self._silence_timer, self._hold_timer):
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
        self._silence_timer = None
        self._hold_timer = None

    def _silence_fire(self) -> None:
        with self._lock:
            if self._listening:
                self._stop(commit=True)

    def _hold_fire(self) -> None:
        with self._lock:
            if self._listening:
                self._stop(commit=True)

    def _on_recognized(self, sender, event) -> None:
        try:
            text = str(event.Result.Text or "").strip()
        except Exception:
            text = ""
        if not text:
            return
        with self._lock:
            if not self._listening:
                return
            if self._mode == "wake":
                if WAKE_RE.search(text):
                    self._queue("wake", {"text": text})
                    self._stop(commit=False)
                return
            self._buffer.append(text)
            joined = " ".join(self._buffer).strip()
            self._queue("partial", {"text": joined, "final_chunk": text})
            self._arm_silence()

    def _on_hypothesized(self, sender, event) -> None:
        try:
            text = str(event.Result.Text or "").strip()
        except Exception:
            text = ""
        if not text:
            return
        with self._lock:
            if not self._listening:
                return
            if self._mode == "wake":
                if WAKE_RE.search(text):
                    self._queue("hypothesis", {"text": text, "mode": "wake"})
                return
            preview = (" ".join(self._buffer) + " " + text).strip()
            self._queue("hypothesis", {"text": preview})

    def _on_completed(self, sender, event) -> None:
        with self._lock:
            if not self._listening:
                return
            if self._mode == "wake" and self._engine is not None:
                try:
                    self._engine.RecognizeAsync(self._RecognizeMode.Multiple)
                except Exception:
                    self._queue("ended", {"transcript": "", "mode": "wake"})
                    self._listening = False
                    self._engine = None
