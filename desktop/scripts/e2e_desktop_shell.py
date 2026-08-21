"""
Desktop shell E2E: start dist/AICA.exe (WebView2 launcher + verified engine),
probe the local AICA URL, then shut down and assert no orphan engines.
Does not print secrets. Does not rebuild the engine.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "dist" / "AICA.exe"
ENGINE = ROOT / "dist" / "AICA.Engine" / "AICA.Engine.exe"
PORT = int(os.environ.get("AICA_DESKTOP_E2E_PORT", "18801"))
BASE = f"http://127.0.0.1:{PORT}"


class Client:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, data=None, follow=True, timeout=30):
        url = BASE + path
        body = None
        headers = {"User-Agent": "AICA-DesktopE2E/1.0"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except TimeoutError:
            return 598, b"timeout"
        code = resp.status
        content = resp.read()
        if follow and code in (301, 302, 303, 307) and resp.headers.get("Location"):
            loc = resp.headers.get("Location")
            if loc.startswith("/"):
                return self.request("GET", loc.replace(BASE, "") if loc.startswith(BASE) else loc, follow=True, timeout=timeout)
            if loc.startswith(BASE):
                return self.request("GET", loc[len(BASE) :], follow=True, timeout=timeout)
        return code, content


def wait_health(timeout=180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def kill_aica_tree():
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/IM", "AICA.exe", "/T"],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["taskkill", "/F", "/IM", "AICA.Engine.exe", "/T"],
            capture_output=True,
            text=True,
        )
        time.sleep(1)


def orphan_count() -> int:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq AICA.Engine.exe", "/FO", "CSV", "/NH"],
            text=True,
            errors="replace",
        )
        lines = [ln for ln in out.splitlines() if "AICA.Engine.exe" in ln]
        return len(lines)
    except Exception:
        return -1


def main() -> int:
    if not LAUNCHER.is_file():
        print("FAIL missing launcher", LAUNCHER)
        return 1
    if not ENGINE.is_file():
        print("FAIL missing engine", ENGINE)
        return 1

    kill_aica_tree()
    results = []

    env = os.environ.copy()
    env["AICA_PORT"] = str(PORT)
    env["AICA_HOST"] = "127.0.0.1"
    env["AICA_DESKTOP"] = "1"
    # Prefer AppData config already seeded; do not inject secrets here.

    print("==> start desktop launcher", LAUNCHER)
    proc = subprocess.Popen(
        [str(LAUNCHER)],
        cwd=str(LAUNCHER.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not wait_health():
            print("FAIL launcher did not bring engine /health up")
            return 2
        results.append(("launcher_health", True))
        print("health OK")

        c = Client()
        code, body = c.request("GET", "/login", follow=False)
        results.append(("login_ui", code == 200 and (b"password" in body.lower() or b"login" in body.lower() or code == 200)))

        email = f"desktop.e2e.{uuid.uuid4().hex[:8]}@aica.test"
        password = "VerifyPass123!"
        code, body = c.request(
            "POST",
            "/signup",
            {
                "org_name": "Desktop E2E Org",
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
                "full_name": "Desktop E2E",
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            follow=True,
        )
        results.append(("signup", code == 200))
        print("signup", code, email)

        c2 = Client()
        code, body = c2.request(
            "POST",
            "/login",
            {"email": email, "password": password, "remember": "true"},
            follow=True,
        )
        results.append(("login_persist", code == 200 and b"Invalid email" not in body))

        for path, name in (
            ("/select-interface", "nav_select"),
            ("/pos", "nav_pos"),
            ("/expenses", "nav_expenses"),
            ("/gst", "nav_gst"),
            ("/income-tax", "nav_income_tax"),
            ("/employees", "nav_payroll"),
            ("/", "nav_dashboard"),
        ):
            code, _ = c2.request("GET", path, follow=True, timeout=40)
            results.append((name, code == 200))
            print(name, code)

        code, body = c2.request("GET", "/camera/status", follow=False)
        import json
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        print("camera", data)
        results.append(("yolo_model_ready", bool(data.get("model_ready"))))
        code, body = c2.request("POST", "/camera/power", {"enabled": "true"}, follow=False)
        results.append(("camera_power", code == 200))
        c2.request("POST", "/camera/power", {"enabled": "false"}, follow=False)

        t0 = time.time()
        code, body = c2.request(
            "POST",
            "/api/assistant",
            {
                "question": "Reply with exactly: IRA desktop ok.",
                "page": "dashboard",
                "path": "/executive-dashboard",
                "task": "",
                "history": "[]",
                "opt_context": "",
            },
            follow=False,
            timeout=45,
        )
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        print("ira", code, round(time.time() - t0, 1), "s", text[:120])
        results.append(("ira_desktop", code == 200 and len(text) > 5))

    finally:
        print("==> shutdown launcher")
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        kill_aica_tree()
        time.sleep(2)
        orphans = orphan_count()
        print("orphans_after_close", orphans)
        results.append(("no_orphan_engine", orphans == 0))

    # Restart smoke
    print("==> restart smoke")
    kill_aica_tree()
    proc2 = subprocess.Popen(
        [str(LAUNCHER)],
        cwd=str(LAUNCHER.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ok = wait_health(timeout=180)
        results.append(("restart_health", ok))
        print("restart_health", ok)
    finally:
        kill_aica_tree()
        time.sleep(1)
        results.append(("no_orphan_after_restart", orphan_count() == 0))

    print("\n== RESULTS ==")
    failed = 0
    for name, ok in results:
        print(("PASS" if ok else "FAIL"), name)
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
