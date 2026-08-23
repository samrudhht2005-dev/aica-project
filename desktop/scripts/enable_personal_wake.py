"""Enable personal Hey IRA wake in AppData metadata (after evaluation approval)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from desktop.launcher.voice_paths import personal_wake_meta_path, personal_wake_npz_path
from desktop.launcher.voice_wake_personal import PersonalWakeProfile, resolve_personal_wake


def main() -> int:
    parser = argparse.ArgumentParser(description="Enable/disable personal wake in metadata")
    parser.add_argument("--enable", action="store_true", help="Set enabled:true and margin_threshold")
    parser.add_argument("--disable", action="store_true", help="Set enabled:false")
    parser.add_argument("--threshold", type=float, default=0.03)
    args = parser.parse_args()

    meta_path = personal_wake_meta_path()
    if not meta_path.is_file():
        print("Missing", meta_path, "- run build_personal_wake.py first")
        return 1
    if not personal_wake_npz_path().is_file():
        print("Missing", personal_wake_npz_path())
        return 1

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if args.disable:
        meta["enabled"] = False
    elif args.enable:
        meta["enabled"] = True
        meta["margin_threshold"] = float(args.threshold)
        meta["recommended_threshold"] = float(args.threshold)
    else:
        active, thr, profile = resolve_personal_wake()
        print("enabled:", meta.get("enabled"))
        print("margin_threshold:", meta.get("margin_threshold"))
        print("resolve_personal_wake:", active, thr, profile is not None)
        return 0

    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    active, thr, profile = resolve_personal_wake()
    print("Wrote", meta_path)
    print("resolve_personal_wake:", active, "threshold=", thr, "profile=", profile is not None)
    return 0 if active or args.disable else 1


if __name__ == "__main__":
    raise SystemExit(main())
