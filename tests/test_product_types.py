"""Loose vs packaged product type: model, validation, weigh eligibility, checkout."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Bind SQLite before any database.db import (see test_product_type_upgrade guard).
_TEST_TMP = tempfile.TemporaryDirectory()
_TEST_DB = Path(_TEST_TMP.name) / "product_types.db"
os.environ["DATABASE_URL"] = "sqlite:///" + _TEST_DB.resolve().as_posix()
os.environ["AICA_DESKTOP"] = "1"
os.environ["AICA_DB_BACKEND"] = "sqlite"


class ProductTypeUnitTests(unittest.TestCase):
    def test_normalize_and_units(self):
        from backend.product_types import (
            PRODUCT_TYPE_LOOSE,
            PRODUCT_TYPE_PACKAGED,
            normalize_product_type,
            sale_unit_for,
            validate_sale_quantity,
            validate_stock_value,
            can_change_product_type,
            ProductTypeError,
        )

        self.assertEqual(normalize_product_type("Loose"), PRODUCT_TYPE_LOOSE)
        self.assertEqual(normalize_product_type("packaged"), PRODUCT_TYPE_PACKAGED)
        self.assertEqual(sale_unit_for("loose"), "kg")
        self.assertEqual(sale_unit_for("packaged"), "unit")

        self.assertEqual(validate_stock_value("loose", 2.5), 2.5)
        self.assertEqual(validate_stock_value("packaged", 50), 50.0)
        with self.assertRaises(ProductTypeError):
            validate_stock_value("packaged", 2.5)
        with self.assertRaises(ProductTypeError):
            validate_sale_quantity("packaged", 2.5)
        self.assertEqual(validate_sale_quantity("packaged", 2), 2.0)
        self.assertEqual(validate_sale_quantity("loose", 2.5), 2.5)

        can_change_product_type(current_type="packaged", new_type="loose", current_stock=50)
        with self.assertRaises(ProductTypeError) as ctx:
            can_change_product_type(current_type="loose", new_type="packaged", current_stock=42.5)
        self.assertEqual(ctx.exception.code, "type_change_fractional_stock")
        can_change_product_type(current_type="loose", new_type="packaged", current_stock=42.0)


class ProductTypeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database.schema_init import init_database_schema, reset_schema_init_state_for_tests
        import models.db_models  # noqa: F401

        reset_schema_init_state_for_tests()
        init_database_schema(force=True)

        from database.db import SessionLocal
        from models.db_models import Organization, User
        from backend.auth import hash_password, SESSION_COOKIE
        from fastapi.testclient import TestClient
        import backend.main as mainmod

        db_sess = SessionLocal()
        try:
            org = Organization(name="Type Org")
            db_sess.add(org)
            db_sess.flush()
            user = User(
                org_id=org.id,
                full_name="Type User",
                email="types@example.com",
                password_hash=hash_password("securePass9"),
            )
            db_sess.add(user)
            db_sess.commit()
            cls.org_id = org.id
            cls.user_id = user.id
        finally:
            db_sess.close()

        cls.client = TestClient(mainmod.app)
        cls.SESSION_COOKIE = SESSION_COOKIE

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.close()
        except Exception:
            pass

    def _auth(self):
        from backend.auth import create_session_token

        token = create_session_token(self.user_id, self.org_id, remember=False)
        self.client.cookies.set(self.SESSION_COOKIE, token)

    def test_schema_has_product_type_default_packaged(self):
        from sqlalchemy import inspect
        from database.db import engine, SessionLocal
        from models.db_models import Product

        cols = {c["name"] for c in inspect(engine).get_columns("products")}
        self.assertIn("product_type", cols)

        db = SessionLocal()
        try:
            p = Product(org_id=self.org_id, name="Legacy Candy", stock=10.0, price=5.0)
            db.add(p)
            db.commit()
            db.refresh(p)
            self.assertEqual(p.product_type, "packaged")
        finally:
            db.close()

    def test_create_loose_and_packaged_via_warehouse(self):
        self._auth()
        r1 = self.client.post(
            "/add_product",
            data={"name": "Rice", "price": "50", "stock": "42", "product_type": "loose"},
            follow_redirects=False,
        )
        self.assertIn(r1.status_code, (302, 303))
        r2 = self.client.post(
            "/add_product",
            data={"name": "Cadbury Dairy Milk", "price": "40", "stock": "50", "product_type": "packaged"},
            follow_redirects=False,
        )
        self.assertIn(r2.status_code, (302, 303))

        # Fractional packaged stock rejected
        bad = self.client.post(
            "/add_product",
            data={"name": "Coke", "price": "30", "stock": "2.5", "product_type": "packaged"},
            follow_redirects=False,
        )
        self.assertIn(bad.status_code, (302, 303))
        self.assertIn("error=", bad.headers.get("location", ""))

        products = self.client.get("/api/products").json()
        by_name = {p["name"]: p for p in products}
        self.assertEqual(by_name["Rice"]["product_type"], "loose")
        self.assertEqual(by_name["Rice"]["unit"], "kg")
        self.assertEqual(by_name["Cadbury Dairy Milk"]["product_type"], "packaged")
        self.assertEqual(by_name["Cadbury Dairy Milk"]["unit"], "unit")
        self.assertNotIn("Coke", by_name)

    def test_weigh_list_and_ticket_rejects_packaged(self):
        self._auth()
        # Ensure products exist
        self.client.post(
            "/add_product",
            data={"name": "Rice", "price": "50", "stock": "0", "product_type": "loose"},
            follow_redirects=False,
        )
        self.client.post(
            "/add_product",
            data={"name": "Cadbury Dairy Milk", "price": "40", "stock": "0", "product_type": "packaged"},
            follow_redirects=False,
        )
        page = self.client.get("/weigh")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Rice", page.content)
        self.assertNotIn(b"Cadbury", page.content)

        products = {p["name"]: p for p in self.client.get("/api/products").json()}
        rice_id = products["Rice"]["id"]
        cad_id = products["Cadbury Dairy Milk"]["id"]

        ok = self.client.post(
            "/api/weigh-tickets",
            json={"product_id": rice_id, "weight": 2.5, "unit": "kg"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(ok.json().get("ok"))

        bad = self.client.post(
            "/api/weigh-tickets",
            json={"product_id": cad_id, "weight": 2.5, "unit": "kg"},
        )
        self.assertEqual(bad.status_code, 400, bad.text)
        body = bad.json()
        self.assertEqual(body.get("code"), "not_loose")

    def test_checkout_packaged_fractional_rejected_and_integer_ok(self):
        self._auth()
        self.client.post(
            "/add_product",
            data={"name": "Cadbury Dairy Milk", "price": "40", "stock": "0", "product_type": "packaged"},
            follow_redirects=False,
        )
        # Ensure stock
        self.client.post(
            "/add_product",
            data={"name": "Cadbury Dairy Milk", "price": "40", "stock": "10", "product_type": "packaged"},
            follow_redirects=False,
        )

        with patch("backend.routes.update_ai_insights_and_recommendations_bg"):
            bad = self.client.post(
                "/add_multiple",
                json=[{"product": "Cadbury Dairy Milk", "price": 40, "quantity": 2.5}],
            )
            self.assertEqual(bad.status_code, 400, bad.text)
            self.assertEqual(bad.json().get("code"), "fractional_packaged_qty")

            ok = self.client.post(
                "/add_multiple",
                json=[{"product": "Cadbury Dairy Milk", "price": 40, "quantity": 2}],
            )
            self.assertEqual(ok.status_code, 200, ok.text)

        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            p = db.query(Product).filter(
                Product.org_id == self.org_id,
                Product.name == "Cadbury Dairy Milk",
            ).first()
            # Started from prior tests may vary; after +10 and -2 at least integer semantics held
            self.assertTrue(float(p.stock) == int(float(p.stock)))
        finally:
            db.close()

    def test_type_change_fractional_stock_rejected(self):
        self._auth()
        self.client.post(
            "/add_product",
            data={"name": "Sugar", "price": "40", "stock": "12.5", "product_type": "loose"},
            follow_redirects=False,
        )
        bad = self.client.post(
            "/add_product",
            data={"name": "Sugar", "price": "40", "stock": "0", "product_type": "packaged"},
            follow_redirects=False,
        )
        self.assertIn(bad.status_code, (302, 303))
        loc = bad.headers.get("location", "")
        self.assertIn("error=", loc)

        # Whole stock can convert
        self.client.post(
            "/add_product",
            data={"name": "Flour", "price": "30", "stock": "20", "product_type": "loose"},
            follow_redirects=False,
        )
        ok = self.client.post(
            "/add_product",
            data={"name": "Flour", "price": "30", "stock": "0", "product_type": "packaged"},
            follow_redirects=False,
        )
        self.assertIn(ok.status_code, (302, 303))
        self.assertEqual(ok.headers.get("location"), "/warehouse")


if __name__ == "__main__":
    unittest.main()
