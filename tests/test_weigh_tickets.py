"""Weigh-ticket backend foundation tests (no UI / camera / checkout wiring)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_sqlite_url(tmpdir: str) -> str:
    path = Path(tmpdir) / "weigh_tickets_test.db"
    return "sqlite:///" + path.resolve().as_posix()


class WeighTicketFoundationTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import models.db_models  # noqa: F401
        from database.db import Base
        from database.schema_init import (
            init_database_schema,
            reset_schema_init_state_for_tests,
        )
        from models.db_models import Organization, Product, User
        from backend.auth import hash_password

        self._tmpdir = tempfile.TemporaryDirectory()
        url = _fresh_sqlite_url(self._tmpdir.name)
        self.engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 30}
        )
        reset_schema_init_state_for_tests()
        init_database_schema(force=True, bind=self.engine, metadata=Base.metadata)

        Session = sessionmaker(bind=self.engine)
        self.db = Session()

        self.org_a = Organization(name="Org A")
        self.org_b = Organization(name="Org B")
        self.db.add_all([self.org_a, self.org_b])
        self.db.flush()

        self.user_a = User(
            org_id=self.org_a.id,
            full_name="Alice",
            email="alice@weigh.test",
            password_hash=hash_password("pass"),
        )
        self.user_b = User(
            org_id=self.org_b.id,
            full_name="Bob",
            email="bob@weigh.test",
            password_hash=hash_password("pass"),
        )
        self.db.add_all([self.user_a, self.user_b])
        self.db.flush()

        self.rice_a = Product(
            org_id=self.org_a.id, name="Rice", stock=100.0, price=50.0, product_type="loose"
        )
        self.wheat_b = Product(
            org_id=self.org_b.id, name="Wheat", stock=80.0, price=40.0, product_type="loose"
        )
        self.db.add_all([self.rice_a, self.wheat_b])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self._tmpdir.cleanup()

    def test_schema_includes_weigh_tickets(self):
        from sqlalchemy import inspect

        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("weigh_tickets", tables)

    def test_schema_init_rerun_preserves_ticket(self):
        from database.db import Base
        from database.schema_init import (
            init_database_schema,
            reset_schema_init_state_for_tests,
        )
        from backend.weigh_tickets import create_weigh_ticket
        from models.db_models import WeighTicket

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.5,
            created_by_user_id=self.user_a.id,
        )
        token = ticket.public_token
        reset_schema_init_state_for_tests()
        init_database_schema(force=True, bind=self.engine, metadata=Base.metadata)
        again = (
            self.db.query(WeighTicket)
            .filter(WeighTicket.public_token == token)
            .first()
        )
        self.assertIsNotNone(again)
        self.assertEqual(again.status, "active")

    def test_create_ticket_uses_server_price_and_unique_token(self):
        from backend.weigh_tickets import TOKEN_PREFIX, create_weigh_ticket, generate_public_token

        stock_before = float(self.rice_a.stock)
        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=2.5,
            created_by_user_id=self.user_a.id,
        )
        self.assertTrue(ticket.public_token.startswith(TOKEN_PREFIX))
        self.assertGreaterEqual(len(ticket.public_token), len(TOKEN_PREFIX) + 20)
        self.assertEqual(ticket.status, "active")
        self.assertEqual(ticket.unit_price_snapshot, 50.0)
        self.assertEqual(ticket.total_amount_snapshot, 125.0)
        self.assertEqual(ticket.product_name_snapshot, "Rice")
        self.assertEqual(ticket.weight, 2.5)
        self.assertEqual(ticket.unit, "kg")

        self.db.refresh(self.rice_a)
        self.assertEqual(float(self.rice_a.stock), stock_before)

        tokens = {generate_public_token() for _ in range(40)}
        self.assertEqual(len(tokens), 40)

    def test_create_rejects_client_price_tampering_by_ignoring_it(self):
        """create_weigh_ticket has no price argument — authoritative product.price only."""
        from backend.weigh_tickets import create_weigh_ticket

        self.rice_a.price = 60.0
        self.db.commit()
        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
        )
        self.assertEqual(ticket.unit_price_snapshot, 60.0)
        self.assertEqual(ticket.total_amount_snapshot, 60.0)

    def test_org_isolation_on_resolve(self):
        from backend.weigh_tickets import WeighTicketError, create_weigh_ticket, resolve_weigh_ticket

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
        )
        with self.assertRaises(WeighTicketError) as ctx:
            resolve_weigh_ticket(
                self.db, org_id=self.org_b.id, public_token=ticket.public_token
            )
        self.assertEqual(ctx.exception.code, "not_found")

    def test_resolve_active_ok_without_stock_change(self):
        from backend.weigh_tickets import create_weigh_ticket, resolve_weigh_ticket

        stock_before = float(self.rice_a.stock)
        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=3.0,
        )
        resolved = resolve_weigh_ticket(
            self.db, org_id=self.org_a.id, public_token=ticket.public_token
        )
        self.assertEqual(resolved.ticket.id, ticket.id)
        self.assertEqual(resolved.product.id, self.rice_a.id)
        self.db.refresh(self.rice_a)
        self.assertEqual(float(self.rice_a.stock), stock_before)
        self.assertEqual(resolved.ticket.status, "active")

    def test_consumed_ticket_rejected_as_already_purchased(self):
        from backend.weigh_tickets import (
            WeighTicketError,
            claim_active_ticket_for_checkout,
            create_weigh_ticket,
            resolve_weigh_ticket,
        )

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
        )
        claim_active_ticket_for_checkout(
            self.db,
            org_id=self.org_a.id,
            public_token=ticket.public_token,
            transaction_id=None,
        )
        self.db.commit()

        with self.assertRaises(WeighTicketError) as ctx:
            resolve_weigh_ticket(
                self.db, org_id=self.org_a.id, public_token=ticket.public_token
            )
        self.assertEqual(ctx.exception.code, "already_purchased")

        with self.assertRaises(WeighTicketError) as ctx2:
            claim_active_ticket_for_checkout(
                self.db,
                org_id=self.org_a.id,
                public_token=ticket.public_token,
            )
        self.assertEqual(ctx2.exception.code, "already_purchased")

    def test_cancelled_ticket_rejected(self):
        from backend.weigh_tickets import (
            WeighTicketError,
            cancel_weigh_ticket,
            create_weigh_ticket,
            resolve_weigh_ticket,
        )

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
        )
        cancel_weigh_ticket(
            self.db, org_id=self.org_a.id, ticket_id=ticket.id, reason="demo void"
        )
        with self.assertRaises(WeighTicketError) as ctx:
            resolve_weigh_ticket(
                self.db, org_id=self.org_a.id, public_token=ticket.public_token
            )
        self.assertEqual(ctx.exception.code, "cancelled")

    def test_expired_ticket_rejected(self):
        from backend.weigh_tickets import (
            WeighTicketError,
            create_weigh_ticket,
            resolve_weigh_ticket,
        )

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
            expires_at=datetime.utcnow() - timedelta(minutes=1),
        )
        with self.assertRaises(WeighTicketError) as ctx:
            resolve_weigh_ticket(
                self.db, org_id=self.org_a.id, public_token=ticket.public_token
            )
        self.assertEqual(ctx.exception.code, "cancelled")
        self.db.refresh(ticket)
        self.assertEqual(ticket.status, "cancelled")
        self.assertEqual(ticket.cancel_reason, "timeout_12h")
        self.assertIsNotNone(ticket.cancelled_at)

    def test_double_claim_is_atomic(self):
        from backend.weigh_tickets import (
            WeighTicketError,
            claim_active_ticket_for_checkout,
            create_weigh_ticket,
        )

        ticket = create_weigh_ticket(
            self.db,
            org_id=self.org_a.id,
            product_id=self.rice_a.id,
            weight=1.0,
        )
        claim_active_ticket_for_checkout(
            self.db, org_id=self.org_a.id, public_token=ticket.public_token
        )
        self.db.commit()
        with self.assertRaises(WeighTicketError) as ctx:
            claim_active_ticket_for_checkout(
                self.db, org_id=self.org_a.id, public_token=ticket.public_token
            )
        self.assertEqual(ctx.exception.code, "already_purchased")

    def test_create_wrong_org_product(self):
        from backend.weigh_tickets import WeighTicketError, create_weigh_ticket

        with self.assertRaises(WeighTicketError) as ctx:
            create_weigh_ticket(
                self.db,
                org_id=self.org_a.id,
                product_id=self.wheat_b.id,
                weight=1.0,
            )
        self.assertEqual(ctx.exception.code, "product_not_found")


if __name__ == "__main__":
    unittest.main()
