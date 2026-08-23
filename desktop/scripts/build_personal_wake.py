"""
Build personal Hey IRA wake profile from a completed calibration session.

Usage:
  python desktop/scripts/build_personal_wake.py
  python desktop/scripts/build_personal_wake.py --session "%APPDATA%\\AICA\\logs\\wake_calibration\\session_20260823_123725"

Writes (AppData only — not packaged in AICA.exe):
  %LOCALAPPDATA%\\AICA\\voice\\models\\personal_hey_ira_embeddings.npz
  %LOCALAPPDATA%\\AICA\\voice\\models\\personal_hey_ira.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_wake_personal import (
    PersonalWakeProfile,
    build_personal_profile_from_wavs,
    save_personal_profile,
)
from desktop.scripts.calibrate_wake_voice import calibration_root


def default_session() -> Path:
    latest = calibration_root() / "latest_report.json"
    if latest.is_file():
        data = json.loads(latest.read_text(encoding="utf-8"))
        return Path(data["session_dir"])
    sessions = sorted(calibration_root().glob("session_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        raise SystemExit("No calibration session found.")
    return sessions[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build personal Hey IRA wake profile")
    parser.add_argument("--session", type=Path, default=None, help="Calibration session directory")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.03,
        help="Recommended strong margin threshold (default 0.03)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not backup existing personal profile files",
    )
    args = parser.parse_args()

    session = (args.session or default_session()).resolve()
    pos_dir = session / "positive"
    if not pos_dir.is_dir():
        print("Missing positive samples:", pos_dir)
        return 1

    wavs = sorted(pos_dir.glob("*.wav"))
    if not wavs:
        print("No positive WAV files in", pos_dir)
        return 1

    hey = [w for w in wavs if w.stem.startswith("pos_") and "hey" in w.stem]
    hi = [w for w in wavs if w.stem.startswith("pos_hi_")]
    paths = [(w.stem, w) for w in wavs]
    print(f"Building personal profile from {len(paths)} samples")
    print(f"  Hey Aira: {len(hey)}  |  Hi Aira: {len(hi)}")
    print("Session:", session)
    if not hi:
        print("WARNING: no Hi Aira samples — run calibrate_wake_voice.py --collect-hi-aira first")

    embeddings, centroid, labels = build_personal_profile_from_wavs(paths)
    npz_path, meta_path = save_personal_profile(
        embeddings=embeddings,
        centroid=centroid,
        labels=labels,
        session_id=session.name,
        session_dir=str(session),
        recommended_threshold=args.threshold,
        backup_existing=not args.no_backup,
    )

    print("OK — personal_wake_embeddings:", embeddings.shape)
    print("OK — npz ->", npz_path)
    print("OK — meta ->", meta_path)
    print("Profile enabled flag preserved from prior meta (if any).")
    print("After evaluation approval, enable with threshold 0.03:")
    print("  python desktop/scripts/enable_personal_wake.py --enable --threshold 0.03")
    print("Run: python desktop/scripts/evaluate_wake_phase2_validation.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
