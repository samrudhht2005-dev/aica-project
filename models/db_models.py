from sqlalchemy import Column, Integer, Float, String, TIMESTAMP, ForeignKey, Boolean
from database.db import engine, Base
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    phone = Column(String, default="")
    role = Column(String, default="admin")
    preferred_language = Column(String, default="en")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    product_name = Column(String)
    price = Column(Float)
    quantity = Column(Float)
    gst_percent = Column(Float)
    category = Column(String)
    gst_amount = Column(Float)
    total_amount = Column(Float)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    name = Column(String)
    stock = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True)
    name = Column(String, default="My Company")
    business_type = Column(String, default="Company") # Private Ltd, Sole Proprietorship, LLP, Partnership
    industry = Column(String, default="Retail")
    pan = Column(String, default="")
    gstin = Column(String, default="")
    gst_registered = Column(Boolean, default=False)
    registered_address = Column(String, default="")
    city = Column(String, default="")
    state = Column(String, default="Karnataka")
    pincode = Column(String, default="")
    contact_number = Column(String, default="")
    business_email = Column(String, default="")
    financial_year = Column(String, default="2026-27")
    accounting_period = Column(String, default="Monthly")
    tax_regime = Column(String, default="Regular (Old Regime)") # Section 115BAA, Section 115BAB, Old Regime
    employees_count = Column(Integer, default=0)
    business_turnover = Column(Float, default=0.0)
    organization_size = Column(String, default="Micro") # Micro, Small, Medium, Large
    branches = Column(String, default="Main Branch")
    bank_accounts = Column(String, default="")
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    date = Column(TIMESTAMP, default=datetime.utcnow)
    vendor = Column(String, nullable=False)
    invoice_number = Column(String, default="")
    category = Column(String, nullable=False)
    subcategory = Column(String, default="")
    amount = Column(Float, nullable=False) # taxable value
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    total_tax = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False) # grand total
    payment_method = Column(String, default="Cash") # Cash, Bank Transfer, Card
    is_business = Column(Boolean, default=True) # business vs personal
    classification = Column(String, default="Revenue") # Revenue vs Capital
    supplier_state = Column(String, default="")
    gst_rate = Column(Float, default=0.0)
    doc_path = Column(String, default="") # invoice file path/url
    description = Column(String, default="")
    branch = Column(String, default="Main Branch")
    status = Column(String, default="Approved") # Approved, Pending, Rejected
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    employee_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    department = Column(String, default="Sales")
    designation = Column(String, default="Associate")
    joining_date = Column(TIMESTAMP, default=datetime.utcnow)
    salary = Column(Float, default=0.0) # monthly gross
    basic_salary = Column(Float, default=0.0) # monthly basic
    allowances = Column(Float, default=0.0) # monthly allowances
    bonuses = Column(Float, default=0.0) # yearly/monthly bonuses
    incentives = Column(Float, default=0.0)
    employer_contribution = Column(Float, default=0.0) # PF, ESIC etc.
    benefits = Column(Float, default=0.0) # health insurance, benefits
    status = Column(String, default="Active") # Active, Resigned
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    name = Column(String, nullable=False)
    purchase_date = Column(TIMESTAMP, default=datetime.utcnow)
    purchase_value = Column(Float, nullable=False)
    gst_amount = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    supplier = Column(String, default="")
    category = Column(String, nullable=False) # Computers, Machinery, Furniture, Vehicles, Equipment, Buildings
    useful_life_years = Column(Float, default=5.0)
    depreciation_rate = Column(Float, default=15.0)
    current_value = Column(Float, nullable=False) # current book value
    disposal_date = Column(TIMESTAMP, nullable=True)
    disposal_value = Column(Float, default=0.0)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class ComplianceObligation(Base):
    __tablename__ = "compliance_obligations"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    title = Column(String, nullable=False) # e.g. "GSTR-3B Filing"
    due_date = Column(TIMESTAMP, nullable=False)
    category = Column(String, nullable=False) # GST, Income Tax, TDS, Payroll
    description = Column(String, default="")
    status = Column(String, default="Pending") # Pending, Completed, Overdue
    required_documents = Column(String, default="") # comma-separated list
    completed_date = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class TaxRecommendation(Base):
    __tablename__ = "tax_recommendations"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    title = Column(String, nullable=False)
    detected_item = Column(String, nullable=False) # e.g. "Unclaimed ITC on AWS"
    reason = Column(String, nullable=False)
    rule_section = Column(String, nullable=False) # e.g. "Section 16"
    eligibility_conditions = Column(String, nullable=False)
    required_documents = Column(String, nullable=False)
    estimated_tax_impact = Column(Float, default=0.0)
    confidence_level = Column(Float, default=100.0)
    severity = Column(String, default="Medium") # Critical, High, Medium, Low
    status = Column(String, default="Requires Verification") # Confirmed, Potential, Requires Verification, Missing Info
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Anomaly(Base):
    __tablename__ = "anomalies"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    severity = Column(String, default="Medium") # Critical, High, Medium, Low
    reason = Column(String, nullable=False)
    historical_comparison = Column(String, default="")
    details = Column(String, default="")
    status = Column(String, default="Active") # Active, Resolved, Ignored
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

# Auto-create missing tables, then additive upgrades (no drops / no data wipe)
Base.metadata.create_all(bind=engine)
from database.schema_upgrade import upgrade_schema
upgrade_schema()
