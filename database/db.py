from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
import sys

# Desktop/web: load AppData config.env + project .env before reading DATABASE_URL
try:
    from backend.runtime_paths import (
        load_runtime_env,
        resolve_database_url,
        database_config_error_message,
        is_frozen,
        is_sqlite_url,
        desktop_sqlite_url,
    )
    load_runtime_env()
except Exception:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    def resolve_database_url():
        return os.getenv("DATABASE_URL")

    def database_config_error_message():
        return "DATABASE_URL is not configured."

    def is_frozen():
        return bool(getattr(sys, "frozen", False))

    def is_sqlite_url(url):
        return bool(url) and str(url).lower().startswith("sqlite:")

    def desktop_sqlite_url():
        from pathlib import Path
        base = os.environ.get("AICA_APPDATA") or os.environ.get("APPDATA") or "."
        path = Path(base) / "AICA" / "aica.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite:///" + path.resolve().as_posix()


DATABASE_URL = resolve_database_url()

if not DATABASE_URL:
    if is_frozen():
        DATABASE_URL = desktop_sqlite_url()
    else:
        # Pure web/dev without .env: last-resort local default (not used for packaged desktop).
        DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/aica_db"


def _configure_sqlite_connection(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _make_engine(url: str):
    if is_sqlite_url(url):
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(eng, "connect", _configure_sqlite_connection)
        return eng
    return create_engine(url)


engine = _make_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()
