"""
AICA.Updater.exe entry point.

Usage:
  AICA.Updater.exe --handoff <path-to-handoff.json>
  python -m desktop.updater.main --handoff <path> [--dry-run]

Does not download. Only applies a Phase-4 verified staged installer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="AICA.Updater", add_help=True)
    parser.add_argument(
        "--handoff",
        required=True,
        help="Path to handoff.json under %%TEMP%%\\AICA\\updates\\{version}\\",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and simulate; never run the installer or restart AICA.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for AICA/engine exit (default 120).",
    )
    args = parser.parse_args(argv)

    handoff_path = Path(args.handoff)

    if args.dry_run and handoff_path.is_file():
        try:
            data = json.loads(handoff_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["dry_run"] = True
                handoff_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    from desktop.updater.updater_apply import apply_from_handoff

    return apply_from_handoff(
        handoff_path,
        wait_timeout_s=float(args.wait_timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
