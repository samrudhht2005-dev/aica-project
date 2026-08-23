"""
Desktop WebView2 helpers: persistent profile + allow microphone (IRA wake word).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path


def webview_user_data_dir() -> Path:
    override = os.environ.get("AICA_APPDATA")
    if override:
        path = Path(override) / "webview"
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / "AICA" / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_webview2_permission_hook() -> None:
    """
    Auto-allow Microphone (and Camera) permission prompts inside WebView2 so
    Web Speech API / getUserMedia can run for Hey IRA without a dead permission dialog.
    """
    try:
        from webview.platforms.edgechromium import EdgeChrome
    except Exception as e:
        logging.warning("WebView2 permission hook unavailable: %s", e)
        return

    if getattr(EdgeChrome, "_aica_perm_hooked", False):
        return

    orig = EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        orig(self, sender, args)
        try:
            from Microsoft.Web.WebView2.Core import (  # type: ignore
                CoreWebView2PermissionKind,
                CoreWebView2PermissionState,
            )

            def _on_permission(_s, e):
                try:
                    kind = e.PermissionKind
                    names = set()
                    for attr in ("Microphone", "Camera", "MicrophoneAndCamera"):
                        if hasattr(CoreWebView2PermissionKind, attr):
                            names.add(getattr(CoreWebView2PermissionKind, attr))
                    allow = kind in names or "Microphone" in str(kind) or "Camera" in str(kind)
                except Exception:
                    allow = "Microphone" in str(getattr(e, "PermissionKind", "")) or "Camera" in str(
                        getattr(e, "PermissionKind", "")
                    )
                if allow:
                    e.State = CoreWebView2PermissionState.Allow
                    try:
                        e.Handled = True
                    except Exception:
                        pass

            sender.CoreWebView2.PermissionRequested += _on_permission
            logging.info("AICA: WebView2 microphone/camera permission auto-allow installed")
        except Exception as ex:
            logging.warning("AICA: could not hook WebView2 PermissionRequested: %s", ex)

    EdgeChrome.on_webview_ready = on_webview_ready
    EdgeChrome._aica_perm_hooked = True


def desktop_bootstrap_js() -> str:
    """Injected after page load: mark desktop, probe mic, start Hey Ira wake listener."""
    import json

    try:
        from backend.runtime_paths import app_release_info

        version_js = json.dumps(str(app_release_info().get("version") or ""))
    except Exception:
        version_js = '""'

    return (
        """
    (function () {
      try { window.AICA_DESKTOP = true; } catch (e) {}
      try { window.AICA_VERSION = """
        + version_js
        + """; } catch (e) {}
      try { window.AICA_VOICE_BACKEND = 'pending'; } catch (e) {}
      function unlockMic() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return Promise.resolve(false);
        return navigator.mediaDevices.getUserMedia({ audio: true, video: false })
          .then(function (stream) {
            try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
            return true;
          })
          .catch(function (err) {
            try { window.AICA_GUM_ERROR = (err && err.name) ? err.name : String(err); } catch (e) {}
            return false;
          });
      }
      function startWake() {
        try {
          localStorage.setItem('ira_ambient_enabled', '1');
          if (window.AICA_IRA && typeof window.AICA_IRA.startAmbient === 'function') {
            window.AICA_IRA.startAmbient();
          }
        } catch (e) {}
      }
      unlockMic().then(function (ok) {
        window.AICA_MIC_UNLOCKED = !!ok;
        try {
          if (window.pywebview && window.pywebview.api && window.pywebview.api.voice_backend) {
            window.pywebview.api.voice_backend().then(function (b) {
              window.AICA_VOICE_BACKEND = b;
            }).catch(function () {});
          }
        } catch (e) {}
        startWake();
        setTimeout(startWake, 1000);
      });
    })();
    """
    )
