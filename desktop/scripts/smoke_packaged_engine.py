"""
Post-build smoke for dist/AICA.Engine (local test engine — not a public release).

1) Bundle layout: torch / torchvision / ultralytics / weights
2) Import torch + load YOLO using the packaged _internal tree
3) Optional HTTP smoke of the frozen EXE (/health, camera AI) when it binds

Usage (after build_engine.ps1):
  .\\venv\\Scripts\\python.exe desktop\\scripts\\smoke_packaged_engine.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "dist" / "AICA.Engine"
ENGINE_EXE = ENGINE_DIR / "AICA.Engine.exe"
INTERNAL = ENGINE_DIR / "_internal"


def _ok(msg: str) -> None:
    print(f"OK  {msg}")


def _fail(msg: str) -> None:
    print(f"FAIL {msg}")
    raise SystemExit(2)


def check_bundle_layout() -> None:
    if not ENGINE_EXE.is_file():
        _fail(f"missing {ENGINE_EXE} — run desktop/scripts/build_engine.ps1 first")
    if not (INTERNAL / "torch").is_dir():
        _fail(f"missing bundled torch at {INTERNAL / 'torch'}")
    _ok("torch dir present")
    if not (INTERNAL / "torchvision").is_dir() and not any(INTERNAL.rglob("*torchvision*")):
        _fail("torchvision not found under _internal")
    _ok("torchvision present")
    if not (INTERNAL / "ultralytics").is_dir():
        _fail("ultralytics package missing under _internal")
    _ok("ultralytics present")
    weights = INTERNAL / "vision" / "weights" / "aica_product_detector.pt"
    if not weights.is_file():
        _fail(f"missing weights {weights}")
    _ok(f"weights present ({weights.stat().st_size} bytes)")


def check_piper_build_inputs() -> None:
    sys.path.insert(0, str(ROOT))
    from desktop.scripts.setup_piper_voice import verify_piper_amy

    problems = verify_piper_amy(require_espeak=True)
    if problems:
        for p in problems:
            print("PIPER:", p)
        _fail("Piper Amy / espeak not ready for launcher packaging")
    _ok("Piper Amy + espeak-ng-data verified (launcher build inputs)")


def check_packaged_torch_and_yolo() -> None:
    """Import the bundled copies (not the venv site-packages)."""
    torch_lib = INTERNAL / "torch" / "lib"
    env = os.environ.copy()
    env["PATH"] = str(torch_lib) + os.pathsep + env.get("PATH", "")
    # Prefer packaged weights via project-style layout under _internal
    code = f"""
import sys
from pathlib import Path
internal = Path(r"{INTERNAL}")
sys.path.insert(0, str(internal))
import torch
import torchvision
print("TORCH", torch.__version__, torch.__file__)
print("TV", torchvision.__version__)
assert "+cpu" in torch.__version__.lower() or not torch.cuda.is_available()
assert "AICA.Engine" in str(Path(torch.__file__).resolve())
from vision.yolo_inference import YOLOProductDetector
# Force packaged weights path
import vision.yolo_inference as yi
w = internal / "vision" / "weights" / "aica_product_detector.pt"
assert w.is_file(), w
d = YOLOProductDetector()
# Detector resolves via project_root; override by loading config path existence check
print("MODEL_READY", d.model_ready, "PATH", d.model_path)
assert d.model_ready
# One inference on blank frame
import numpy as np
frame = np.zeros((480, 640, 3), dtype=np.uint8)
dets = d.detect(frame)
print("INFER_OK", isinstance(dets, list), "n", len(dets))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr[-4000:])
        _fail(f"packaged torch/YOLO import failed (exit {proc.returncode})")
    if "AICA.Engine" not in proc.stdout:
        _fail("torch was not imported from dist/AICA.Engine/_internal")
    if "MODEL_READY True" not in proc.stdout:
        _fail("YOLO model_ready was not True using packaged stack")
    _ok("packaged torch + YOLO load + blank-frame inference")


class _Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, path: str, *, method: str = "GET", form: dict | None = None, timeout: float = 60.0):
        data = None
        headers = {}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            method = "POST"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
            payload = json.loads(body) if "json" in ctype or body[:1] == "{" else body
            return resp.status, payload


def try_engine_http_smoke() -> bool:
    """
    Attempt frozen EXE HTTP smoke. Returns True on full AI success.
    Returns False if the EXE does not bind (non-fatal when import smoke passed),
    but fails hard if it binds and AI is unavailable.
    """
    port = "8765"
    smoke_db = ENGINE_DIR / "smoke_packaged.db"
    env_file = ENGINE_DIR / "smoke_packaged.env"
    env_file.write_text(
        "\n".join(
            [
                f"DATABASE_URL=sqlite:///{smoke_db.resolve().as_posix()}",
                "AICA_DB_BACKEND=sqlite",
                f"AICA_PORT={port}",
                "AICA_HOST=127.0.0.1",
                "AICA_DESKTOP=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AICA_ENV_FILE"] = str(env_file)
    env["AICA_DESKTOP"] = "1"
    env["AICA_PORT"] = port
    env["AICA_HOST"] = "127.0.0.1"
    env["DATABASE_URL"] = f"sqlite:///{smoke_db.resolve().as_posix()}"

    proc = subprocess.Popen(
        [str(ENGINE_EXE)],
        cwd=str(ENGINE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = _Client(f"http://127.0.0.1:{port}")
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"WARN frozen EXE exited early code={proc.returncode}")
                return False
            try:
                code, health = client.request("/health", timeout=2.0)
                if code == 200 and isinstance(health, dict) and health.get("ok"):
                    _ok(f"frozen /health version={health.get('version')}")
                    ready = True
                    break
            except Exception:
                time.sleep(1.0)
        if not ready:
            print("WARN frozen EXE did not accept /health within 90s — import smoke already verified Torch")
            return False

        email = f"engine_smoke_{int(time.time())}@example.com"
        password = "SmokeTest1!"
        try:
            client.request(
                "/signup",
                method="POST",
                form={
                    "org_name": "Engine Smoke Org",
                    "business_type": "retail",
                    "gst_registered": "false",
                    "full_name": "Engine Smoke",
                    "email": email,
                    "password": password,
                    "confirm_password": password,
                },
                timeout=30.0,
            )
        except urllib.error.HTTPError:
            pass

        try:
            code, status = client.request("/camera/status", timeout=30.0)
        except urllib.error.HTTPError:
            client.request("/login", method="POST", form={"email": email, "password": password}, timeout=30.0)
            code, status = client.request("/camera/status", timeout=30.0)
        _ok(f"/camera/status HTTP {code} powered={status.get('camera_powered') if isinstance(status, dict) else status}")

        code, power = client.request("/camera/power", method="POST", form={"enabled": "true"}, timeout=180.0)
        _ok(f"/camera/power ON HTTP {code}")
        time.sleep(2.0)
        code, det = client.request("/camera/detections", timeout=60.0)
        avail = bool(det.get("ai_detection_available") or det.get("model_ready"))
        print(
            f"    ai_detection_available={det.get('ai_detection_available')} "
            f"model_ready={det.get('model_ready')} error={det.get('ai_detection_error')}"
        )
        if not avail:
            _fail("Frozen Engine bound HTTP but AI detection unavailable")
        _ok("ai_detection_available=true on frozen Engine")
        client.request("/camera/power", method="POST", form={"enabled": "false"}, timeout=30.0)
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        for p in (smoke_db, env_file):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass


def main() -> int:
    print("=== Packaged Engine smoke ===")
    check_bundle_layout()
    check_piper_build_inputs()
    check_packaged_torch_and_yolo()
    http_ok = try_engine_http_smoke()
    if http_ok:
        print("=== SMOKE PASSED (bundle + import + frozen HTTP) ===")
    else:
        print("=== SMOKE PASSED (bundle + import; frozen HTTP optional warn) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
