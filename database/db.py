from sqlalchemy import create_engine
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


DATABASE_URL = resolve_database_url()

if not DATABASE_URL:
    # Desktop / packaged: fail clearly — never connect to template host "HOST".
    if is_frozen() or os.environ.get("AICA_DESKTOP") == "1":
        msg = database_config_error_message()
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)
    # Pure web/dev without .env: last-resort local default (not used for desktop).
    DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/aica_db"

engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()
