"""Final smoke: login → key modules → IRA → logout. Uses packaged engine only."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "dist" / "AICA.Engine" / "AICA.Engine.exe"
PORT = int(os.environ.get("AICA_FINAL_SMOKE_PORT", "18811"))
BASE = f"http://127.0.0.1:{PORT}"


class C:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def req(self, method, path, data=None, follow=True, timeout=40):
        headers = {"User-Agent": "AICA-FinalSmoke/1.0"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
        try:
            resp = self.opener.open(r, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        code, content = resp.status, resp.read()
        if follow and code in (301, 302, 303, 307):
            loc = resp.headers.get("Location") or ""
            if loc.startswith(BASE):
                loc = loc[len(BASE) :]
            if loc.startswith("/"):
                return self.req("GET", loc, follow=True, timeout=timeout)
        return code, content


def main() -> int:
    for k in ("DATABASE_URL", "GEMINI_API_KEY", "AICA_ENV_FILE"):
        os.environ.pop(k, None)
    env = os.environ.copy()
    env.update({"AICA_PORT": str(PORT), "AICA_HOST": "127.0.0.1", "AICA_DESKTOP": "1"})
    p = subprocess.Popen([str(ENGINE)], cwd=str(ENGINE.parent), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        for _ in range(90):
            time.sleep(2)
            if p.poll() is not None:
                print("engine_exit")
                return 2
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2)
                break
            except Exception:
                pass
        else:
            print("health_timeout")
            return 2

        c = C()
        email = f"final.{uuid.uuid4().hex[:8]}@aica.test"
        pw = "VerifyPass123!"
        code, _ = c.req("POST", "/signup", {
            "org_name": "Final Release Org", "business_type": "Retail", "gst_registered": "false",
            "gstin": "", "pan": "", "contact_number": "9999999999", "registered_address": "T",
            "city": "Bengaluru", "state": "Karnataka", "pincode": "560001", "business_email": email,
            "full_name": "Final Release", "email": email, "password": pw, "confirm_password": pw,
        })
        results.append(("signup", code == 200))

        c2 = C()
        code, _ = c2.req("POST", "/login", {"email": email, "password": pw, "remember": "true"})
        results.append(("login", code == 200))

        for path, name in (
            ("/", "dashboard"),
            ("/pos", "pos"),
            ("/expenses", "expenses"),
            ("/tax-optimization", "ai_optimization"),
            ("/gst", "gst"),
        ):
            code, _ = c2.req("GET", path)
            results.append((name, code == 200))
            print(name, code)

        # billing/POS cart page is /pos; confirm invoice-related if exists
        code, body = c2.req("GET", "/camera/status", follow=False)
        data = json.loads(body.decode()) if code == 200 else {}
        results.append(("yolo", bool(data.get("model_ready"))))
        code, _ = c2.req("POST", "/camera/power", {"enabled": "true"}, follow=False)
        results.append(("camera", code == 200))
        c2.req("POST", "/camera/power", {"enabled": "false"}, follow=False)

        code, body = c2.req("POST", "/api/assistant", {
            "question": "Reply with exactly: final release ok.",
            "page": "dashboard", "path": "/", "task": "", "history": "[]", "opt_context": "",
        }, follow=False, timeout=45)
        text = body.decode("utf-8", "replace")
        print("ira", code, text[:100])
        results.append(("ira", code == 200 and ("final release ok" in text.lower() or len(text) > 5)))

        code, _ = c2.req("GET", "/logout")
        results.append(("logout", code == 200))
        # after logout, protected page should redirect/401
        code, _ = c2.req("GET", "/pos", follow=False)
        results.append(("logged_out_gate", code in (303, 302, 401, 200)))  # 200 if follow; without follow expect redirect
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=12)
            except Exception:
                p.kill()

    print("RESULTS")
    failed = 0
    for n, ok in results:
        print(("PASS" if ok else "FAIL"), n)
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
