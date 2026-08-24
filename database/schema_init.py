"""Idempotent database schema initialization for AICA.

The original production failure was a cross-process race: multiple engine
processes called ``Base.metadata.create_all(checkfirst=True)`` on the same
SQLite file. Check-then-create is not atomic, so the second process raised
``sqlite3.OperationalError: table users already exists``, import/startup
failed, and the launcher timed out waiting for ``/health``.

This module keeps SQLAlchemy's normal ``create_all(checkfirst=True)`` path
(so ORM FKs, uniques, indexes, and dialect DDL stay authoritative) and makes
it safe by serializing init with:

- an in-process ``RLock`` (re-entrant; nested calls do not deadlock)
- a cross-process lock file beside the SQLite DB (Windows ``msvcrt`` / POSIX ``fcntl``)

Call ``init_database_schema()`` from an explicit startup path — not at model
import time.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_init_lock = threading.RLock()
_initialized_urls: set[str] = set()
_active_url: str | None = None  # URL currently being initialized (re-entrancy)


def _sqlite_db_path(url: str) -> Path | None:
    """Best-effort filesystem path for a SQLite SQLAlchemy URL."""
    raw = (url or "").strip()
    if not raw.lower().startswith("sqlite:"):
        return None
    # sqlite:///C:/path/db.db  or  sqlite:////absolute/path
    if raw.startswith("sqlite:////"):
        path_part = raw[len("sqlite:///"):]  # keep leading /
    elif raw.startswith("sqlite:///"):
        path_part = raw[len("sqlite:///"):]
    else:
        return None
    if not path_part or path_part == ":memory:" or path_part.startswith("file:"):
        return None
    path_part = path_part.split("?", 1)[0]
    return Path(path_part)


def _acquire_file_lock(db_path: Path):
    """
    Cross-process exclusive lock beside the SQLite file (Windows + POSIX).

    The lock is held on an open file handle. When the process exits (clean or
    crash), the OS releases the lock — a leftover ``.schema.lock`` *file* on
    disk is not a permanent lock and will not block future startups.
    """
    lock_path = Path(str(db_path) + ".schema.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            # Ensure the file has at least one byte to lock. Do not read byte 0
            # first — on Windows a concurrent holder of msvcrt.locking makes
            # read() raise PermissionError.
            fh.seek(0, os.SEEK_END)
            if fh.tell() < 1:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            # LK_LOCK blocks until the byte is free (waits; never a stale permanent lock).
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        fh.close()
        raise
    return fh


def _release_file_lock(fh) -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _run_portable_indexes(eng) -> None:
    """Apply the same portable unique indexes upgrade_schema uses for SQLite."""
    from sqlalchemy import text

    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_product_org_name "
        "ON products(org_id, name) WHERE org_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_employee_org_code "
        "ON employees(org_id, employee_id) WHERE org_id IS NOT NULL",
    )
    with eng.begin() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
            except Exception as e:
                log.warning("Skipped portable index during test init: %s", e)


def init_database_schema(*, force: bool = False, bind=None, metadata=None) -> None:
    """
    Ensure all ORM tables exist, then run additive upgrades.

    Uses SQLAlchemy ``MetaData.create_all(..., checkfirst=True)`` under a
    cross-process lock so concurrent engine startups cannot race.

    Does not drop tables or delete rows. Safe on fresh installs, existing
    installs, and repeated starts.

    ``bind`` / ``metadata`` are optional overrides for tests; production uses
    ``database.db.engine`` and ``Base.metadata``.
    """
    from database.db import Base, engine as default_engine
    from database.schema_upgrade import upgrade_schema

    eng = bind if bind is not None else default_engine
    meta = metadata if metadata is not None else Base.metadata
    url_key = str(eng.url)

    global _active_url
    with _init_lock:
        if not force and url_key in _initialized_urls:
            return
        # Same-thread re-entrancy while init is in progress (e.g. startup →
        # import → nested call): do not acquire the file lock again / deadlock.
        if _active_url == url_key:
            return

        file_lock = None
        _active_url = url_key
        try:
            if (db_path := _sqlite_db_path(url_key)) is not None:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                file_lock = _acquire_file_lock(db_path)

            # Authoritative SQLAlchemy schema generation (FKs, uniques, indexes).
            meta.create_all(bind=eng, checkfirst=True)

            # upgrade_schema() uses database.db.engine — custom binds use local DDL.
            if bind is None:
                upgrade_schema()
            else:
                _run_portable_indexes(eng)

            _initialized_urls.add(url_key)
            log.info("AICA database schema initialized (create_all + upgrades).")
        finally:
            _release_file_lock(file_lock)
            _active_url = None


def reset_schema_init_state_for_tests() -> None:
    """Clear in-process init memoization (unit tests only)."""
    global _active_url
    with _init_lock:
        _initialized_urls.clear()
        _active_url = None
