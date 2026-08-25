"""POS checkout integration for WeighTicket lines (manual token path)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WeighCheckoutIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        db = Path(cls._tmpdir.name) / "weigh_checkout.db"
        url = "sqlite:///" + db.resolve().as_posix()
        os.environ["DATABASE_URL"] = url
        os.environ["AICA_DESKTOP"] = "1"
        os.environ["AICA_DB_BACKEND"] = "sqlite"

        from database.schema_init import init_database_schema, reset_schema_init_state_for_tests
        import models.db_models  # noqa: F401

        reset_schema_init_state_for_tests()
        init_database_schema(force=True)

        from database.db import SessionLocal
        from models.db_models import Organization, Product, User
        from backend.auth import hash_password, SESSION_COOKIE
        from fastapi.testclient import TestClient
        import backend.main as mainmod

        db_sess = SessionLocal()
        try:
            org_a = Organization(name="Checkout Org A")
            org_b = Organization(name="Checkout Org B")
            db_sess.add_all([org_a, org_b])
            db_sess.flush()
            user_a = User(
                org_id=org_a.id,
                full_name="Cashier A",
                email="checkout.a@example.com",
                password_hash=hash_password("securePass9"),
            )
            user_b = User(
                org_id=org_b.id,
                full_name="Cashier B",
                email="checkout.b@example.com",
                password_hash=hash_password("securePass9"),
            )
            db_sess.add_all([user_a, user_b])
            db_sess.flush()
            rice = Product(org_id=org_a.id, name="Rice", stock=42.0, price=50.0, product_type="loose")
            ketchup = Product(org_id=org_a.id, name="Ketchup", stock=20.0, price=30.0, product_type="packaged")
            db_sess.add_all([rice, ketchup])
            db_sess.commit()
            cls.org_a_id = org_a.id
            cls.org_b_id = org_b.id
            cls.user_a_id = user_a.id
            cls.user_b_id = user_b.id
            cls.rice_id = rice.id
            cls.ketchup_id = ketchup.id
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
        try:
            cls._tmpdir.cleanup()
        except PermissionError:
            pass

    def _auth(self, uid: int, oid: int):
        from backend.auth import create_session_token

        self.client.cookies.set(
            self.SESSION_COOKIE, create_session_token(uid, oid, remember=False)
        )

    def _create_ticket(self, product_id: int, weight: float, **kwargs):
        from database.db import SessionLocal
        from backend.weigh_tickets import create_weigh_ticket

        db = SessionLocal()
        try:
            ticket = create_weigh_ticket(
                db,
                org_id=self.org_a_id,
                product_id=product_id,
                weight=weight,
                created_by_user_id=self.user_a_id,
                **kwargs,
            )
            return ticket
        finally:
            db.close()

    def _stock(self, product_id: int) -> float:
        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            return float(db.get(Product, product_id).stock)
        finally:
            db.close()

    def _ticket(self, ticket_id: int):
        from database.db import SessionLocal
        from models.db_models import WeighTicket

        db = SessionLocal()
        try:
            return db.get(WeighTicket, ticket_id)
        finally:
            db.close()

    def test_successful_ticket_checkout_reduces_stock_and_consumes(self):
        self._auth(self.user_a_id, self.org_a_id)
        ticket = self._create_ticket(self.rice_id, 2.5)
        self.assertEqual(self._stock(self.rice_id), 42.0)

        resolve = self.client.post(
            "/api/weigh-tickets/resolve",
            json={"public_token": ticket.public_token},
        )
        self.assertEqual(resolve.status_code, 200)
        self.assertTrue(resolve.json()["ok"])
        self.assertEqual(self._stock(self.rice_id), 42.0)

        checkout = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 999.0,  # client tamper ignored for ticket lines
                    "quantity": 99.0,
                    "weigh_ticket_token": ticket.public_token,
                }
            ],
        )
        self.assertEqual(checkout.status_code, 200, checkout.text[:500])
        self.assertTrue(
            (checkout.headers.get("content-type") or "").startswith("application/pdf")
            or checkout.json().get("ok")
        )

        self.assertAlmostEqual(self._stock(self.rice_id), 39.5, places=3)
        row = self._ticket(ticket.id)
        self.assertEqual(row.status, "consumed")
        self.assertIsNotNone(row.consumed_at)
        self.assertIsNotNone(row.transaction_id)

    def test_replay_rejected_no_extra_stock_deduction(self):
        self._auth(self.user_a_id, self.org_a_id)
        ticket = self._create_ticket(self.rice_id, 1.0)
        first = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1,
                    "weigh_ticket_token": ticket.public_token,
                }
            ],
        )
        self.assertEqual(first.status_code, 200, first.text[:300])
        stock_after = self._stock(self.rice_id)

        second = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1,
                    "weigh_ticket_token": ticket.public_token,
                }
            ],
        )
        self.assertEqual(second.status_code, 409)
        body = second.json()
        self.assertEqual(body.get("code"), "already_purchased")
        self.assertEqual(self._stock(self.rice_id), stock_after)

        resolve = self.client.post(
            "/api/weigh-tickets/resolve",
            json={"public_token": ticket.public_token},
        )
        self.assertEqual(resolve.status_code, 409)
        self.assertEqual(resolve.json().get("code"), "already_purchased")

    def test_rollback_keeps_ticket_active_when_stock_insufficient(self):
        self._auth(self.user_a_id, self.org_a_id)
        ticket = self._create_ticket(self.rice_id, 2.0)
        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            p = db.get(Product, self.rice_id)
            p.stock = 1.0  # less than ticket weight
            db.commit()
        finally:
            db.close()

        res = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 2,
                    "weigh_ticket_token": ticket.public_token,
                }
            ],
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._ticket(ticket.id).status, "active")
        self.assertAlmostEqual(self._stock(self.rice_id), 1.0, places=3)

        # Restore stock for later tests
        db = SessionLocal()
        try:
            db.get(Product, self.rice_id).stock = 42.0
            db.commit()
        finally:
            db.close()

    def test_rollback_when_commit_fails_after_claim(self):
        """Simulate failure after mutations — ticket must not stay consumed."""
        from database.db import SessionLocal
        from models.db_models import Product, Transaction
        from backend.weigh_tickets import (
            claim_active_ticket_for_checkout,
            create_weigh_ticket,
        )

        db = SessionLocal()
        try:
            ticket = create_weigh_ticket(
                db,
                org_id=self.org_a_id,
                product_id=self.rice_id,
                weight=0.5,
                created_by_user_id=self.user_a_id,
                commit=True,
            )
            token = ticket.public_token
            tid = ticket.id
            stock_before = float(db.get(Product, self.rice_id).stock)

            product = db.get(Product, self.rice_id)
            product.stock = stock_before - 0.5
            tx = Transaction(
                org_id=self.org_a_id,
                product_name="Bill (1 items)",
                price=0.0,
                quantity=0.0,
                gst_percent=0.0,
                category="[]",
                gst_amount=0.0,
                total_amount=25.0,
            )
            db.add(tx)
            db.flush()
            claim_active_ticket_for_checkout(
                db,
                org_id=self.org_a_id,
                public_token=token,
                transaction_id=tx.id,
            )
            db.rollback()  # simulate failure before durable commit

            db.expire_all()
            again = db.get(type(ticket), tid)
            self.assertEqual(again.status, "active")
            self.assertIsNone(again.transaction_id)
            self.assertAlmostEqual(float(db.get(Product, self.rice_id).stock), stock_before, places=3)
        finally:
            db.close()

    def test_org_isolation_on_checkout(self):
        self._auth(self.user_a_id, self.org_a_id)
        ticket = self._create_ticket(self.rice_id, 1.0)
        self._auth(self.user_b_id, self.org_b_id)
        res = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1,
                    "weigh_ticket_token": ticket.public_token,
                }
            ],
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self._ticket(ticket.id).status, "active")

    def test_cancelled_and_expired_rejected(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import cancel_weigh_ticket

        self._auth(self.user_a_id, self.org_a_id)
        cancelled = self._create_ticket(self.rice_id, 1.0)
        db = SessionLocal()
        try:
            cancel_weigh_ticket(db, org_id=self.org_a_id, ticket_id=cancelled.id, reason="void")
        finally:
            db.close()
        res = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1,
                    "weigh_ticket_token": cancelled.public_token,
                }
            ],
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json().get("code"), "cancelled")

        expired = self._create_ticket(
            self.rice_id,
            1.0,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        res2 = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1,
                    "weigh_ticket_token": expired.public_token,
                }
            ],
        )
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json().get("code"), "cancelled")

    def test_duplicate_ticket_in_same_checkout_rejected(self):
        self._auth(self.user_a_id, self.org_a_id)
        ticket = self._create_ticket(self.rice_id, 1.0)
        line = {
            "product": "Rice",
            "price": 50,
            "quantity": 1,
            "weigh_ticket_token": ticket.public_token,
        }
        res = self.client.post("/add_multiple", json=[line, line])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("code"), "duplicate_ticket")
        self.assertEqual(self._ticket(ticket.id).status, "active")

    def test_mixed_normal_and_ticket_checkout(self):
        self._auth(self.user_a_id, self.org_a_id)
        # Reset rice stock to known value
        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            db.get(Product, self.rice_id).stock = 42.0
            db.get(Product, self.ketchup_id).stock = 20.0
            db.commit()
        finally:
            db.close()

        ticket = self._create_ticket(self.rice_id, 2.5)
        res = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 2.5,
                    "weigh_ticket_token": ticket.public_token,
                },
                {"product": "Ketchup", "price": 30, "quantity": 2},
            ],
        )
        self.assertEqual(res.status_code, 200, res.text[:400])
        self.assertAlmostEqual(self._stock(self.rice_id), 39.5, places=3)
        self.assertAlmostEqual(self._stock(self.ketchup_id), 18.0, places=3)
        self.assertEqual(self._ticket(ticket.id).status, "consumed")

    def test_non_ticket_checkout_regression(self):
        self._auth(self.user_a_id, self.org_a_id)
        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            db.get(Product, self.ketchup_id).stock = 20.0
            db.commit()
        finally:
            db.close()
        before = self._stock(self.ketchup_id)
        res = self.client.post(
            "/add_multiple",
            json=[{"product": "Ketchup", "price": 30, "quantity": 1}],
        )
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertAlmostEqual(self._stock(self.ketchup_id), before - 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
