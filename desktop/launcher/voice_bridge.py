"""
Desktop local speech recognition for IRA click-to-talk.

WebView2 Web Speech API is unreliable (Listening… with no transcripts).
This bridge uses Windows System.Speech and delivers events via a poll queue
(safe across threads — evaluate_js from recognition callbacks is unreliable).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("aica.voice")

WAKE_RE = re.compile(
    r"\b(hey|hay|hi|he)\s*[,.\-]?\s*(ira|aira|era|ara)\b|\bheira\b|\bhaira\b",
    re.I,
)

_SPEECH_DLL_CANDIDATES = (
    r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\WPF\System.Speech.dll",
    r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\WPF\System.Speech.dll",
    r"C:\Program Files\Reference Assemblies\Microsoft\Framework\v3.0\System.Speech.dll",
)


def _voice_log_path():
    try:
        from backend.runtime_paths import logs_dir

        return logs_dir() / "voice.log"
    except Exception:
        base = os.environ.get("APPDATA") or "."
        d = os.path.join(base, "AICA", "logs")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "voice.log")


def _vlog(msg: str, **extra: Any) -> None:
    line = msg
    if extra:
        try:
            line = f"{msg} {json.dumps(extra, default=str)}"
        except Exception:
            line = f"{msg} {extra}"
    logger.info(line)
    try:
        path = _voice_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def _load_system_speech():
    import clr  # type: ignore

    last_err: Exception | None = None
    for path in _SPEECH_DLL_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            clr.AddReference(path)
            from System.Speech.Recognition import (  # type: ignore  # noqa: F401
                DictationGrammar,
                RecognizeMode,
                SpeechRecognitionEngine,
            )
            from System import TimeSpan  # type: ignore  # noqa: F401

            return SpeechRecognitionEngine, DictationGrammar, RecognizeMode, TimeSpan
        except Exception as e:
            last_err = e
            _vlog("Failed loading System.Speech", path=path, error=str(e))
    raise RuntimeError(
        "Windows System.Speech is not available. "
        f"Last error: {last_err}"
    )


class DesktopVoiceBridge:
    """pywebview js_api object — methods are called from JavaScript."""

    def __init__(self) -> None:
        self._window = None
        self._engine = None
        self._lock = threading.Lock()
        self._listening = False
        self._buffer: list[str] = []
        self._events: list[dict[str, Any]] = []
        self._silence_timer: threading.Timer | None = None
        self._hold_timer: threading.Timer | None = None
        self._silence_ms = 1800
        self._hold_ms = 25000
        self._mode = "command"  # command | wake
        self._SpeechRecognitionEngine = None
        self._DictationGrammar = None
        self._RecognizeMode = None
        self._TimeSpan = None

    def attach_window(self, window) -> None:
        self._window = window

    def voice_backend(self) -> str:
        return "windows-system-speech"

    def poll_voice_events(self) -> list[dict[str, Any]]:
        """JS polls this while listening — thread-safe event delivery."""
        with self._lock:
            if not self._events:
                return []
            out = self._events[:]
            self._events.clear()
            return out

    def mic_available(self) -> dict[str, Any]:
        try:
            SpeechRecognitionEngine, *_rest = _load_system_speech()
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
            _vlog("mic_available", **info)
            return info
        except Exception as e:
            info = {"ok": False, "device": "NOT_AVAILABLE", "error": str(e)}
            _vlog("mic_available_failed", **info)
            return info

    def start_voice_listen(self, silence_ms: int = 1800, hold_ms: int = 25000) -> dict[str, Any]:
        with self._lock:
            self._silence_ms = int(silence_ms) if silence_ms else 1800
            self._hold_ms = int(hold_ms) if hold_ms else 25000
            if self._listening:
                if self._mode == "command":
                    return {"ok": True, "already": True}
                # Switch from wake → command
                self._stop_unlocked(commit=False)
            try:
                result = self._start_unlocked(mode="command")
                _vlog("start_voice_listen", **result)
                return result
            except Exception as e:
                _vlog("start_voice_listen_failed", error=str(e))
                self._queue_event("error", {"message": str(e)})
                return {"ok": False, "error": str(e)}

    def start_wake_listen(self) -> dict[str, Any]:
        """Continuous local wake-word listen for 'Hey Ira' (no Gemini)."""
        with self._lock:
            if self._listening and self._mode == "wake":
                return {"ok": True, "already": True}
            if self._listening:
                self._stop_unlocked(commit=False)
            try:
                result = self._start_unlocked(mode="wake")
                _vlog("start_wake_listen", **result)
                return result
            except Exception as e:
                _vlog("start_wake_listen_failed", error=str(e))
                self._queue_event("error", {"message": str(e)})
                return {"ok": False, "error": str(e)}

    def stop_wake_listen(self) -> dict[str, Any]:
        with self._lock:
            if self._listening and self._mode == "wake":
                self._stop_unlocked(commit=False)
        return {"ok": True}

    def stop_voice_listen(self) -> dict[str, Any]:
        with self._lock:
            text = self._stop_unlocked(commit=True)
        _vlog("stop_voice_listen", transcript=text)
        return {"ok": True, "transcript": text}

    def cancel_voice_listen(self) -> dict[str, Any]:
        with self._lock:
            self._stop_unlocked(commit=False)
        _vlog("cancel_voice_listen")
        return {"ok": True}

    def _queue_event(self, event: str, payload: dict[str, Any]) -> None:
        self._events.append({"event": event, "payload": payload, "ts": time.time()})

    def _start_unlocked(self, mode: str = "command") -> dict[str, Any]:
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
            # Stay open for long ambient listening; ignore short silence cutoffs
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

        self._queue_event("started", {"backend": "windows-system-speech", "mode": mode})
        if mode == "command":
            self._arm_hold_timer()
        return {"ok": True, "backend": "windows-system-speech", "mode": mode}

    def _stop_unlocked(self, commit: bool) -> str:
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
                self._queue_event("final", {"text": text})
            self._queue_event(
                "ended",
                {"transcript": text if commit else "", "mode": mode},
            )
        return text if commit else ""

    def _arm_silence_timer(self) -> None:
        if self._silence_timer:
            self._silence_timer.cancel()
        self._silence_timer = threading.Timer(
            self._silence_ms / 1000.0, self._silence_fire
        )
        self._silence_timer.daemon = True
        self._silence_timer.start()

    def _arm_hold_timer(self) -> None:
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
            if not self._listening:
                return
            _vlog("silence_commit", buffer=list(self._buffer))
            self._stop_unlocked(commit=True)

    def _hold_fire(self) -> None:
        with self._lock:
            if not self._listening:
                return
            _vlog("hold_commit", buffer=list(self._buffer))
            self._stop_unlocked(commit=True)

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
                _vlog("wake_heard", text=text)
                if WAKE_RE.search(text):
                    _vlog("wake_matched", text=text)
                    self._queue_event("wake", {"text": text})
                    self._stop_unlocked(commit=False)
                return
            self._buffer.append(text)
            joined = " ".join(self._buffer).strip()
            _vlog("recognized", text=text, joined=joined)
            self._queue_event("partial", {"text": joined, "final_chunk": text})
            self._arm_silence_timer()

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
                    # Early match on hypothesis — wait for final SpeechRecognized
                    self._queue_event("hypothesis", {"text": text, "mode": "wake"})
                return
            preview = (" ".join(self._buffer) + " " + text).strip()
            self._queue_event("hypothesis", {"text": preview})

    def _on_completed(self, sender, event) -> None:
        try:
            err = event.Error
        except Exception:
            err = None
        with self._lock:
            if not self._listening:
                return
            if err is not None:
                _vlog("recognize_completed_error", error=str(err), mode=self._mode)
                self._queue_event("error", {"message": str(err)})
                return
            if self._mode == "wake" and self._engine is not None:
                # Long silence ends the async session — keep wake listening alive
                try:
                    self._engine.RecognizeAsync(self._RecognizeMode.Multiple)
                    _vlog("wake_restart_after_silence")
                except Exception as e:
                    _vlog("wake_restart_failed", error=str(e))
                    self._queue_event("ended", {"transcript": "", "mode": "wake"})
                    self._listening = False
                    self._engine = None


_bridge: Optional[DesktopVoiceBridge] = None


def get_voice_bridge() -> DesktopVoiceBridge:
    global _bridge
    if _bridge is None:
        _bridge = DesktopVoiceBridge()
    return _bridge
