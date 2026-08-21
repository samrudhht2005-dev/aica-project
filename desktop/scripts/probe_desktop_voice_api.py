"""Verify desktop voice bridge is exposed and transcripts reach IRA handlers."""
from __future__ import annotations

import json
import os
import sys
import time
import threading
import subprocess
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, HTTPError, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

v = dotenv_values(ROOT / ".env")
for k in ("DATABASE_URL", "GEMINI_API_KEY"):
    if v.get(k):
        os.environ[k] = v[k]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    from desktop.launcher.voice_bridge import get_voice_bridge

    mic = get_voice_bridge().mic_available()
    print("MICROPHONE_DEVICE:", mic.get("device"))
    print("MIC_INFO:", json.dumps(mic))
    if not mic.get("ok"):
        print("FAIL mic not available")
        return 1

    port = 18850
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
            time.sleep(0.2)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                if p.poll() is not None:
                    print("ENGINE_DIED")
                    return 1

        email = f"voice_{uuid.uuid4().hex[:8]}@aica.test"
        pw = "VerifyPass123!"
        jar = CookieJar()
        op = build_opener(HTTPCookieProcessor(jar), NoRedirect)

        def post(path, data):
            req = Request(
                f"http://127.0.0.1:{port}{path}",
                data=urlencode(data).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                return op.open(req).status
            except HTTPError as e:
                return e.code

        post(
            "/signup",
            {
                "org_name": "Voice Org",
                "business_type": "Retail",
                "gst_registered": "false",
                "gstin": "",
                "pan": "",
                "contact_number": "9999999999",
                "registered_address": "Test",
                "city": "Bengaluru",
                "state": "Karnataka",
                "pincode": "560001",
                "business_email": email,
                "full_name": "Voice",
                "email": email,
                "password": pw,
                "confirm_password": pw,
            },
        )
        post("/login", {"email": email, "password": pw, "remember": "true"})

        import webview
        from desktop.launcher.webview_desktop import (
            desktop_bootstrap_js,
            install_webview2_permission_hook,
            webview_user_data_dir,
        )

        install_webview2_permission_hook()
        voice = get_voice_bridge()
        storage = webview_user_data_dir()
        result = {}

        # Seed cookies into WebView by logging in via form (same as user)
        window = webview.create_window(
            "AICA Voice API Probe",
            url=f"http://127.0.0.1:{port}/login",
            width=1100,
            height=800,
            js_api=voice,
        )
        voice.attach_window(window)

        def work():
            time.sleep(1.2)
            try:
                window.evaluate_js(desktop_bootstrap_js())
                window.evaluate_js(
                    f"""
                    (function(){{
                      var e=document.querySelector('input[name=email],#email');
                      var p=document.querySelector('input[name=password],#password');
                      if(e) e.value={json.dumps(email)};
                      if(p) p.value={json.dumps(pw)};
                      var f=document.querySelector('form'); if(f) f.submit();
                    }})();
                    """
                )
                time.sleep(3.0)
                window.evaluate_js(desktop_bootstrap_js())
                window.evaluate_js(
                    """
                    (function(){
                      var orgBtn=[...document.querySelectorAll('button,a,input')].find(x=>/organisation|organization|org mode|continue/i.test((x.textContent||x.value||'')));
                      if(orgBtn) orgBtn.click();
                      var f=document.querySelector('form'); if(f && location.pathname.indexOf('select')>=0) f.submit();
                    })();
                    """
                )
                time.sleep(2.5)
                window.evaluate_js(desktop_bootstrap_js())
                time.sleep(1.0)

                result["diag"] = window.evaluate_js(
                    "window.AICA_IRA && window.AICA_IRA.diagnostics ? JSON.stringify(window.AICA_IRA.diagnostics()) : 'no-ira'"
                )
                result["api"] = window.evaluate_js(
                    "!!(window.pywebview && window.pywebview.api && window.pywebview.api.start_voice_listen)"
                )
                result["backend"] = window.evaluate_js(
                    "window.pywebview && window.pywebview.api ? window.pywebview.api.voice_backend() : null"
                )

                # Start listen via API, then inject a transcript as the bridge would
                window.evaluate_js(
                    """
                    (function(){
                      window.__VOICE_TEST = {started:false, got:null};
                      if (window.AICA_IRA) {
                        // open panel / enter listening UI like mic click
                        var btn = document.getElementById('aicaMicBtn');
                        if (btn) btn.click();
                      }
                    })();
                    """
                )
                time.sleep(1.0)
                result["after_mic_click"] = window.evaluate_js(
                    "window.AICA_IRA ? JSON.stringify(window.AICA_IRA.diagnostics()) : null"
                )

                # Simulate successful System.Speech result
                window.evaluate_js(
                    """
                    window.AICA_DESKTOP_VOICE && window.AICA_DESKTOP_VOICE('ended', {
                      transcript: 'What is my sales total today'
                    });
                    """
                )
                time.sleep(2.0)
                result["after_transcript"] = window.evaluate_js(
                    "window.AICA_IRA ? JSON.stringify(window.AICA_IRA.diagnostics()) : null"
                )
                result["chat_has_user"] = window.evaluate_js(
                    """
                    (function(){
                      var body = document.getElementById('aicaChatBody');
                      if(!body) return false;
                      return /sales total/i.test(body.innerText || '');
                    })()
                    """
                )
            except Exception as e:
                result["err"] = str(e)
            finally:
                try:
                    voice.cancel_voice_listen()
                except Exception:
                    pass
                try:
                    window.destroy()
                except Exception:
                    pass

        def on_loaded():
            if result.get("_started"):
                return
            result["_started"] = True
            threading.Thread(target=work, daemon=True).start()

        window.events.loaded += on_loaded
        webview.start(private_mode=False, storage_path=str(storage))
        print("RESULT", json.dumps({k: v for k, v in result.items() if k != "_started"}, indent=2))
        ok = bool(result.get("api")) and result.get("chat_has_user") is True
        print("PASS" if ok else "FAIL")
        return 0 if ok else 2
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
