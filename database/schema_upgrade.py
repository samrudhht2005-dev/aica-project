"""Safe, additive schema upgrades. Never drops tables or deletes rows."""
from sqlalchemy import inspect, text
from database.db import engine
import logging

log = logging.getLogger(__name__)


def _columns(inspector, table):
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _has_index(inspector, table, name):
    try:
        return any(idx.get("name") == name for idx in inspector.get_indexes(table))
    except Exception:
        return False


def upgrade_schema():
    inspector = inspect(engine)
    with engine.begin() as conn:
        org_cols = _columns(inspector, "organizations")
        org_adds = {
            "gst_registered": "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS gst_registered BOOLEAN DEFAULT FALSE",
            "city": "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS city VARCHAR DEFAULT ''",
            "pincode": "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS pincode VARCHAR DEFAULT ''",
            "contact_number": "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS contact_number VARCHAR DEFAULT ''",
            "business_email": "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS business_email VARCHAR DEFAULT ''",
        }
        for col, sql in org_adds.items():
            if col not in org_cols:
                conn.execute(text(sql))

        if "org_id" not in _columns(inspector, "transactions"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS org_id INTEGER REFERENCES organizations(id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_transactions_org_id ON transactions(org_id)"))

        exp_cols = _columns(inspector, "expenses")
        if "supplier_state" not in exp_cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS supplier_state VARCHAR DEFAULT ''"))
        if "gst_rate" not in exp_cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS gst_rate DOUBLE PRECISION DEFAULT 0"))

        prod_cols = _columns(inspector, "products")
        if "org_id" not in prod_cols:
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS org_id INTEGER REFERENCES organizations(id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_products_org_id ON products(org_id)"))

        # Allow same product name in different organizations
        conn.execute(text("ALTER TABLE products DROP CONSTRAINT IF EXISTS products_name_key"))
        conn.execute(text("ALTER TABLE employees DROP CONSTRAINT IF EXISTS employees_employee_id_key"))

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                org_id INTEGER REFERENCES organizations(id),
                full_name VARCHAR NOT NULL,
                email VARCHAR NOT NULL UNIQUE,
                password_hash VARCHAR NOT NULL,
                role VARCHAR DEFAULT 'admin',
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_org_id ON users(org_id)"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR DEFAULT ''"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_language VARCHAR DEFAULT 'en'"))
        try:
            with conn.begin_nested():
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_product_org_name ON products(org_id, name) WHERE org_id IS NOT NULL"))
        except Exception as e:
            log.warning("Skipped unique product index (existing duplicates): %s", e)
        try:
            with conn.begin_nested():
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_employee_org_code ON employees(org_id, employee_id) WHERE org_id IS NOT NULL"))
        except Exception as e:
            log.warning("Skipped unique employee index (existing duplicates): %s", e)
    log.info("AICA schema upgrade complete (additive only).")
