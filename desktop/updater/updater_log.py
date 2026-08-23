"""Dedicated logging for AICA.Updater.exe."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _log_path() -> Path:
    override = os.environ.get("AICA_APPDATA")
    if override:
        base = Path(override)
    else:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "AICA"
    path = base / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "updater.log"


def ulog(msg: str, **extra: Any) -> None:
    line = msg
    if extra:
        try:
            # Never log secrets; callers must not pass secrets.
            line = f"{msg} {json.dumps(extra, default=str)}"
        except Exception:
            line = f"{msg} {extra}"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}\n"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    try:
        print(text, end="", flush=True)
    except Exception:
        pass
