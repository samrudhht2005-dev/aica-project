"""
SQLite portability experiment — isolated process.

Must be run as its own interpreter so DATABASE_URL is bound before
database.db creates the engine:

    python tests/test_sqlite_experiment.py

Uses database/_experiment/aica_experiment.sqlite only.
Does not touch PostgreSQL data or the packaged AICA 1.0.1 install.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXP_DIR = ROOT / "database" / "_experiment"
EXP_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_PATH = EXP_DIR / "aica_experiment.sqlite"
for extra in (SQLITE_PATH, Path(str(SQLITE_PATH) + "-wal"), Path(str(SQLITE_PATH) + "-shm")):
    if extra.exists():
        extra.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + SQLITE_PATH.resolve().as_posix()
os.environ["AICA_DB_BACKEND"] = "sqlite"
os.environ["AICA_SQLITE_PATH"] = str(SQLITE_PATH.resolve())


def _signup(client, suffix: str, org_name: str, email: str):
    password = "securePass9"
    r = client.post("/signup", data={
        "org_name": org_name,
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
        "full_name": "SQLite Admin",
        "email": email,
        "password": password,
        "confirm_password": password,
    }, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return password


def _product_named(client, name: str):
    products = client.get("/api/products").json()
    return next((p for p in products if p["name"].lower() == name.lower()), None)


def main():
    t0 = time.perf_counter()
    findings = []

    import backend.main as mainmod
    from database.db import DATABASE_URL, engine

    assert engine.dialect.name == "sqlite", engine.dialect.name
    assert str(DATABASE_URL).startswith("sqlite:"), DATABASE_URL
    print("ENGINE", DATABASE_URL)

    with patch.object(mainmod, "init_camera"), patch.object(
        mainmod.routes, "update_ai_insights_and_recommendations_bg"
    ):
        from fastapi.testclient import TestClient

        # --- init ---
        t_start = time.perf_counter()
        client = TestClient(mainmod.app)
        health = client.get("/health")
        assert health.status_code == 200
        startup_ms = (time.perf_counter() - t_start) * 1000
        print(f"STARTUP_MS {startup_ms:.1f}")

        con = sqlite3.connect(str(SQLITE_PATH))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {
            "users", "organizations", "products", "transactions", "expenses",
            "employees", "assets", "compliance_obligations", "tax_recommendations", "anomalies",
        }
        missing = required - tables
        assert not missing, missing
        fk_on = con.execute("PRAGMA foreign_keys").fetchone()[0]
        # Direct sqlite3 connection may not inherit SQLAlchemy pragmas; ORM path is what matters.
        con.close()
        print("TABLES", sorted(required))
        findings.append(f"sqlite3 PRAGMA foreign_keys on raw connection={fk_on} (ORM uses SQLAlchemy connect hook)")

        suffix = uuid.uuid4().hex[:8]
        email_a = f"sqlite_a_{suffix}@example.com"
        email_b = f"sqlite_b_{suffix}@example.com"
        _signup(client, suffix, f"SQLite Mart {suffix}", email_a)

        login = client.post("/login", data={
            "email": email_a,
            "password": "securePass9",
            "remember": "on",
        }, follow_redirects=False)
        assert login.status_code in (303, 200)

        # --- products ---
        t_prod = time.perf_counter()
        assert client.post("/add_product", data={"name": "Maggi", "price": "15", "stock": "20"}, follow_redirects=False).status_code == 303
        assert client.post("/add_product", data={"name": "Lays", "price": "10", "stock": "12"}, follow_redirects=False).status_code == 303
        assert client.post("/add_product", data={"name": "Milk", "price": "28", "stock": "8"}, follow_redirects=False).status_code == 303
        # edit via add_product (existing name updates price/stock)
        assert client.post("/add_product", data={"name": "Milk", "price": "30", "stock": "2"}, follow_redirects=False).status_code == 303
        milk = _product_named(client, "Milk")
        assert milk and abs(milk["price"] - 30) < 0.01
        assert abs(milk["stock"] - 10) < 0.01  # 8 + 2
        extra = client.post("/add_product", data={"name": "TempDelete", "price": "5", "stock": "1"}, follow_redirects=False)
        assert extra.status_code == 303
        warehouse = client.get("/warehouse")
        assert warehouse.status_code == 200
        # delete TempDelete
        from database.db import SessionLocal
        from models.db_models import Product, Organization, User
        db = SessionLocal()
        org = db.query(Organization).order_by(Organization.id.asc()).first()
        temp = db.query(Product).filter(Product.org_id == org.id, Product.name == "Tempdelete").first()
        if temp is None:
            temp = db.query(Product).filter(Product.org_id == org.id, Product.name == "TempDelete").first()
        assert temp is not None, [p.name for p in db.query(Product).all()]
        temp_id = temp.id
        db.close()
        assert client.post(f"/delete_product/{temp_id}", follow_redirects=False).status_code == 303
        assert _product_named(client, "TempDelete") is None
        prod_ms = (time.perf_counter() - t_prod) * 1000
        print(f"PRODUCT_CRUD_MS {prod_ms:.1f}")

        # --- POS + GST ---
        maggi_gst = mainmod.routes.classify_gst("Maggi")
        assert maggi_gst == 18
        t_sale = time.perf_counter()
        sale = client.post("/add_multiple", json=[
            {"product": "Maggi", "price": 15, "quantity": 2},
            {"product": "Lays", "price": 10, "quantity": 1},
        ])
        sale_ms = (time.perf_counter() - t_sale) * 1000
        assert sale.status_code == 200, sale.text[:400]
        assert sale.headers.get("content-type", "").startswith("application/pdf")
        assert sale.content[:4] == b"%PDF"
        print(f"SALE_MS {sale_ms:.1f}")

        maggi = _product_named(client, "Maggi")
        lays = _product_named(client, "Lays")
        assert abs(maggi["stock"] - 18) < 0.01, maggi  # 20-2
        assert abs(lays["stock"] - 11) < 0.01, lays  # 12-1

        # Maggi 15*2=30 @18% => 5.40; Lays 10 @18% => 1.80; total tax 7.20; grand 47.20
        expected_sub = 40.0
        expected_tax = round(30 * 0.18 + 10 * 0.18, 2)
        expected_total = round(expected_sub + expected_tax, 2)
        assert abs(expected_tax - 7.20) < 0.001
        assert abs(expected_total - 47.20) < 0.001

        t_hist = time.perf_counter()
        hist = client.get("/api/pos/history").json()
        hist_ms = (time.perf_counter() - t_hist) * 1000
        assert hist["items"], hist
        row = hist["items"][0]
        assert abs(row["grand_total"] - expected_total) < 0.02, row
        assert abs(row["total_tax"] - expected_tax) < 0.02, row
        tx_id = row["id"]
        print(f"HISTORY_MS {hist_ms:.1f} invoice={row['invoice_number']}")

        t_inv = time.perf_counter()
        pdf = client.get(f"/download_invoice/{tx_id}", headers={"Accept": "application/pdf, application/json"})
        inv_ms = (time.perf_counter() - t_inv) * 1000
        assert pdf.status_code == 200
        assert pdf.content[:4] == b"%PDF"
        print(f"INVOICE_MS {inv_ms:.1f}")

        t_intel = time.perf_counter()
        intel = client.get("/api/pos/intelligence").json()
        intel_ms = (time.perf_counter() - t_intel) * 1000
        assert intel.get("empty") is False
        assert intel["today"]["transactions"] >= 1
        names = [p["name"].lower() for p in (intel.get("top_selling") or [])]
        assert "maggi" in names or "lays" in names
        print(f"INTEL_MS {intel_ms:.1f}")

        # date filter
        filtered = client.get("/api/pos/history", params={"date_from": "2099-01-01", "date_to": "2099-01-02"}).json()
        assert filtered["total"] == 0

        # more sales for analytics
        client.post("/add_multiple", json=[{"product": "Milk", "price": 30, "quantity": 1}])
        milk_after = _product_named(client, "Milk")
        assert abs(milk_after["stock"] - 9) < 0.01, milk_after

        # pages
        for path in ("/", "/pos", "/sales", "/expenses", "/gst", "/tax-optimization", "/warehouse"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)

        # expenses GST
        intra = client.get("/api/expenses/tax-preview", params={
            "category": "Rent", "amount": 10000, "supplier_state": "Karnataka"
        }).json()
        assert intra["ok"] is True
        assert abs(intra["cgst"] - 900) < 0.01
        create_exp = client.post("/api/expenses/create", data={
            "vendor": "Office Landlord",
            "invoice_number": "R-SQL-1",
            "category": "Rent",
            "subcategory": "HQ",
            "amount": "10000",
            "supplier_state": "Karnataka",
            "payment_method": "Bank Transfer",
            "is_business": "true",
            "classification": "Revenue",
            "description": "Rent",
        }, follow_redirects=False)
        assert create_exp.status_code == 303, create_exp.text[:300]

        # org isolation
        client_b = TestClient(mainmod.app)
        _signup(client_b, suffix, f"Other Org {suffix}", email_b)
        assert client_b.post("/add_product", data={"name": "Maggi", "price": "99", "stock": "3"}, follow_redirects=False).status_code == 303
        b_maggi = _product_named(client_b, "Maggi")
        a_maggi = _product_named(client, "Maggi")
        assert abs(b_maggi["price"] - 99) < 0.01
        assert abs(a_maggi["price"] - 15) < 0.01
        assert abs(a_maggi["stock"] - 18) < 0.01

        # concurrency: parallel reads
        def _read(_i):
            return client.get("/api/pos/intelligence").status_code

        t_conc = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(_read, range(12)))
        conc_ms = (time.perf_counter() - t_conc) * 1000
        assert codes == [200] * 12, codes
        print(f"CONCURRENT_READ_MS {conc_ms:.1f}")

        # concurrent writes of distinct products
        client.post("/add_product", data={"name": "Sugar", "price": "40", "stock": "30"}, follow_redirects=False)
        client.post("/add_product", data={"name": "Rice", "price": "60", "stock": "30"}, follow_redirects=False)
        locked = []

        def _sell(name):
            try:
                r = client.post("/add_multiple", json=[{"product": name, "price": 40 if name == "Sugar" else 60, "quantity": 1}])
                return name, r.status_code, r.headers.get("content-type", "")
            except Exception as e:
                locked.append(str(e))
                return name, 0, str(e)

        with ThreadPoolExecutor(max_workers=2) as pool:
            write_results = list(pool.map(_sell, ["Sugar", "Rice"]))
        print("CONCURRENT_WRITES", write_results, "errors", locked)
        ok_writes = [r for r in write_results if r[1] == 200]
        if len(ok_writes) < 2:
            findings.append(f"concurrent writes not both 200: {write_results} {locked}")
        else:
            findings.append("concurrent writes of two distinct products succeeded")

        sugar = _product_named(client, "Sugar")
        rice = _product_named(client, "Rice")
        if sugar:
            findings.append(f"sugar stock after concurrent sell={sugar['stock']}")
        if rice:
            findings.append(f"rice stock after concurrent sell={rice['stock']}")

        # duplicate employee id
        emp1 = client.post("/api/employees/create", data={
            "employee_id": "E100",
            "name": "Ravi",
            "department": "Sales",
            "designation": "Associate",
            "salary": "20000",
            "basic_salary": "12000",
            "allowances": "3000",
            "bonuses": "0",
            "incentives": "0",
            "employer_contribution": "0",
            "benefits": "0",
            "joining_date": "2026-04-01",
        }, follow_redirects=False)
        # endpoint may be different; don't fail experiment on missing payroll form
        findings.append(f"employee create status={emp1.status_code}")

        from sqlalchemy.exc import IntegrityError
        fk_db = SessionLocal()
        try:
            fk_db.add(Product(org_id=999999, name="OrphanFK", price=1.0, stock=1.0))
            fk_db.commit()
            raise AssertionError("SQLite did not enforce product.org_id foreign key")
        except IntegrityError:
            fk_db.rollback()
            findings.append("ORM foreign_keys=ON rejected product with invalid org_id")
        finally:
            fk_db.close()

    # --- persistence after ORM session close: raw sqlite3 ---
    raw = sqlite3.connect(str(SQLITE_PATH))
    raw.execute("PRAGMA foreign_keys=ON")
    users = raw.execute("SELECT count(*) FROM users").fetchone()[0]
    orgs = raw.execute("SELECT count(*) FROM organizations").fetchone()[0]
    txs = raw.execute("SELECT count(*) FROM transactions").fetchone()[0]
    products = raw.execute("SELECT count(*) FROM products").fetchone()[0]
    expenses = raw.execute("SELECT count(*) FROM expenses").fetchone()[0]
    assert users >= 2, users
    assert orgs >= 2, orgs
    assert txs >= 1, txs
    assert products >= 3, products
    assert expenses >= 1, expenses
    money = raw.execute("SELECT total_amount, gst_amount FROM transactions ORDER BY id ASC LIMIT 1").fetchone()
    assert money is not None
    assert abs(float(money[0]) - 47.20) < 0.02, money
    assert abs(float(money[1]) - 7.20) < 0.02, money
    raw.close()
    print("PERSISTENCE users", users, "orgs", orgs, "txs", txs, "products", products, "expenses", expenses)

    total_s = time.perf_counter() - t0
    print("FINDINGS")
    for line in findings:
        print(" -", line)
    print(f"SQLITE_EXPERIMENT_OK seconds={total_s:.1f}")
    print("SQLITE_PATH", SQLITE_PATH)


if __name__ == "__main__":
    main()
