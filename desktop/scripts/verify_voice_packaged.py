"""Verify packaged AICA.exe voice stack (no full app install)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "dist" / "AICA.exe"


def _read_log(name: str) -> str:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "AICA" / "logs"
    p = base / name
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def main() -> int:
    if not LAUNCHER.is_file():
        print("FAIL missing", LAUNCHER)
        return 1

    env = os.environ.copy()
    env.pop("AICA_VOICE_MODELS_DIR", None)
    env.pop("AICA_VOICE_BACKEND", None)

    print("==> whisper-minimal-test")
    code1 = subprocess.call([str(LAUNCHER), "--whisper-minimal-test"], env=env, cwd=str(LAUNCHER.parent))
    print("MINIMAL_EXIT", code1)
    print(_read_log("whisper_minimal_test.log") or "(no minimal log)")

    print("==> voice-selftest")
    code2 = subprocess.call([str(LAUNCHER), "--voice-selftest"], env=env, cwd=str(LAUNCHER.parent))
    print("SELFTEST_EXIT", code2)
    print(_read_log("voice_selftest.log") or "(no selftest log)")

    ok = code1 == 0 and code2 == 0
    print("VERIFY_OK" if ok else "VERIFY_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
