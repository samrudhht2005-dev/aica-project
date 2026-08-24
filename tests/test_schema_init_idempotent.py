"""
Regression: schema init must be idempotent on SQLite (all tables).

Protects against the packaged-desktop failure where concurrent
``create_all(checkfirst=True)`` raised ``table users already exists``,
crashing engine startup before ``/health`` answered.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_sqlite_url(tmpdir: str) -> str:
    path = Path(tmpdir) / "aica_schema_test.db"
    return "sqlite:///" + path.resolve().as_posix()


class SchemaInitIdempotentTests(unittest.TestCase):
    def test_import_models_does_not_create_tables(self):
        """Schema init must not run as a side effect of importing models."""
        source = (ROOT / "models" / "db_models.py").read_text(encoding="utf-8")
        # No executable call — comments may mention the function name.
        self.assertNotRegex(source, r"(?m)^\s*init_database_schema\s*\(")
        self.assertNotRegex(source, r"(?m)^\s*Base\.metadata\.create_all\s*\(")

        import models.db_models  # noqa: F401
        from database.db import Base
        from sqlalchemy import create_engine, inspect

        with tempfile.TemporaryDirectory() as tmp:
            url = _fresh_sqlite_url(tmp)
            eng = create_engine(url)
            # Virgin DB — import alone must not create tables on this engine
            self.assertEqual(inspect(eng).get_table_names(), [])
            self.assertIn("users", Base.metadata.tables)
            eng.dispose()

    def test_create_all_under_lock_preserves_data_and_survives_rerun(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        import models.db_models as models  # noqa: F401
        from database.db import Base
        from database.schema_init import (
            init_database_schema,
            reset_schema_init_state_for_tests,
        )

        with tempfile.TemporaryDirectory() as tmp:
            url = _fresh_sqlite_url(tmp)
            eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
            reset_schema_init_state_for_tests()

            init_database_schema(force=True, bind=eng, metadata=Base.metadata)

            table_names = set(Base.metadata.tables.keys())
            self.assertIn("users", table_names)
            self.assertGreaterEqual(len(table_names), 10)

            with eng.connect() as conn:
                existing = {r[0] for r in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))}
            for name in table_names:
                self.assertIn(name, existing, f"missing table after first init: {name}")

            Session = sessionmaker(bind=eng)
            db = Session()
            try:
                org = models.Organization(name="Preserve Org", business_type="Private Ltd")
                db.add(org)
                db.flush()
                user = models.User(
                    org_id=org.id,
                    full_name="Keep Me",
                    email="keepme@example.com",
                    password_hash="hash",
                )
                db.add(user)
                db.commit()
                org_id, user_id, email = org.id, user.id, user.email
            finally:
                db.close()

            reset_schema_init_state_for_tests()
            init_database_schema(force=True, bind=eng, metadata=Base.metadata)

            db = Session()
            try:
                u = db.query(models.User).filter_by(email="keepme@example.com").one()
                self.assertEqual(u.id, user_id)
                self.assertEqual(u.org_id, org_id)
                self.assertEqual(u.full_name, "Keep Me")
                self.assertEqual(u.email, email)
                o = db.query(models.Organization).filter_by(id=org_id).one()
                self.assertEqual(o.name, "Preserve Org")
            finally:
                db.close()

            eng.dispose()

    def test_concurrent_threads_do_not_raise_already_exists(self):
        from sqlalchemy import create_engine, text

        import models.db_models as models  # noqa: F401
        from database.db import Base
        from database.schema_init import (
            init_database_schema,
            reset_schema_init_state_for_tests,
        )

        with tempfile.TemporaryDirectory() as tmp:
            url = _fresh_sqlite_url(tmp)
            eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
            reset_schema_init_state_for_tests()
            errors: list[BaseException] = []

            def worker(_i: int) -> None:
                try:
                    init_database_schema(force=True, bind=eng, metadata=Base.metadata)
                except BaseException as e:
                    errors.append(e)

            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = [pool.submit(worker, i) for i in range(8)]
                for f in as_completed(futs):
                    f.result()

            self.assertEqual(errors, [], f"concurrent init errors: {errors!r}")

            with eng.connect() as conn:
                names = {r[0] for r in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))}
            for name in Base.metadata.tables:
                self.assertIn(name, names)

            from sqlalchemy.orm import sessionmaker
            Session = sessionmaker(bind=eng)
            db = Session()
            try:
                org = models.Organization(name="Race Org")
                db.add(org)
                db.commit()
                self.assertIsNotNone(org.id)
            finally:
                db.close()
            eng.dispose()

    def test_reentrant_init_does_not_deadlock(self):
        """Nested init_database_schema from the same thread must return, not hang."""
        from sqlalchemy import create_engine

        import models.db_models  # noqa: F401
        from database.db import Base
        from database.schema_init import (
            init_database_schema,
            reset_schema_init_state_for_tests,
            _init_lock,
            _active_url,
        )

        with tempfile.TemporaryDirectory() as tmp:
            url = _fresh_sqlite_url(tmp)
            eng = create_engine(url, connect_args={"check_same_thread": False})
            reset_schema_init_state_for_tests()

            nested_ok = {"called": False}

            original_create_all = Base.metadata.create_all

            def wrapping_create_all(*args, **kwargs):
                nested_ok["called"] = True
                # Simulate indirect re-entry during create_all
                init_database_schema(force=True, bind=eng, metadata=Base.metadata)
                return original_create_all(*args, **kwargs)

            Base.metadata.create_all = wrapping_create_all  # type: ignore[method-assign]
            try:
                done = threading.Event()

                def run():
                    init_database_schema(force=True, bind=eng, metadata=Base.metadata)
                    done.set()

                t = threading.Thread(target=run)
                t.start()
                finished = done.wait(timeout=15)
                t.join(timeout=1)
                self.assertTrue(finished, "re-entrant init deadlocked")
                self.assertTrue(nested_ok["called"])
            finally:
                Base.metadata.create_all = original_create_all  # type: ignore[method-assign]
                reset_schema_init_state_for_tests()
                eng.dispose()

    def test_legacy_unlocked_create_all_race(self):
        """Document the old failure mode (unlocked check-then-create race)."""
        from sqlalchemy import Column, ForeignKey, Integer, String, create_engine
        from sqlalchemy.orm import declarative_base

        with tempfile.TemporaryDirectory() as tmp:
            url = _fresh_sqlite_url(tmp)
            eng = create_engine(url, connect_args={"check_same_thread": False})
            Base = declarative_base()

            class Organization(Base):
                __tablename__ = "organizations"
                id = Column(Integer, primary_key=True)

            class User(Base):
                __tablename__ = "users"
                id = Column(Integer, primary_key=True)
                org_id = Column(Integer, ForeignKey("organizations.id"))
                email = Column(String)

            barrier = threading.Barrier(6)
            errors: list[BaseException] = []

            def worker():
                try:
                    barrier.wait(timeout=5)
                    Base.metadata.create_all(bind=eng)
                except BaseException as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            already = [e for e in errors if "already exists" in str(e).lower()]
            if already:
                self.assertTrue(
                    any("users" in str(e) or "organizations" in str(e) for e in already)
                )
            eng.dispose()


class ConcurrentProcessSchemaInitTests(unittest.TestCase):
    def test_concurrent_processes_init_same_sqlite(self):
        """Real multi-process race against one SQLite file — must not fail."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "multiproc.db"
            url = "sqlite:///" + db_path.resolve().as_posix()
            worker = textwrap.dedent(
                f"""
                import os, sys
                os.environ["DATABASE_URL"] = {url!r}
                os.environ["AICA_DESKTOP"] = "1"
                sys.path.insert(0, {str(ROOT)!r})
                import models.db_models  # register tables only
                from database.db import Base
                from database.schema_init import init_database_schema, reset_schema_init_state_for_tests
                from sqlalchemy import create_engine
                reset_schema_init_state_for_tests()
                eng = create_engine({url!r}, connect_args={{'check_same_thread': False, 'timeout': 60}})
                init_database_schema(force=True, bind=eng, metadata=Base.metadata)
                eng.dispose()
                print("PROC_OK")
                """
            )
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", worker],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env={**os.environ, "DATABASE_URL": url, "AICA_DESKTOP": "1"},
                )
                for _ in range(4)
            ]
            results = []
            for p in procs:
                out, err = p.communicate(timeout=90)
                results.append((p.returncode, out, err))

            failures = [r for r in results if r[0] != 0 or "PROC_OK" not in r[1]]
            self.assertEqual(
                failures,
                [],
                "concurrent process init failed:\n"
                + "\n".join(f"rc={a} out={b!r} err={c!r}" for a, b, c in failures),
            )


class EngineHealthAfterSchemaInitTests(unittest.TestCase):
    def test_backend_startup_health_with_existing_sqlite(self):
        """
        Subprocess: build DB, insert row, import backend.main (startup init),
        hit /health, confirm data preserved.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "engine_health.db"
            url = "sqlite:///" + db_path.resolve().as_posix()
            script = textwrap.dedent(
                f"""
                import os, sys
                os.environ["DATABASE_URL"] = {url!r}
                os.environ["AICA_DESKTOP"] = "1"
                os.environ["AICA_DB_BACKEND"] = "sqlite"
                sys.path.insert(0, {str(ROOT)!r})

                from database.schema_init import init_database_schema, reset_schema_init_state_for_tests
                import models.db_models as m
                from database.db import SessionLocal

                reset_schema_init_state_for_tests()
                init_database_schema(force=True)

                db = SessionLocal()
                try:
                    org = m.Organization(name="Health Org")
                    db.add(org)
                    db.flush()
                    db.add(m.User(
                        org_id=org.id,
                        full_name="Health User",
                        email="health@example.com",
                        password_hash="x",
                    ))
                    db.commit()
                    email = "health@example.com"
                finally:
                    db.close()

                # Fresh process-style: reset memo and let FastAPI startup init again
                reset_schema_init_state_for_tests()

                from fastapi.testclient import TestClient
                import backend.main as mainmod
                client = TestClient(mainmod.app)
                r = client.get("/health")
                assert r.status_code == 200, r.text
                assert r.json().get("ok") is True

                db = SessionLocal()
                try:
                    u = db.query(m.User).filter_by(email=email).one()
                    assert u.full_name == "Health User"
                finally:
                    db.close()
                print("HEALTH_OK")
                """
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "DATABASE_URL": url, "AICA_DESKTOP": "1"},
            )
            if proc.returncode != 0:
                self.fail(
                    f"subprocess failed ({proc.returncode})\n"
                    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
            self.assertIn("HEALTH_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
