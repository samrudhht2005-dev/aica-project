"""Expense auto-GST smoke test."""
from unittest.mock import patch
import uuid
from fastapi.testclient import TestClient


def main():
    import backend.main as mainmod
    with patch.object(mainmod, "init_camera"), patch.object(
        mainmod.routes, "update_ai_insights_and_recommendations_bg"
    ):
        client = TestClient(mainmod.app)
        suffix = uuid.uuid4().hex[:8]
        email = f"gst_{suffix}@example.com"
        password = "securePass9"
        assert client.post("/signup", data={
            "org_name": f"GST Co {suffix}",
            "business_type": "Private Ltd",
            "gst_registered": "true",
            "gstin": "29AAACA1234A1Z5",
            "pan": "AAACA1234B",
            "contact_number": "9999999999",
            "registered_address": "1 Test Street",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560001",
            "business_email": email,
            "full_name": "GST Admin",
            "email": email,
            "password": password,
            "confirm_password": password,
        }, follow_redirects=False).status_code == 303

        # missing supplier state
        miss = client.get("/api/expenses/tax-preview", params={
            "category": "Rent", "amount": 10000, "supplier_state": ""
        }).json()
        assert miss["ok"] is False
        assert "supplier" in miss["error"].lower() or "state" in miss["error"].lower()

        intra = client.get("/api/expenses/tax-preview", params={
            "category": "Rent", "amount": 10000, "supplier_state": "Karnataka"
        }).json()
        assert intra["ok"] is True
        assert intra["rate"] == 18.0
        assert intra["split"] == "intra"
        assert abs(intra["cgst"] - 900) < 0.01
        assert abs(intra["sgst"] - 900) < 0.01
        assert intra["igst"] == 0

        inter = client.get("/api/expenses/tax-preview", params={
            "category": "Rent", "amount": 10000, "supplier_state": "Maharashtra"
        }).json()
        assert inter["split"] == "inter"
        assert abs(inter["igst"] - 1800) < 0.01
        assert inter["cgst"] == 0

        salary = client.get("/api/expenses/tax-preview", params={
            "category": "Salaries", "amount": 50000, "supplier_state": "Karnataka"
        }).json()
        assert salary["ok"] is True
        assert salary["total_tax"] == 0

        create = client.post("/api/expenses/create", data={
            "vendor": "Office Landlord",
            "invoice_number": "R-1",
            "category": "Rent",
            "subcategory": "HQ",
            "amount": "10000",
            "supplier_state": "Karnataka",
            "payment_method": "Bank Transfer",
            "is_business": "true",
            "classification": "Revenue",
            "description": "Rent",
        }, follow_redirects=False)
        assert create.status_code == 303, create.text[:300]

        # markdown pages should not contain literal **
        emp = client.get("/employees").text
        assert "**30%" not in emp
        assert "<strong>30% deduction</strong>" in emp or "30% deduction" in emp

        print("EXPENSE GST + MARKDOWN SMOKE PASSED")


if __name__ == "__main__":
    main()
