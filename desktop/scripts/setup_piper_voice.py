"""
Download + verify Piper en_US-amy-medium for IRA TTS (deterministic).

  python desktop/scripts/setup_piper_voice.py
  python desktop/scripts/setup_piper_voice.py --verify-only

Writes (gitignored .onnx; tracked .onnx.json):
  desktop/voice/models/piper/en_US-amy-medium.onnx
  desktop/voice/models/piper/en_US-amy-medium.onnx.json

Packaged launcher builds MUST run this (or --verify-only after copy) before PyInstaller.
A release must NEVER ship without Amy — build_launcher.ps1 / aica_launcher.spec fail-fast.
"""
from __future__ import annotations

import argparse
import hashlib
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

# Pinned to rhasspy/piper-voices v1.0.0 en_US-amy-medium (verified 2026-08-26).
EXPECTED_ONNX_SHA256 = "b3a6e47b57b8c7fbe6a0ce2518161a50f59a9cdd8a50835c02cb02bdd6206c18"
EXPECTED_ONNX_MIN_BYTES = 50_000_000
EXPECTED_JSON_SHA256 = "95a23eb4d42909d38df73bb9ac7f45f597dbfcde2d1bf9526fdeaf5466977d77"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  -> {dest}")
    urllib.request.urlretrieve(url, dest)


def verify_piper_amy(*, require_espeak: bool = False) -> list[str]:
    """
    Return a list of human-readable problems (empty == OK).
    """
    problems: list[str] = []
    model = piper_voice_model_path()
    config = piper_voice_config_path()

    if not model.is_file():
        problems.append(f"missing model: {model}")
    else:
        size = model.stat().st_size
        if size < EXPECTED_ONNX_MIN_BYTES:
            problems.append(f"model too small ({size} bytes): {model}")
        digest = _sha256_file(model)
        if digest != EXPECTED_ONNX_SHA256:
            problems.append(
                f"model SHA-256 mismatch for {model.name}: "
                f"got {digest}, expected {EXPECTED_ONNX_SHA256}"
            )

    if not config.is_file():
        problems.append(f"missing config: {config}")
    else:
        digest = _sha256_file(config)
        if digest != EXPECTED_JSON_SHA256:
            problems.append(
                f"config SHA-256 mismatch for {config.name}: "
                f"got {digest}, expected {EXPECTED_JSON_SHA256}"
            )

    if require_espeak:
        from desktop.launcher.voice_paths import piper_espeak_data_dir

        espeak = piper_espeak_data_dir()
        if not espeak.is_dir():
            problems.append(f"missing piper espeak-ng-data: {espeak}")
        else:
            # Phoneme tables must exist for en_US.
            probe = espeak / "phontab"
            if not probe.is_file():
                # Some layouts nest under espeak-ng-data directly with phontab
                alt = list(espeak.rglob("phontab"))
                if not alt:
                    problems.append(f"espeak-ng-data incomplete (no phontab) under {espeak}")

    return problems


def ensure_piper_amy(*, force: bool = False) -> int:
    model = piper_voice_model_path()
    config = piper_voice_config_path()
    piper_voice_dir().mkdir(parents=True, exist_ok=True)

    if not force:
        problems = verify_piper_amy(require_espeak=False)
        if not problems:
            print("OK Piper Amy verified at", model)
            print("OK config", config)
            print(f"  sha256={EXPECTED_ONNX_SHA256}")
            return 0
        for p in problems:
            print("VERIFY:", p)

    _download(f"{HF_BASE}/en_US-amy-medium.onnx", model)
    _download(f"{HF_BASE}/en_US-amy-medium.onnx.json", config)

    problems = verify_piper_amy(require_espeak=False)
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 2

    print("OK Piper Amy voice ready:", model)
    print(f"  model_bytes={model.stat().st_size}")
    print(f"  sha256={EXPECTED_ONNX_SHA256}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download/verify Piper Amy voice for AICA")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify local files (no download). Exit 2 on failure.",
    )
    parser.add_argument(
        "--require-espeak",
        action="store_true",
        help="Also require piper espeak-ng-data (installed with piper-tts).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if verification would pass.",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        problems = verify_piper_amy(require_espeak=args.require_espeak)
        if problems:
            for p in problems:
                print("FAIL:", p)
            print("Run: python desktop/scripts/setup_piper_voice.py")
            return 2
        print("OK Piper Amy verification passed")
        return 0

    code = ensure_piper_amy(force=args.force)
    if code != 0:
        return code
    if args.require_espeak:
        problems = verify_piper_amy(require_espeak=True)
        if problems:
            for p in problems:
                print("FAIL:", p)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
