"""Shared voice subsystem logging."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("aica.voice")


def voice_log_path():
    try:
        from backend.runtime_paths import logs_dir

        return logs_dir() / "voice.log"
    except Exception:
        base = os.environ.get("APPDATA") or "."
        d = os.path.join(base, "AICA", "logs")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "voice.log")


def vlog(msg: str, **extra: Any) -> None:
    line = msg
    if extra:
        try:
            line = f"{msg} {json.dumps(extra, default=str)}"
        except Exception:
            line = f"{msg} {extra}"
    logger.info(line)
    try:
        path = voice_log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass
