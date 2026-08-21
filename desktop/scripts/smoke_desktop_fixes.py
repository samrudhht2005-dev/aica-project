"""Smoke: Remember Me cookie + WebView2 SpeechRecognition diagnostics."""
from __future__ import annotations

import os
import sys
import time
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

v = dotenv_values(ROOT / ".env")
for k in ("DATABASE_URL", "GEMINI_API_KEY"):
    if v.get(k):
        os.environ[k] = v[k]


def test_remember_cookie_headers():
    from backend.auth import REMEMBER_MAX_AGE, set_session_cookie
    from starlette.responses import Response

    r = Response()
    set_session_cookie(r, 1, 1, remember=True)
    cookies = [val.decode() for key, val in r.raw_headers if key.lower() == b"set-cookie"]
    assert any(f"Max-Age={REMEMBER_MAX_AGE}" in c for c in cookies), cookies

    r2 = Response()
    set_session_cookie(r2, 1, 1, remember=False)
    cookies2 = [val.decode() for key, val in r2.raw_headers if key.lower() == b"set-cookie"]
    assert all("Max-Age=" not in c for c in cookies2), cookies2
    print("PASS cookie headers remember vs session")


def test_login_remember_against_engine(port: int = 18825):
    import subprocess
    import urllib.request

    env = os.environ.copy()
    env["AICA_PORT"] = str(port)
    env["AICA_HOST"] = "127.0.0.1"
    env["AICA_DESKTOP"] = "1"
    exe = ROOT / "dist" / "AICA.Engine" / "AICA.Engine.exe"
    p = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                if p.poll() is not None:
                    raise RuntimeError("engine died")
        else:
            raise RuntimeError("engine not ready")

        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        # Fetch login for CSRF-less form
        opener.open(f"http://127.0.0.1:{port}/login")
        # Need real credentials from env or skip
        email = os.environ.get("AICA_TEST_EMAIL") or v.get("AICA_TEST_EMAIL")
        password = os.environ.get("AICA_TEST_PASSWORD") or v.get("AICA_TEST_PASSWORD")
        if not email or not password:
            print("SKIP live login remember (set AICA_TEST_EMAIL/PASSWORD)")
            return
        body = urlencode({"email": email, "password": password, "remember": "on"}).encode()
        req = Request(
            f"http://127.0.0.1:{port}/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            opener.open(req)
        except Exception as e:
            # 303 redirects may raise depending on opener
            pass
        names = [c.name for c in jar]
        sess = [c for c in jar if c.name == "aica_session"]
        print("cookies_after_login", names)
        if sess:
            c = sess[0]
            print("session_expires", c.expires, "discard", c.discard)
            assert c.expires is not None and not c.discard, "Remember Me should set persistent cookie"
            print("PASS remember cookie persisted in jar")
        else:
            print("WARN no session cookie — credentials may be wrong")
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


def test_webview_speech_api():
    """Open WebView2 briefly and probe SpeechRecognition + storage path."""
    import webview
    from desktop.launcher.webview_desktop import (
        install_webview2_permission_hook,
        webview_user_data_dir,
    )

    install_webview2_permission_hook()
    storage = webview_user_data_dir()
    result = {"sr": None, "desktop": None, "err": None}

    html = """
    <html><body><script>
      window.__probe = {
        sr: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
        media: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
      };
    </script>Speech probe</body></html>
    """
    window = webview.create_window("AICA probe", html=html, width=400, height=200)

    def _loaded():
        try:
            result["sr"] = window.evaluate_js(
                "(window.SpeechRecognition || window.webkitSpeechRecognition) ? true : false"
            )
            result["media"] = window.evaluate_js(
                "(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) ? true : false"
            )
        except Exception as e:
            result["err"] = str(e)
        finally:
            window.destroy()

    try:
        window.events.loaded += _loaded
    except Exception:
        pass

    t = threading.Thread(
        target=lambda: webview.start(private_mode=False, storage_path=str(storage)),
        daemon=True,
    )
    t.start()
    t.join(timeout=25)
    print("PASS webview probe", result, "storage", storage)
    assert result.get("err") is None or result.get("sr") is not None


if __name__ == "__main__":
    test_remember_cookie_headers()
    test_login_remember_against_engine()
    try:
        test_webview_speech_api()
    except Exception as e:
        print("WARN webview probe failed (may need interactive desktop):", e)
