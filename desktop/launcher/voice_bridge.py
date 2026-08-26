"""
Desktop local speech for IRA — pywebview js_api.

Delegates to ModernVoiceEngine (faster-whisper + openWakeWord + native TTS).
Legacy System.Speech remains available via AICA_VOICE_BACKEND=legacy.
"""
from __future__ import annotations

from typing import Any, Optional

from desktop.launcher.voice_engine import ModernVoiceEngine

_engine: Optional[DesktopVoiceBridge] = None


def get_voice_bridge() -> DesktopVoiceBridge:
    global _engine
    if _engine is None:
        _engine = DesktopVoiceBridge()
    return _engine


class DesktopVoiceBridge:
    """pywebview js_api — method names preserved for assistant.js compatibility."""

    def __init__(self) -> None:
        self._window = None
        self._core = ModernVoiceEngine()

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

    def speak_response_async(self, text: str) -> dict[str, Any]:
        return self._core.speak_response_async(text)

    def cancel_speak(self) -> dict[str, Any]:
        return self._core.cancel_speak()

    def start_voice_listen(
        self,
        silence_ms: int = 1400,
        hold_ms: int = 20000,
        ui_mode: str = "org",
    ) -> dict[str, Any]:
        return self._core.start_voice_listen(silence_ms, hold_ms, ui_mode)

    def start_wake_listen(self) -> dict[str, Any]:
        return self._core.start_wake_listen()

    def stop_wake_listen(self) -> dict[str, Any]:
        return self._core.stop_wake_listen()

    def stop_voice_listen(self) -> dict[str, Any]:
        return self._core.stop_voice_listen()

    def cancel_voice_listen(self) -> dict[str, Any]:
        return self._core.cancel_voice_listen()

    def get_update_status(self) -> dict[str, Any]:
        """Read-only update state for desktop UI (no network)."""
        try:
            from desktop.launcher.update_checker import get_update_status_dict

            return get_update_status_dict()
        except Exception as e:
            return {
                "status": "error",
                "checking": False,
                "update_available": False,
                "installed_version": "",
                "available_version": None,
                "mandatory": False,
                "release_notes": "",
                "published_at": None,
                "last_check_at": None,
                "from_cache": False,
                "error": str(e),
            }

    def check_for_updates_now(self) -> dict[str, Any]:
        """Trigger a fresh background manifest check (Profile manual action)."""
        try:
            from desktop.launcher.update_checker import (
                force_refresh_update_check,
                get_update_status_dict,
            )

            scheduled = force_refresh_update_check(delay_s=0.0)
            status = get_update_status_dict()
            return {
                "ok": scheduled,
                "checking": True if scheduled else status.get("checking", False),
                "already_in_progress": not scheduled,
                "status": status,
            }
        except Exception as e:
            return {
                "ok": False,
                "checking": False,
                "already_in_progress": False,
                "error": str(e),
            }

    def start_update_download(self) -> dict[str, Any]:
        """Download and verify the available update installer (Phase 4)."""
        try:
            from desktop.launcher.update_download import start_update_download

            return start_update_download()
        except Exception as e:
            return {
                "ok": False,
                "status": "error",
                "error": "Download failed. Please try again later.",
                "active": False,
            }

    def get_update_download_status(self) -> dict[str, Any]:
        """Poll installer download progress (no network)."""
        try:
            from desktop.launcher.update_download import get_update_download_status_dict

            return get_update_download_status_dict()
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "active": False,
            }

    def apply_staged_update(self) -> dict[str, Any]:
        """Launch AICA.Updater.exe for a verified staged installer, then shut down."""
        try:
            from desktop.launcher.update_apply import apply_staged_update

            return apply_staged_update()
        except Exception:
            return {
                "ok": False,
                "status": "error",
                "error": "Unable to start the update. AICA will stay open.",
                "updater_started": False,
                "already_in_progress": False,
            }

    def get_update_apply_status(self) -> dict[str, Any]:
        """Poll update-apply handoff status (no filesystem paths)."""
        try:
            from desktop.launcher.update_apply import get_apply_status_dict

            return get_apply_status_dict()
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "active": False,
                "updater_started": False,
            }

    def save_user_download(self, filename: str, content_b64: str) -> dict[str, Any]:
        """
        Save a generated PDF/file from the WebView (invoice, weigh label, etc.).
        Called from frontend when AICA_DESKTOP is set — avoids broken blob downloads.
        """
        try:
            from desktop.launcher.user_downloads import decode_download_payload, save_bytes_with_dialog

            data = decode_download_payload(content_b64)
            return save_bytes_with_dialog(self._window, filename, data)
        except Exception as e:
            return {
                "ok": False,
                "cancelled": False,
                "error": str(e) or "Could not save file",
            }
