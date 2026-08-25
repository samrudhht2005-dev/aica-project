"""Weigh UI / label endpoints: auth, QR payload, PDF, no stock deduction."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WeighUiLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        db = Path(cls._tmpdir.name) / "weigh_ui.db"
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
        from backend.auth import hash_password, create_session_token, SESSION_COOKIE
        from fastapi.testclient import TestClient
        import backend.main as mainmod

        db_sess = SessionLocal()
        try:
            org_a = Organization(name="Weigh Org A")
            org_b = Organization(name="Weigh Org B")
            db_sess.add_all([org_a, org_b])
            db_sess.flush()
            user_a = User(
                org_id=org_a.id,
                full_name="Weigh User A",
                email="weigh.a@example.com",
                password_hash=hash_password("securePass9"),
            )
            user_b = User(
                org_id=org_b.id,
                full_name="Weigh User B",
                email="weigh.b@example.com",
                password_hash=hash_password("securePass9"),
            )
            db_sess.add_all([user_a, user_b])
            db_sess.flush()
            rice = Product(org_id=org_a.id, name="Rice", stock=100.0, price=50.0, product_type="loose")
            db_sess.add(rice)
            db_sess.commit()
            cls.org_a_id = org_a.id
            cls.org_b_id = org_b.id
            cls.user_a_id = user_a.id
            cls.user_b_id = user_b.id
            cls.rice_id = rice.id
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

        token = create_session_token(uid, oid, remember=False)
        self.client.cookies.set(self.SESSION_COOKIE, token)

    def test_weigh_page_requires_auth(self):
        self.client.cookies.clear()
        res = self.client.get("/weigh", follow_redirects=False)
        self.assertIn(res.status_code, (302, 303, 401))

    def test_weigh_page_ok_for_org_user(self):
        self._auth(self.user_a_id, self.org_a_id)
        res = self.client.get("/weigh")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Generate QR", res.content)
        self.assertIn(b"QR History", res.content)
        self.assertIn(b"Rice", res.content)
        # Top-level Weigh workspace chrome (not Organization sidebar)
        self.assertIn(b'data-sidebar-mode="weigh"', res.content)
        self.assertIn(b"AICA Weigh", res.content)
        self.assertIn(b"Switch to POS", res.content)
        self.assertIn(b"Switch to Organization", res.content)
        self.assertEqual(res.cookies.get("aica_ui_mode"), "weigh")

    def test_org_sidebar_does_not_nest_weigh_nav(self):
        """Weigh must not appear as an Organization subsection nav item."""
        self._auth(self.user_a_id, self.org_a_id)
        res = self.client.get("/warehouse")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'data-sidebar-mode="org"', res.content)
        self.assertNotIn(b'href="/weigh"', res.content)
        self.assertIn(b'name="target" value="weigh"', res.content)

    def test_select_and_switch_weigh_workspace(self):
        from backend.auth import UI_MODE_COOKIE

        self._auth(self.user_a_id, self.org_a_id)
        sel = self.client.get("/select-interface")
        self.assertEqual(sel.status_code, 200)
        # Landing cards share the same /switch-interface + target= wiring as sidebars
        self.assertIn(b'action="/switch-interface"', sel.content)
        self.assertIn(b'name="target" value="weigh"', sel.content)
        self.assertIn(b'name="target" value="pos"', sel.content)
        self.assertIn(b'name="target" value="org"', sel.content)

        # Selecting Weigh from the landing page must enter /weigh with cookie set
        choose = self.client.post(
            "/switch-interface",
            data={"target": "weigh"},
            follow_redirects=False,
        )
        self.assertIn(choose.status_code, (302, 303))
        self.assertEqual(choose.headers.get("location"), "/weigh")
        self.assertEqual(choose.cookies.get(UI_MODE_COOKIE), "weigh")

        # Legacy POST /select-interface?mode= still works for compatibility
        legacy = self.client.post(
            "/select-interface",
            data={"mode": "weigh"},
            follow_redirects=False,
        )
        self.assertIn(legacy.status_code, (302, 303))
        self.assertEqual(legacy.headers.get("location"), "/weigh")
        self.assertEqual(legacy.cookies.get(UI_MODE_COOKIE), "weigh")

        to_pos = self.client.post(
            "/switch-interface",
            data={"target": "pos"},
            follow_redirects=False,
        )
        self.assertIn(to_pos.status_code, (302, 303))
        self.assertEqual(to_pos.headers.get("location"), "/pos")
        self.assertEqual(to_pos.cookies.get(UI_MODE_COOKIE), "pos")

        to_org = self.client.post(
            "/switch-interface",
            data={"target": "org"},
            follow_redirects=False,
        )
        self.assertIn(to_org.status_code, (302, 303))
        self.assertEqual(to_org.headers.get("location"), "/")
        self.assertEqual(to_org.cookies.get(UI_MODE_COOKIE), "org")

        to_weigh = self.client.post(
            "/switch-interface",
            data={"target": "weigh"},
            follow_redirects=False,
        )
        self.assertIn(to_weigh.status_code, (302, 303))
        self.assertEqual(to_weigh.headers.get("location"), "/weigh")
        self.assertEqual(to_weigh.cookies.get(UI_MODE_COOKIE), "weigh")

    def test_create_qr_pdf_flow_no_stock_change_active(self):
        from database.db import SessionLocal
        from models.db_models import Product, WeighTicket
        from backend.weigh_tickets import TOKEN_PREFIX

        self._auth(self.user_a_id, self.org_a_id)
        db = SessionLocal()
        try:
            stock_before = float(db.query(Product).get(self.rice_id).stock)
        finally:
            db.close()

        create = self.client.post(
            "/api/weigh-tickets",
            json={"product_id": self.rice_id, "weight": 2.5, "unit": "kg"},
        )
        self.assertEqual(create.status_code, 200, create.text)
        body = create.json()
        self.assertTrue(body.get("ok"))
        ticket = body["ticket"]
        self.assertEqual(ticket["status"], "active")
        self.assertEqual(ticket["weight"], 2.5)
        self.assertEqual(ticket["unit_price"], 50.0)
        self.assertEqual(ticket["total_amount"], 125.0)
        self.assertTrue(str(ticket["public_token"]).startswith(TOKEN_PREFIX))
        self.assertEqual(ticket["qr_payload"], ticket["public_token"])
        # QR must be opaque — not a structured price/weight payload
        self.assertNotIn("unit_price", ticket["public_token"])
        self.assertNotIn("weight=", ticket["public_token"])
        self.assertNotIn("Rice", ticket["public_token"])

        tid = ticket["id"]
        qr = self.client.get(f"/api/weigh-tickets/{tid}/qr.png")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.headers.get("content-type", ""), "image/png")
        self.assertGreater(len(qr.content), 100)
        self.assertTrue(qr.content.startswith(b"\x89PNG"))

        pdf = self.client.get(f"/api/weigh-tickets/{tid}/label.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers.get("content-type", ""), "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertGreater(len(pdf.content), 500)

        db = SessionLocal()
        try:
            product = db.query(Product).get(self.rice_id)
            self.assertEqual(float(product.stock), stock_before)
            row = db.query(WeighTicket).get(tid)
            self.assertEqual(row.status, "active")
            self.assertEqual(row.public_token, ticket["public_token"])
        finally:
            db.close()

    def test_label_endpoints_org_isolated(self):
        self._auth(self.user_a_id, self.org_a_id)
        create = self.client.post(
            "/api/weigh-tickets",
            json={"product_id": self.rice_id, "weight": 1.0, "unit": "kg"},
        )
        tid = create.json()["ticket"]["id"]

        self._auth(self.user_b_id, self.org_b_id)
        qr = self.client.get(f"/api/weigh-tickets/{tid}/qr.png")
        self.assertEqual(qr.status_code, 404)
        pdf = self.client.get(f"/api/weigh-tickets/{tid}/label.pdf")
        self.assertEqual(pdf.status_code, 404)

    def test_qr_png_bytes_payload_is_token_only(self):
        from backend.weigh_label import qr_png_bytes
        from backend.weigh_tickets import generate_public_token

        token = generate_public_token()
        png = qr_png_bytes(token)
        self.assertTrue(png.startswith(b"\x89PNG"))
        # Round-trip decode with OpenCV if available
        import numpy as np
        import cv2

        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        det = cv2.QRCodeDetector()
        val, _, _ = det.detectAndDecode(img)
        self.assertEqual(val, token)


if __name__ == "__main__":
    unittest.main()
