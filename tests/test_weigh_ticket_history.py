"""QR ticket history, 12h timeout, verified cancel-by-token, stock integrity."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WeighTicketHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        db = Path(cls._tmpdir.name) / "weigh_history.db"
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
            org_a = Organization(name="Hist Org A")
            org_b = Organization(name="Hist Org B")
            db_sess.add_all([org_a, org_b])
            db_sess.flush()
            user_a = User(
                org_id=org_a.id,
                full_name="Hist A",
                email="hist.a@example.com",
                password_hash=hash_password("securePass9"),
            )
            user_b = User(
                org_id=org_b.id,
                full_name="Hist B",
                email="hist.b@example.com",
                password_hash=hash_password("securePass9"),
            )
            db_sess.add_all([user_a, user_b])
            db_sess.flush()
            rice = Product(org_id=org_a.id, name="Rice", stock=100.0, price=50.0, product_type="loose")
            wheat = Product(org_id=org_b.id, name="Wheat", stock=80.0, price=40.0, product_type="loose")
            db_sess.add_all([rice, wheat])
            db_sess.commit()
            cls.org_a_id = org_a.id
            cls.org_b_id = org_b.id
            cls.user_a_id = user_a.id
            cls.user_b_id = user_b.id
            cls.rice_id = rice.id
            cls.wheat_id = wheat.id
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

    def _login(self, email: str):
        from backend.auth import create_session_token
        from database.db import SessionLocal
        from models.db_models import User

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            token = create_session_token(user.id, user.org_id, remember=False)
        finally:
            db.close()
        self.client.cookies.set(self.SESSION_COOKIE, token)

    def _stock(self, product_id: int) -> float:
        from database.db import SessionLocal
        from models.db_models import Product

        db = SessionLocal()
        try:
            p = db.query(Product).filter(Product.id == product_id).first()
            return float(p.stock or 0)
        finally:
            db.close()

    def _create_domain(self, *, org_id, product_id, weight=1.0, expires_at=None, user_id=None):
        from database.db import SessionLocal
        from backend.weigh_tickets import create_weigh_ticket

        db = SessionLocal()
        try:
            t = create_weigh_ticket(
                db,
                org_id=org_id,
                product_id=product_id,
                weight=weight,
                created_by_user_id=user_id,
                expires_at=expires_at,
                commit=True,
            )
            return t.id, t.public_token
        finally:
            db.close()

    def test_new_ticket_expires_about_12h(self):
        from database.db import SessionLocal
        from models.db_models import WeighTicket

        tid, _ = self._create_domain(
            org_id=self.org_a_id, product_id=self.rice_id, user_id=self.user_a_id
        )
        db = SessionLocal()
        try:
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            self.assertIsNotNone(t.expires_at)
            delta = t.expires_at - t.created_at
            self.assertAlmostEqual(delta.total_seconds(), 12 * 3600, delta=5)
        finally:
            db.close()

    def test_active_before_deadline(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import resolve_weigh_ticket, cancel_timed_out_tickets
        from models.db_models import WeighTicket

        tid, token = self._create_domain(
            org_id=self.org_a_id, product_id=self.rice_id, user_id=self.user_a_id
        )
        db = SessionLocal()
        try:
            cancel_timed_out_tickets(db, commit=True)
            resolved = resolve_weigh_ticket(db, org_id=self.org_a_id, public_token=token)
            self.assertEqual(resolved.ticket.status, "active")
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            self.assertEqual(t.status, "active")
        finally:
            db.close()

    def test_timeout_sweep_cancels_without_stock_or_txn(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import cancel_timed_out_tickets
        from models.db_models import WeighTicket, Transaction

        stock_before = self._stock(self.rice_id)
        tid, _ = self._create_domain(
            org_id=self.org_a_id,
            product_id=self.rice_id,
            expires_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db = SessionLocal()
        try:
            txn_before = db.query(Transaction).filter(Transaction.org_id == self.org_a_id).count()
            n = cancel_timed_out_tickets(db, commit=True)
            self.assertGreaterEqual(n, 1)
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            self.assertEqual(t.status, "cancelled")
            self.assertEqual(t.cancel_reason, "timeout_12h")
            self.assertIsNotNone(t.cancelled_at)
            self.assertIsNone(t.transaction_id)
            txn_after = db.query(Transaction).filter(Transaction.org_id == self.org_a_id).count()
            self.assertEqual(txn_before, txn_after)
        finally:
            db.close()
        self.assertEqual(self._stock(self.rice_id), stock_before)

    def test_lazy_expire_on_resolve(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import resolve_weigh_ticket, WeighTicketError
        from models.db_models import WeighTicket

        tid, token = self._create_domain(
            org_id=self.org_a_id,
            product_id=self.rice_id,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db = SessionLocal()
        try:
            with self.assertRaises(WeighTicketError) as ctx:
                resolve_weigh_ticket(db, org_id=self.org_a_id, public_token=token)
            self.assertEqual(ctx.exception.code, "cancelled")
            db.commit()
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            self.assertEqual(t.status, "cancelled")
            self.assertEqual(t.cancel_reason, "timeout_12h")
        finally:
            db.close()

    def test_redeemed_not_auto_cancelled_after_deadline(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import (
            claim_active_ticket_for_checkout,
            cancel_timed_out_tickets,
            maybe_expire_ticket,
        )
        from models.db_models import WeighTicket

        tid, token = self._create_domain(
            org_id=self.org_a_id, product_id=self.rice_id, weight=0.5
        )
        db = SessionLocal()
        try:
            claim_active_ticket_for_checkout(
                db, org_id=self.org_a_id, public_token=token, transaction_id=None
            )
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            t.expires_at = datetime.utcnow() - timedelta(hours=2)
            db.commit()
            cancel_timed_out_tickets(db, commit=True)
            maybe_expire_ticket(db, t)
            db.refresh(t)
            self.assertEqual(t.status, "consumed")
            self.assertNotEqual(t.cancel_reason, "timeout_12h")
        finally:
            db.close()

    def test_verified_cancel_by_token(self):
        stock_before = self._stock(self.rice_id)
        _, token = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        self._login("hist.a@example.com")
        res = self.client.post("/api/weigh-tickets/cancel-by-token", json={"token": token})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data["ticket"]["status"], "cancelled")
        self.assertEqual(data["ticket"]["cancel_reason"], "verified_manual_cancel")
        self.assertIsNotNone(data["ticket"]["cancelled_at"])
        self.assertNotIn("public_token", data["ticket"])
        self.assertEqual(self._stock(self.rice_id), stock_before)

    def test_cancel_rejects_unknown_other_org_redeemed_cancelled_timed_out(self):
        self._login("hist.a@example.com")

        bad = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": "AICA1.notrealtoken"}
        )
        self.assertEqual(bad.status_code, 404)

        _, other_token = self._create_domain(org_id=self.org_b_id, product_id=self.wheat_id)
        cross = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": other_token}
        )
        self.assertEqual(cross.status_code, 404)

        from database.db import SessionLocal
        from backend.weigh_tickets import claim_active_ticket_for_checkout

        _, redeemed_token = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        db = SessionLocal()
        try:
            claim_active_ticket_for_checkout(
                db, org_id=self.org_a_id, public_token=redeemed_token
            )
            db.commit()
        finally:
            db.close()
        red = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": redeemed_token}
        )
        self.assertEqual(red.status_code, 409)
        self.assertEqual(red.json().get("code"), "already_purchased")

        _, cancel_token = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        first = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": cancel_token}
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": cancel_token}
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json().get("code"), "cancelled")

        _, timed_token = self._create_domain(
            org_id=self.org_a_id,
            product_id=self.rice_id,
            expires_at=datetime.utcnow() - timedelta(minutes=1),
        )
        timed = self.client.post(
            "/api/weigh-tickets/cancel-by-token", json={"token": timed_token}
        )
        self.assertEqual(timed.status_code, 409)
        self.assertEqual(timed.json().get("code"), "cancelled")

    def test_id_cancel_endpoint_blocked(self):
        tid, _ = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        self._login("hist.a@example.com")
        res = self.client.post(f"/api/weigh-tickets/{tid}/cancel")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json().get("code"), "cancel_requires_token")

        from database.db import SessionLocal
        from models.db_models import WeighTicket

        db = SessionLocal()
        try:
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            self.assertEqual(t.status, "active")
        finally:
            db.close()

    def test_cancelled_cannot_resolve_or_checkout(self):
        _, token = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        self._login("hist.a@example.com")
        self.client.post("/api/weigh-tickets/cancel-by-token", json={"token": token})
        r = self.client.post("/api/weigh-tickets/resolve", json={"public_token": token})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json().get("code"), "cancelled")

        checkout = self.client.post(
            "/add_multiple",
            json=[
                {
                    "product": "Rice",
                    "price": 50,
                    "quantity": 1.0,
                    "weigh_ticket_token": token,
                }
            ],
        )
        self.assertGreaterEqual(checkout.status_code, 400)
        body = checkout.json()
        code = body.get("code") or ""
        err = str(body.get("error") or "") + str(body.get("detail") or "")
        self.assertTrue(
            code == "cancelled" or "cancel" in err.lower(),
            msg=str(body),
        )

    def test_history_org_isolation_and_filters(self):
        self._create_domain(org_id=self.org_a_id, product_id=self.rice_id, weight=1.1)
        self._create_domain(org_id=self.org_b_id, product_id=self.wheat_id, weight=2.2)
        _, cancel_tok = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        self._login("hist.a@example.com")
        self.client.post("/api/weigh-tickets/cancel-by-token", json={"token": cancel_tok})

        all_a = self.client.get("/api/weigh-tickets?status=all&limit=50")
        self.assertEqual(all_a.status_code, 200)
        payload = all_a.json()
        self.assertTrue(payload.get("ok"))
        tickets = payload["tickets"]
        self.assertTrue(tickets)
        for t in tickets:
            self.assertEqual(t["org_id"], self.org_a_id)
            self.assertNotIn("public_token", t)
            self.assertIn("token_ref", t)
            self.assertIn("status_label", t)

        names = {t["product_name_snapshot"] for t in tickets}
        self.assertIn("Rice", names)
        self.assertNotIn("Wheat", names)

        cancelled = self.client.get("/api/weigh-tickets?status=cancelled").json()
        self.assertTrue(all(x["status"] in ("cancelled", "expired") for x in cancelled["tickets"]))
        active = self.client.get("/api/weigh-tickets?status=active").json()
        self.assertTrue(all(x["status"] in ("active", "reserved") for x in active["tickets"]))

        self._login("hist.b@example.com")
        b_list = self.client.get("/api/weigh-tickets?status=all").json()
        for t in b_list["tickets"]:
            self.assertEqual(t["org_id"], self.org_b_id)
            self.assertNotEqual(t.get("product_name_snapshot"), "Rice")

    def test_ui_markers_weigh_and_pos(self):
        self._login("hist.a@example.com")
        weigh = self.client.get("/weigh")
        self.assertEqual(weigh.status_code, 200)
        html = weigh.text
        self.assertIn("QR History", html)
        self.assertIn("weighHistoryFilter", html)
        self.assertIn("cancel-by-token", html)
        self.assertIn("weighCancelScanBtn", html)
        self.assertIn("/camera/video_feed", html)
        self.assertIn("/camera/detections", html)
        self.assertIn("/camera/scan_purpose", html)
        self.assertIn("weighCancelFeed", html)
        self.assertIn("OpenCV", html)
        self.assertNotIn("new BarcodeDetector", html)
        self.assertNotIn("getUserMedia", html)

        pos = self.client.get("/pos")
        self.assertEqual(pos.status_code, 200)
        self.assertIn("data-pos-panel=\"qr-status\"", pos.text)
        self.assertIn("posQrStatusFilter", pos.text)
        self.assertIn("scan_purpose", pos.text)

    def test_opencv_decode_endpoint_and_scan_purpose(self):
        """Desktop-safe path: OpenCV QRCodeDetector via /camera/decode + purpose lock."""
        import cv2
        from backend.weigh_label import qr_png_bytes
        from backend.weigh_tickets import generate_public_token
        import backend.routes as routes

        self._login("hist.a@example.com")
        token = generate_public_token()
        png = qr_png_bytes(token, box_size=8, border=2)

        purpose = self.client.post("/camera/scan_purpose", data={"purpose": "cancel"})
        self.assertEqual(purpose.status_code, 200, purpose.text[:300])
        self.assertEqual(purpose.json().get("scan_purpose"), "cancel")
        det = self.client.get("/camera/detections")
        self.assertEqual(det.status_code, 200)
        self.assertEqual(det.json().get("scan_purpose"), "cancel")
        self.assertIsNone(det.json().get("qr_event"))

        decoded = self.client.post(
            "/camera/decode",
            files={"frame": ("qr.png", png, "image/png")},
        )
        self.assertEqual(decoded.status_code, 200, decoded.text[:400])
        body = decoded.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("token"), token)

        # Restore checkout purpose for POS
        back = self.client.post("/camera/scan_purpose", data={"purpose": "checkout"})
        self.assertEqual(back.json().get("scan_purpose"), "checkout")

        if routes.streamer is not None:
            # Ensure OpenCV detector is what decoded (same component as live stream)
            self.assertIsNotNone(routes.streamer._ensure_qr_detector())
            self.assertIsInstance(routes.streamer._ensure_qr_detector(), type(cv2.QRCodeDetector()))

    def test_race_timeout_does_not_overwrite_consumed(self):
        from database.db import SessionLocal
        from backend.weigh_tickets import (
            claim_active_ticket_for_checkout,
            cancel_timed_out_tickets,
            cancel_weigh_ticket_by_token,
            WeighTicketError,
        )
        from models.db_models import WeighTicket

        tid, token = self._create_domain(org_id=self.org_a_id, product_id=self.rice_id)
        db = SessionLocal()
        try:
            claim_active_ticket_for_checkout(db, org_id=self.org_a_id, public_token=token)
            t = db.query(WeighTicket).filter(WeighTicket.id == tid).first()
            t.expires_at = datetime.utcnow() - timedelta(hours=1)
            db.commit()
            cancel_timed_out_tickets(db, commit=True)
            db.refresh(t)
            self.assertEqual(t.status, "consumed")
            with self.assertRaises(WeighTicketError) as ctx:
                cancel_weigh_ticket_by_token(db, org_id=self.org_a_id, public_token=token)
            self.assertEqual(ctx.exception.code, "already_purchased")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
