"""Isolated check: additive schema upgrade defaults legacy products to packaged."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Prevent this module (alphabetically first) from binding database.db to Postgres
# before sibling TestClient suites set their SQLite URLs.
_os_tmp = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(_os_tmp.name) / "upgrade_guard.db").resolve().as_posix()
os.environ["AICA_DESKTOP"] = "1"
os.environ["AICA_DB_BACKEND"] = "sqlite"


class ProductTypeUpgradeTests(unittest.TestCase):
    def test_upgrade_defaults_existing_rows_to_packaged(self):
        from sqlalchemy import create_engine, text, inspect
        from database.schema_upgrade import _ensure_product_type_column

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "legacy.db"
        url = "sqlite:///" + path.resolve().as_posix()

        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE products ("
                "id INTEGER PRIMARY KEY, org_id INTEGER, name VARCHAR, "
                "stock FLOAT, price FLOAT, created_at TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO products (id, org_id, name, stock, price) "
                "VALUES (1, 1, 'Old Rice', 12.5, 40)"
            ))
        self.assertNotIn("product_type", {c["name"] for c in inspect(eng).get_columns("products")})

        with eng.begin() as conn:
            _ensure_product_type_column(conn, dialect="sqlite")

        cols = {c["name"] for c in inspect(eng).get_columns("products")}
        self.assertIn("product_type", cols)
        with eng.begin() as conn:
            row = conn.execute(text("SELECT product_type, stock FROM products WHERE id=1")).one()
        self.assertEqual(row[0], "packaged")
        self.assertEqual(float(row[1]), 12.5)
        eng.dispose()


if __name__ == "__main__":
    unittest.main()
