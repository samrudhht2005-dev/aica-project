"""Quick POS checkout + stock validation smoke test."""
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
        email = f"pos_{suffix}@example.com"
        password = "securePass9"
        r = client.post("/signup", data={
            "org_name": f"POS Co {suffix}",
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
            "full_name": "POS Admin",
            "email": email,
            "password": password,
            "confirm_password": password,
        }, follow_redirects=False)
        assert r.status_code == 303, r.text[:400]

        # product not in warehouse
        miss = client.post("/add_multiple", json=[{"product": "Ghost Item", "price": 10, "quantity": 1}])
        assert miss.status_code == 400
        assert "not found" in miss.json()["error"].lower()

        add = client.post("/add_product", data={"name": "Maggi", "price": "15", "stock": "5"}, follow_redirects=False)
        assert add.status_code == 303, add.text[:300]

        low = client.post("/add_multiple", json=[{"product": "Maggi", "price": 15, "quantity": 9}])
        assert low.status_code == 400
        assert "Insufficient stock" in low.json()["error"]

        ok = client.post("/add_multiple", json=[{"product": "Maggi", "price": 15, "quantity": 2}])
        assert ok.status_code == 200, ok.text[:400]
        assert ok.headers.get("content-type", "").startswith("application/pdf")
        assert ok.content[:4] == b"%PDF"

        products = client.get("/api/products").json()
        maggi = next(p for p in products if p["name"].lower() == "maggi")
        assert abs(maggi["stock"] - 3) < 0.01, maggi

        # profile page + assistant endpoint exist
        assert client.get("/profile").status_code == 200
        with patch("backend.routes.query_gemini_assistant", return_value="Use the vendor field for the supplier name.\nNAV_REQUEST: {\"path\":\"/gst\",\"label\":\"GST & ITC\",\"reason\":\"ITC follows from this bill.\"}"):
            assist = client.post("/api/assistant", data={
                "question": "What should I enter for vendor on this page?",
                "page": "expenses",
                "path": "/expenses",
                "task": "",
                "history": "[]",
            })
            assert assist.status_code == 200
            data = assist.json()
            assert "answer" in data
            assert data.get("navigation") and data["navigation"]["path"] == "/gst"
        print("POS + PROFILE + ASSISTANT SMOKE PASSED")


if __name__ == "__main__":
    main()
