"""Timestamped voice diagnostics for packaged vs dev debugging."""
from __future__ import annotations

import os
import sys
import time
from typing import Any

from desktop.launcher.voice_log import vlog


def vdiag(stage: str, **extra: Any) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"
    frozen = getattr(sys, "frozen", False)
    payload = {"stage": stage, "ts": ts, "frozen": frozen, **extra}
    vlog(f"[VOICE] {stage}", **payload)
    try:
        from backend.runtime_paths import logs_dir

        path = logs_dir() / "voice_diag.log"
        line = f"{ts} [VOICE] {stage}"
        if extra:
            parts = " ".join(f"{k}={extra[k]!r}" for k in sorted(extra))
            line = f"{line} {parts}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
