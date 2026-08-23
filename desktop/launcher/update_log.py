"""Structured logging for the AICA auto-update checker."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("aica.update")


def ulog(msg: str, **extra: Any) -> None:
    line = msg
    if extra:
        try:
            line = f"{msg} {json.dumps(extra, default=str)}"
        except Exception:
            line = f"{msg} {extra}"
    logger.info(line)
    try:
        from backend.runtime_paths import logs_dir

        path = logs_dir() / "update.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass
