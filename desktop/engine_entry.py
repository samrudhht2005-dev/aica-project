"""
Packaged FastAPI engine entrypoint (PyInstaller target).

Runs uvicorn on 127.0.0.1 with AICA_PORT / AICA_HOST from the environment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repo / bundle root on path when frozen
if getattr(sys, "frozen", False):
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    sys.path.insert(0, str(root))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.runtime_paths import load_runtime_env, logs_dir, APP_VERSION  # noqa: E402


def main() -> None:
    load_runtime_env()
    os.environ.setdefault("AICA_DESKTOP", "1")
    os.environ.setdefault("AICA_VERSION", APP_VERSION)
    host = os.environ.get("AICA_HOST", "127.0.0.1")
    port = int(os.environ.get("AICA_PORT", "8765"))

    # Configure logging to AppData when desktop
    try:
        log_file = logs_dir() / "uvicorn.log"
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
        )
    except Exception:
        pass

    # Register ORM models, then initialize schema before accepting HTTP traffic.
    # create_all(checkfirst=True) is serialized with a cross-process lock so
    # concurrent AICA.Engine startups cannot race on the same SQLite file.
    import models.db_models  # noqa: F401
    from database.schema_init import init_database_schema

    init_database_schema()

    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        log_level="info",
        factory=False,
    )


if __name__ == "__main__":
    main()
