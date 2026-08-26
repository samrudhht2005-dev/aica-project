"""
Download Piper en_US-amy-medium voice for IRA TTS (deterministic, offline after download).

  python desktop/scripts/setup_piper_voice.py

Writes:
  desktop/voice/models/piper/en_US-amy-medium.onnx
  desktop/voice/models/piper/en_US-amy-medium.onnx.json

The ~60 MB .onnx is intentionally NOT tracked in Git (see desktop/voice/models/.gitignore).
Packaged builds must run this script (or copy the files) before PyInstaller so Amy is present.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import piper_voice_config_path, piper_voice_dir, piper_voice_model_path

HF_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
    "en/en_US/amy/medium"
)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  -> {dest}")
    urllib.request.urlretrieve(url, dest)


def main() -> int:
    model = piper_voice_model_path()
    config = piper_voice_config_path()
    piper_voice_dir().mkdir(parents=True, exist_ok=True)

    if model.is_file() and model.stat().st_size > 1_000_000 and config.is_file():
        print("OK Piper Amy already at", model)
        print("OK config", config)
        return 0

    _download(f"{HF_BASE}/en_US-amy-medium.onnx", model)
    _download(f"{HF_BASE}/en_US-amy-medium.onnx.json", config)
    print("OK Piper Amy voice ready:", model)
    print(f"  model_bytes={model.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
