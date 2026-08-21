"""Auth + org isolation smoke test. Does not wipe existing tables."""
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient


def test_flow():
    import backend.main as mainmod
    with patch.object(mainmod, "init_camera"), patch.object(
        mainmod.routes, "update_ai_insights_and_recommendations_bg"
    ):
        client = TestClient(mainmod.app)

        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/login"), r.status_code

        suffix = uuid.uuid4().hex[:8]
        email_a = f"admin_a_{suffix}@example.com"
        email_b = f"admin_b_{suffix}@example.com"
        password = "securePass9"

        def signup(email, org_name):
            return client.post("/signup", data={
                "org_name": org_name,
                "business_type": "Private Ltd",
                "gst_registered": "false",
                "gstin": "",
                "pan": "AAACA1234B",
                "contact_number": "9999999999",
                "registered_address": "1 Test Street",
                "city": "Bengaluru",
                "state": "Karnataka",
                "pincode": "560001",
                "business_email": email,
                "full_name": "Admin User",
                "email": email,
                "password": password,
                "confirm_password": password,
            }, follow_redirects=False)

        ra = signup(email_a, f"Alpha Traders {suffix}")
        assert ra.status_code == 303, ra.text[:500]
        dash = client.get("/", follow_redirects=False)
        assert dash.status_code == 200
        assert "₹0.00" in dash.text or "0.00" in dash.text
        assert "No financial data available yet" in dash.text

        exp = client.post("/api/expenses/create", data={
            "vendor": "Test Vendor",
            "invoice_number": "INV-1",
            "category": "Rent",
            "subcategory": "Office",
            "amount": "10000",
            "cgst": "900",
            "sgst": "900",
            "igst": "0",
            "payment_method": "Bank Transfer",
            "is_business": "true",
            "classification": "Revenue",
            "description": "Office rent",
        }, follow_redirects=False)
        assert exp.status_code == 303, exp.text[:400]

        dash2 = client.get("/")
        assert "10,000" in dash2.text or "11800" in dash2.text or "11,800" in dash2.text

        client.get("/logout")
        rb = signup(email_b, f"Beta Mart {suffix}")
        assert rb.status_code == 303
        dash_b = client.get("/")
        assert "No financial data available yet" in dash_b.text
        assert "Test Vendor" not in dash_b.text

        client.get("/logout")
        login = client.post("/login", data={"email": email_a, "password": password, "remember": "true"}, follow_redirects=False)
        assert login.status_code == 303
        dash_a = client.get("/")
        assert "Test Vendor" in dash_a.text or "11,800" in dash_a.text or "11800" in dash_a.text

        unauth = TestClient(mainmod.app)
        home = unauth.get("/", follow_redirects=False)
        assert home.status_code == 303
        print("AUTH + ISOLATION TESTS PASSED")


if __name__ == "__main__":
    test_flow()
