"""
Verify dist/AICA_1.0.2_verify packaged engine with isolated SQLite.
Does not touch %LOCALAPPDATA%\\AICA or %APPDATA%\\AICA.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "dist" / "AICA_1.0.2_verify"
ENGINE = VERIFY / "AICA.Engine.exe"
PORT = int(os.environ.get("AICA_VERIFY_PORT", "18791"))
BASE = f"http://127.0.0.1:{PORT}"
APPDATA = Path(os.environ.get("TEMP", str(ROOT / "dist"))) / "AICA_1.0.2_packtest"


class Client:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, data=None, raw=None, headers=None, timeout=60):
        url = BASE + path
        hdrs = {"User-Agent": "AICA-PackVerify/1.0.2"}
        if headers:
            hdrs.update(headers)
        body = raw
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            resp = self.opener.open(req, timeout=timeout)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


def wait_health(timeout=120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def start_engine():
    APPDATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for k in ("DATABASE_URL", "AICA_DB_BACKEND", "AICA_SQLITE_PATH", "AICA_ENV_FILE"):
        env.pop(k, None)
    env["AICA_APPDATA"] = str(APPDATA)
    env["AICA_DESKTOP"] = "1"
    env["AICA_VERSION"] = "1.0.2"
    env["AICA_HOST"] = "127.0.0.1"
    env["AICA_PORT"] = str(PORT)
    log = APPDATA / "engine_verify.log"
    lf = open(log, "a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [str(ENGINE)],
        cwd=str(VERIFY),
        env=env,
        stdout=lf,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return proc, log


def stop_engine(proc):
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def main() -> int:
    if not ENGINE.is_file():
        print("FAIL missing", ENGINE)
        return 1

    pos_html = VERIFY / "_internal" / "frontend" / "templates" / "pos.html"
    pos_js = VERIFY / "_internal" / "frontend" / "static" / "pos_intelligence.js"
    html = pos_html.read_text(encoding="utf-8", errors="replace") if pos_html.is_file() else ""
    js = pos_js.read_text(encoding="utf-8", errors="replace") if pos_js.is_file() else ""
    for label, ok in [
        ("pos-qty-control", "pos-qty-control" in html),
        ("pos-action-btn New Sale", "pos-action-btn" in html and "posQuickNewSale" in html),
        ("historyHint", "pos.historyHint" in html),
        ("invoicesHint", "pos.invoicesHint" in html),
        ("KPI theme tokens", "color: var(--text" in html),
        ("downloadInvoiceFile", "downloadInvoiceFile" in js),
        ("renderInvoiceRows", "renderInvoiceRows" in js),
    ]:
        print(("PASS" if ok else "FAIL"), "POS bundle:", label)
        if not ok:
            return 1

    proc, log = start_engine()
    try:
        if not wait_health():
            print("FAIL health. log:", log)
            try:
                print(log.read_text(encoding="utf-8")[-2000:])
            except Exception:
                pass
            return 1
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            health = json.loads(r.read().decode("utf-8"))
        print("HEALTH", health)
        if str(health.get("version") or "") not in ("1.0.2", "1.0.1"):
            print("WARN version", health.get("version"))

        db_path = APPDATA / "aica.db"
        print("SQLITE_PATH", db_path)

        c = Client()
        suffix = uuid.uuid4().hex[:8]
        email = f"pack_{suffix}@example.com"
        code, _, body = c.request("POST", "/signup", data={
            "org_name": f"Pack Co {suffix}",
            "business_type": "Private Ltd",
            "gst_registered": "false",
            "gstin": "",
            "pan": "AAACA1234B",
            "contact_number": "9999999999",
            "registered_address": "1 Test Street",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "business_email": email,
            "full_name": "Pack Admin",
            "email": email,
            "password": "securePass9",
            "confirm_password": "securePass9",
        })
        print("SIGNUP", code)
        if code not in (200, 303):
            print(body[:400])
            return 1

        code, _, _ = c.request("POST", "/add_product", data={"name": "Maggi", "price": "15", "stock": "10"})
        assert code in (200, 303), code
        code, _, raw = c.request(
            "POST", "/add_multiple",
            raw=json.dumps([{"product": "Maggi", "price": 15, "quantity": 2}]).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/pdf, application/json"},
        )
        print("SALE", code, (raw[:4] if raw else b""))
        if code != 200 or not raw.startswith(b"%PDF"):
            print(raw[:400])
            return 1

        code, _, products = c.request("GET", "/api/products")
        maggi = next(p for p in json.loads(products) if p["name"].lower() == "maggi")
        print("STOCK", maggi["stock"])
        if abs(float(maggi["stock"]) - 8) > 0.01:
            return 1

        code, _, hist = c.request("GET", "/api/pos/history")
        items = json.loads(hist)["items"]
        tx_id = items[0]["id"]
        if abs(float(items[0]["grand_total"]) - 35.40) > 0.05:
            print("FAIL totals", items[0])
            return 1
        code, hdrs, pdf = c.request("GET", f"/download_invoice/{tx_id}", headers={"Accept": "application/pdf, application/json"})
        cd = str(hdrs.get("Content-Disposition") or "")
        print("INVOICE", code, cd, pdf[:4])
        if code != 200 or not pdf.startswith(b"%PDF") or "attachment" not in cd.lower():
            return 1

        code, _, intel = c.request("GET", "/api/pos/intelligence")
        data = json.loads(intel)
        print("INTEL empty", data.get("empty"), "tx", (data.get("today") or {}).get("transactions"))
        if data.get("empty"):
            return 1

        for path in ("/pos", "/sales", "/"):
            code, _, page = c.request("GET", path)
            if code != 200:
                print("FAIL page", path, code)
                return 1
            if path == "/pos" and "pos-qty-control" not in page.decode("utf-8", "replace"):
                print("FAIL qty markup missing on /pos")
                return 1

        cam, _, status = c.request("GET", "/camera/status")
        print("CAMERA_STATUS", cam, status[:200])

        if not db_path.is_file():
            print("FAIL sqlite file missing", db_path)
            return 1
        first_size = db_path.stat().st_size
    finally:
        stop_engine(proc)

    proc2, _ = start_engine()
    try:
        if not wait_health():
            print("FAIL restart health")
            return 1
        c2 = Client()
        code, _, _ = c2.request("POST", "/login", data={
            "email": email, "password": "securePass9",
        })
        if code not in (200, 303):
            print("FAIL login after restart", code)
            return 1
        code, _, products = c2.request("GET", "/api/products")
        maggi2 = next(p for p in json.loads(products) if p["name"].lower() == "maggi")
        if abs(float(maggi2["stock"]) - 8) > 0.01:
            print("FAIL stock after restart", maggi2)
            return 1
        if not db_path.is_file() or db_path.stat().st_size < first_size * 0.5:
            print("FAIL persistence size", db_path.stat().st_size if db_path.is_file() else None)
            return 1
        print("PERSISTENCE_OK", db_path, "bytes", db_path.stat().st_size)
    finally:
        stop_engine(proc2)

    print("PACKAGED_SQLITE_VERIFY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
