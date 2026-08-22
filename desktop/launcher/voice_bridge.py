"""
Desktop local speech for IRA — pywebview js_api.

Delegates to ModernVoiceEngine (faster-whisper + openWakeWord + native TTS).
Legacy System.Speech remains available via AICA_VOICE_BACKEND=legacy.
"""
from __future__ import annotations

from typing import Any, Optional

from desktop.launcher.voice_engine import ModernVoiceEngine

_engine: Optional[ModernVoiceEngine] = None


def get_voice_bridge() -> ModernVoiceEngine:
    global _engine
    if _engine is None:
        _engine = ModernVoiceEngine()
    return _engine


class DesktopVoiceBridge:
    """pywebview js_api — method names preserved for assistant.js compatibility."""

    def __init__(self) -> None:
        self._window = None
        self._core = get_voice_bridge()

    def attach_window(self, window) -> None:
        self._window = window

    def voice_backend(self) -> str:
        return self._core.voice_backend()

    def poll_voice_events(self) -> list[dict[str, Any]]:
        return self._core.poll_events()

    def mic_available(self) -> dict[str, Any]:
        return self._core.mic_available()

    def warm_up_voice(self) -> dict[str, Any]:
        return self._core.warm_up()

    def speak_response(self, text: str) -> dict[str, Any]:
        return self._core.speak_response(text)

    def start_voice_listen(self, silence_ms: int = 1800, hold_ms: int = 25000) -> dict[str, Any]:
        return self._core.start_voice_listen(silence_ms, hold_ms)

    def start_wake_listen(self) -> dict[str, Any]:
        return self._core.start_wake_listen()

    def stop_wake_listen(self) -> dict[str, Any]:
        return self._core.stop_wake_listen()

    def stop_voice_listen(self) -> dict[str, Any]:
        return self._core.stop_voice_listen()

    def cancel_voice_listen(self) -> dict[str, Any]:
        return self._core.cancel_voice_listen()
