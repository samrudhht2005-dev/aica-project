import asyncio
import csv
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, Response, Depends, UploadFile, File, BackgroundTasks, Body
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, text, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, engine
from models.db_models import Transaction, Product, Organization, Expense, Employee, Asset, ComplianceObligation, TaxRecommendation, Anomaly, User, WeighTicket
from backend.auth import (
    hash_password, verify_password, valid_email, valid_gstin,
    set_session_cookie, clear_session_cookie, session_from_request,
    get_ui_mode, set_ui_mode_cookie, clear_ui_mode_cookie,
)
from backend.weigh_tickets import (
    WeighTicketError,
    cancel_timed_out_tickets,
    cancel_weigh_ticket_by_token,
    claim_active_ticket_for_checkout,
    create_weigh_ticket,
    list_weigh_tickets,
    resolve_error_http_status,
    resolve_weigh_ticket,
    ticket_public_dict,
)
from backend.weigh_label import create_weigh_label_pdf, qr_png_bytes
from backend.product_types import (
    PRODUCT_TYPE_LOOSE,
    PRODUCT_TYPE_PACKAGED,
    ProductTypeError,
    can_change_product_type,
    normalize_product_type,
    product_type_of,
    sale_unit_for,
    validate_sale_quantity,
    validate_stock_value,
)
from gemini.client import (
    query_gemini_assistant, client, MODEL_NAME, generate_content_with_fallback,
    ocr_and_analyze_invoice, classify_expense_ai, generate_tax_recommendations,
    simulate_what_if_scenario, generate_forecasting_data, detect_financial_anomalies,
    IRA_UNAVAILABLE_MSG,
)
from backend.optimization_actions import enrich_recommendation
from backend.optimization_sanitize import (
    scrub_optimization_recommendations,
    validate_ai_recommendation_payload,
    optimization_debug_snapshot,
)
from backend.money import (
    money_round, to_float, format_inr, sanitize_ai_amount, money_dict,
    structured_finance_block, INR_UNIT_LOCK, D,
)
from backend.runtime_paths import templates_dir, tax_rules_path, APP_VERSION

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))
templates.env.filters["inr"] = lambda v: format_inr(v)

# Global camera streamer reference — created lazily on first camera API use (not at FastAPI startup).
streamer = None

def init_camera(detection_callback):
    global streamer
    from camera.camera_stream import CameraStreamer
    streamer = CameraStreamer(detection_callback=detection_callback)
    streamer.start()


def ensure_camera():
    """Start camera streamer thread if needed (YOLO still deferred until power-ON)."""
    global streamer
    if streamer is None:
        init_camera(handle_vision_detection)
    return streamer

# Active SSE listener queues: (asyncio.Queue, event_loop, org_id)
active_queues: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop, int]] = []

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

BLOCKED_ITC_CATEGORIES = {"Employee Welfare", "Food & Beverages", "Motor Vehicles"}

def get_low_stock_flag(db: Session, org_id: int = None):
    if org_id is None:
        return False
    return db.query(Product).filter(Product.org_id == org_id, Product.stock < 10).first() is not None

def current_user_org(request: Request, db: Session):
    payload = session_from_request(request)
    if not payload:
        return None, None
    user = db.query(User).filter(User.id == payload["uid"]).first()
    if not user:
        return None, None
    org = db.query(Organization).filter(Organization.id == user.org_id).first()
    if not org or org.id != int(payload["oid"]):
        return None, None
    return user, org

def login_redirect():
    return RedirectResponse("/login", status_code=303)

def page_ctx(request: Request, db: Session, active_page: str, extra: dict = None, org=None, user=None):
    if org is None or user is None:
        found_user, found_org = current_user_org(request, db)
        if user is None:
            user = found_user
        if org is None:
            org = found_org
    ui_mode = get_ui_mode(request) or "org"
    lang = "en"
    if user is not None:
        lang = (getattr(user, "preferred_language", None) or "en").strip().lower()
        if lang not in ("en", "kn", "hi"):
            lang = "en"
    ctx = {
        "request": request,
        "has_low_stock": get_low_stock_flag(db, org.id if org else None),
        "active_page": active_page,
        "org": org,
        "auth_user": user,
        "ui_mode": ui_mode,
        "preferred_language": lang,
    }
    if extra:
        ctx.update(extra)
    return ctx

def parse_form_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")

def fy_bounds(org):
    fy = (org.financial_year if org else "2026-27") or "2026-27"
    try:
        start_year = int(str(fy).split("-")[0])
    except (TypeError, ValueError):
        start_year = 2026
    return datetime(start_year, 4, 1), datetime(start_year + 1, 3, 31, 23, 59, 59), start_year

def compute_80jjaa(employees, fy_start, fy_end):
    eligible = []
    deduction = 0.0
    for emp in employees:
        if emp.status != "Active":
            continue
        joined_in_fy = emp.joining_date and fy_start <= emp.joining_date <= fy_end
        qualifies = emp.salary <= 25000 and joined_in_fy
        if qualifies:
            eligible.append(emp)
            deduction += (emp.salary * 12) * 0.30
    return eligible, deduction

def gst_output_by_rate(txs):
    splits = {}
    for t in txs:
        items = None
        if t.category:
            try:
                items = json.loads(t.category)
            except (json.JSONDecodeError, TypeError):
                items = None
        if items and isinstance(items, list):
            for item in items:
                try:
                    pct = float(str(item.get("gst_pct", 0)).replace("%", "").strip() or 0)
                    amt = float(item.get("gst_amt", 0) or 0)
                except (TypeError, ValueError):
                    continue
                bucket = splits.setdefault(pct, {"amount": 0.0, "products": set()})
                bucket["amount"] += amt
                if item.get("product"):
                    bucket["products"].add(item["product"])
        else:
            pct = float(t.gst_percent or 0)
            bucket = splits.setdefault(pct, {"amount": 0.0, "products": set()})
            bucket["amount"] += float(t.gst_amount or 0)
            if t.product_name:
                bucket["products"].add(t.product_name)
    for pct, data in splits.items():
        data["amount"] = round(data["amount"], 2)
        data["products"] = sorted(list(data["products"]))
    return dict(sorted(splits.items()))

def get_financial_snapshot(db: Session, org: Organization = None, request: Request = None):
    if org is None and request is not None:
        _, org = current_user_org(request, db)
    if not org:
        empty = {
            "org": None,
            "fy_start": datetime(2026, 4, 1),
            "fy_end": datetime(2027, 3, 31),
            "fy_start_year": 2026,
            "txs": [], "exps": [], "employees": [], "active_employees": [], "assets": [],
            "sales_total": 0, "business_expense_total": 0, "personal_expense_total": 0,
            "monthly_payroll": 0, "annual_payroll": 0, "expenses_total": 0, "profit_total": 0,
            "output_gst": 0, "input_gst": 0, "blocked_itc": 0, "blocked_by_category": {},
            "eligible_itc": 0, "net_gst_payable": 0, "eligible_count": 0, "eligible_80jjaa": 0,
            "total_depreciation": 0, "assets_cost": 0, "taxable_old": 0, "total_tax_old": 0,
            "taxable_new": 0, "total_tax_new": 0, "expense_by_category": {}, "output_gst_splits": {},
            "currency": "INR",
            "money_unit": "absolute_INR",
            "declared_turnover_inr": 0,
        }
        empty["finance"] = structured_finance_block(empty)
        return empty

    fy_start, fy_end, fy_start_year = fy_bounds(org)
    oid = org.id

    def _in_fy(dt):
        if dt is None:
            return True
        try:
            return fy_start <= dt <= fy_end
        except TypeError:
            return True

    all_txs = db.query(Transaction).filter(Transaction.org_id == oid).all()
    txs = [t for t in all_txs if _in_fy(getattr(t, "created_at", None))]
    sales_total = to_float(sum((D(t.total_amount) for t in txs), D("0")))
    output_gst = to_float(sum((D(t.gst_amount) for t in txs), D("0")))

    all_exps = db.query(Expense).filter(Expense.org_id == oid).all()
    exps = [e for e in all_exps if _in_fy(getattr(e, "date", None) or getattr(e, "created_at", None))]
    business_exps = [e for e in exps if e.is_business]
    personal_exps = [e for e in exps if not e.is_business]
    business_expense_total = to_float(sum((D(e.total_amount) for e in business_exps), D("0")))
    personal_expense_total = to_float(sum((D(e.total_amount) for e in personal_exps), D("0")))

    employees = db.query(Employee).filter(Employee.org_id == oid).all()
    active_employees = [e for e in employees if e.status == "Active"]
    monthly_payroll = to_float(sum(
        (D(emp.salary) + D(emp.employer_contribution) + D(emp.benefits) for emp in active_employees),
        D("0"),
    ))
    annual_payroll = to_float(D(monthly_payroll) * 12)

    # Absolute INR throughout. Tax working uses FY sales/expenses + annualised payroll.
    expenses_total = to_float(D(business_expense_total) + D(annual_payroll))
    profit_total = to_float(D(sales_total) - D(expenses_total))

    input_gst_all = to_float(sum((D(e.total_tax) for e in business_exps), D("0")))
    blocked_by_category = {}
    for e in business_exps:
        if e.category in BLOCKED_ITC_CATEGORIES:
            blocked_by_category[e.category] = to_float(
                D(blocked_by_category.get(e.category, 0)) + D(e.total_tax)
            )
    blocked_itc = to_float(sum((D(v) for v in blocked_by_category.values()), D("0")))
    eligible_itc = to_float(max(D("0"), D(input_gst_all) - D(blocked_itc)))
    net_gst_payable = to_float(D(output_gst) - D(eligible_itc))

    eligible_hires, eligible_80jjaa = compute_80jjaa(active_employees, fy_start, fy_end)
    eligible_80jjaa = to_float(eligible_80jjaa)

    assets = db.query(Asset).filter(Asset.org_id == oid).all()
    total_depreciation = D("0")
    assets_cost = D("0")
    for a in assets:
        assets_cost += D(a.purchase_value)
        total_depreciation += D(a.purchase_value) * (D(a.depreciation_rate) / D("100"))
    total_depreciation = to_float(total_depreciation)
    assets_cost = to_float(assets_cost)

    taxable_old = to_float(max(D("0"), D(profit_total) - D(eligible_80jjaa) - D(total_depreciation)))
    tax_old = D(taxable_old) * D("0.25")
    total_tax_old = to_float(tax_old + (tax_old * D("0.04")))

    taxable_new = to_float(max(D("0"), D(profit_total) - D(eligible_80jjaa) - D(total_depreciation)))
    tax_new = D(taxable_new) * D("0.22")
    surcharge_new = tax_new * D("0.10")
    total_tax_new = to_float(tax_new + surcharge_new + ((tax_new + surcharge_new) * D("0.04")))

    expense_by_category = {}
    for e in business_exps:
        cat = e.category or "Uncategorised"
        expense_by_category[cat] = to_float(D(expense_by_category.get(cat, 0)) + D(e.total_amount))

    snap = {
        "org": org,
        "fy_start": fy_start,
        "fy_end": fy_end,
        "fy_start_year": fy_start_year,
        "txs": txs,
        "exps": exps,
        "employees": employees,
        "active_employees": active_employees,
        "assets": assets,
        "sales_total": sales_total,
        "business_expense_total": business_expense_total,
        "personal_expense_total": personal_expense_total,
        "monthly_payroll": monthly_payroll,
        "annual_payroll": annual_payroll,
        "expenses_total": expenses_total,
        "profit_total": profit_total,
        "output_gst": output_gst,
        "input_gst": input_gst_all,
        "blocked_itc": blocked_itc,
        "blocked_by_category": blocked_by_category,
        "eligible_itc": eligible_itc,
        "net_gst_payable": net_gst_payable,
        "eligible_count": len(eligible_hires),
        "eligible_80jjaa": eligible_80jjaa,
        "total_depreciation": total_depreciation,
        "assets_cost": assets_cost,
        "taxable_old": taxable_old,
        "total_tax_old": total_tax_old,
        "taxable_new": taxable_new,
        "total_tax_new": total_tax_new,
        "expense_by_category": expense_by_category,
        "output_gst_splits": gst_output_by_rate(txs),
        "currency": "INR",
        "money_unit": "absolute_INR",
        "declared_turnover_inr": to_float(getattr(org, "business_turnover", 0) or 0),
    }
    snap["finance"] = structured_finance_block(snap)
    return snap

def upsert_rule_based_recommendations(db: Session, snap: dict):
    org = snap.get("org")
    if not org:
        return

    planned = []
    if snap["eligible_80jjaa"] > 0:
        planned.append({
            "title": "Section 80JJAA additional employee deduction",
            "detected_item": f"{snap['eligible_count']} new hire(s) with monthly emoluments ≤ ₹25,000 in FY {org.financial_year}",
            "reason": "These new employees appear to qualify for an extra 30% deduction of their annual emoluments for up to 3 years. This reduces taxable profit without changing take-home pay.",
            "rule_section": "Sec 80JJAA",
            "eligibility_conditions": "New additional employees, emoluments ≤ ₹25,000/month, employed ≥ 240 days (150 in apparel/leather), and participating in a recognised PF scheme. Keep appointment letters, payroll, and EPF challans.",
            "required_documents": "Appointment letter, Form 12BA/payroll register, EPF contribution proof, increment/joining records",
            "estimated_tax_impact": to_float(D(snap["eligible_80jjaa"]) * D("0.25")),
            "impact_type": "tax_saving",
            "confidence_level": 82.0,
            "severity": "High",
        })
    if snap["total_depreciation"] > 0:
        planned.append({
            "title": "Section 32 WDV depreciation on fixed assets",
            "detected_item": f"Capital block worth {format_inr(snap['assets_cost'])}",
            "reason": "Income-tax depreciation is a non-cash deduction. Claiming the correct WDV rate on each asset block lowers taxable income this year.",
            "rule_section": "Sec 32",
            "eligibility_conditions": "Asset must be owned and put to use for the business. Computers/software 40%, plant & machinery 15%, furniture 10%, buildings 10% (WDV).",
            "required_documents": "Purchase invoice, installation/put-to-use note, fixed asset register, GST invoice if ITC was claimed",
            "estimated_tax_impact": to_float(D(snap["total_depreciation"]) * D("0.25")),
            "impact_type": "tax_saving",
            "confidence_level": 90.0,
            "severity": "Medium",
        })
    if snap["eligible_itc"] > 0:
        planned.append({
            "title": "Claim eligible GST Input Tax Credit",
            "detected_item": f"Business input GST {format_inr(snap['input_gst'])} (blocked {format_inr(snap['blocked_itc'])})",
            "reason": "GST paid on eligible business purchases can be set off against GST you collect on sales (output tax). Personal spends and blocked categories cannot be claimed.",
            "rule_section": "GST Sec 16 / 17(5)",
            "eligibility_conditions": "Valid tax invoice, goods/services used for business, supplier has filed GSTR-1, you have filed GSTR-3B, and the credit is not blocked under Section 17(5).",
            "required_documents": "Tax invoices, GSTR-2B matching, e-way bills where applicable",
            "estimated_tax_impact": to_float(snap["eligible_itc"]),
            "impact_type": "tax_credit",
            "confidence_level": 88.0,
            "severity": "High",
        })
    if snap["personal_expense_total"] > 0:
        planned.append({
            "title": "Keep personal spends out of the P&L",
            "detected_item": f"Personal expenses of {format_inr(snap['personal_expense_total'])}",
            "reason": "Personal drawings or owner expenses are not business deductions and do not create ITC. Recording them as 'Personal' keeps your profit and GST figures honest.",
            "rule_section": "IT Act / GST Sec 16",
            "eligibility_conditions": "Only wholly and exclusively business expenditure is deductible. Mixed-use items need a reasonable split.",
            "required_documents": "Bank statements, owner current account / drawings ledger",
            "estimated_tax_impact": to_float(D(snap["personal_expense_total"]) * D("0.25")),
            "impact_type": "risk_avoided",
            "confidence_level": 95.0,
            "severity": "Medium",
        })

    old_tax, new_tax = snap["total_tax_old"], snap["total_tax_new"]
    if abs(old_tax - new_tax) > 1:
        cheaper = "Section 115BAA (22% + surcharge)" if new_tax < old_tax else "Regular old regime (25% + cess, with deductions)"
        planned.append({
            "title": "Compare corporate tax regimes before you lock in",
            "detected_item": f"Old regime tax {format_inr(old_tax)} vs 115BAA {format_inr(new_tax)}",
            "reason": f"On current books, {cheaper} appears lower. Regime choice is largely irreversible for companies under 115BAA — review deductions you would give up.",
            "rule_section": "Sec 115BAA",
            "eligibility_conditions": "115BAA disallows some incentives and additional depreciation; standard WDV and 80JJAA are still available. Once opted, you generally cannot go back.",
            "required_documents": "Form 10-IC (if opting 115BAA), computation of income under both regimes",
            "estimated_tax_impact": to_float(abs(D(old_tax) - D(new_tax))),
            "impact_type": "liability_delta",
            "confidence_level": 70.0,
            "severity": "Medium",
        })

    titles = {p["title"] for p in planned}
    existing = db.query(TaxRecommendation).filter(TaxRecommendation.org_id == org.id).all()
    for rec in existing:
        if rec.title in titles or rec.rule_section in ("Sec 80JJAA", "Sec 32", "GST Sec 16 / 17(5)", "IT Act / GST Sec 16", "Sec 115BAA"):
            db.delete(rec)
    db.commit()

    for p in planned:
        db.add(TaxRecommendation(
            org_id=org.id,
            title=p["title"],
            detected_item=p["detected_item"],
            reason=p["reason"],
            rule_section=p["rule_section"],
            eligibility_conditions=p["eligibility_conditions"],
            required_documents=p["required_documents"],
            estimated_tax_impact=p["estimated_tax_impact"],
            confidence_level=p["confidence_level"],
            severity=p["severity"],
            status="Live from your books"
        ))
    db.commit()

def sync_org_headcount(db: Session, org: Organization):
    if not org:
        return
    org.employees_count = db.query(Employee).filter(Employee.org_id == org.id, Employee.status == "Active").count()
    db.commit()

def get_db_schema() -> str:
    schema = (
        "Table: products\n"
        "Columns: id (INTEGER, PK), org_id (INTEGER), name (String), stock (Float), price (Float), "
        "product_type (String: loose|packaged), created_at (TIMESTAMP)\n\n"
        "Table: transactions\n"
        "Columns: id (INTEGER, PK), org_id (INTEGER), product_name (String), price (Float), quantity (Float), gst_percent (Float), gst_amount (Float), total_amount (Float), category (String, holds JSON details), created_at (TIMESTAMP)\n\n"
        "Table: organizations\n"
        "Columns: id (INTEGER, PK), name (String), business_type (String), industry (String), pan (String), gstin (String), registered_address (String), state (String), financial_year (String), tax_regime (String), employees_count (Integer), business_turnover (Float), organization_size (String), branches (String), bank_accounts (String)\n\n"
        "Table: expenses\n"
        "Columns: id (INTEGER, PK), date (TIMESTAMP), vendor (String), invoice_number (String), category (String), subcategory (String), amount (Float), cgst (Float), sgst (Float), igst (Float), total_tax (Float), total_amount (Float), payment_method (String), is_business (Boolean), classification (String), doc_path (String), description (String), status (String)\n\n"
        "Table: employees\n"
        "Columns: id (INTEGER, PK), employee_id (String, Unique), name (String), department (String), designation (String), joining_date (TIMESTAMP), salary (Float), basic_salary (Float), allowances (Float), bonuses (Float), incentives (Float), employer_contribution (Float), benefits (Float), status (String)\n\n"
        "Table: assets\n"
        "Columns: id (INTEGER, PK), name (String), purchase_date (TIMESTAMP), purchase_value (Float), gst_amount (Float), cgst (Float), sgst (Float), igst (Float), supplier (String), category (String), depreciation_rate (Float), current_value (Float)\n\n"
        "Table: compliance_obligations\n"
        "Columns: id (INTEGER, PK), title (String), due_date (TIMESTAMP), category (String), description (String), status (String), completed_date (TIMESTAMP)\n\n"
        "Table: tax_recommendations\n"
        "Columns: id (INTEGER, PK), title (String), detected_item (String), reason (String), rule_section (String), estimated_tax_impact (Float), confidence_level (Float), severity (String), status (String)\n\n"
        "Table: anomalies\n"
        "Columns: id (INTEGER, PK), severity (String), reason (String), historical_comparison (String), details (String), status (String), created_at (TIMESTAMP)\n"
    )
    return schema

def run_db_query(query: str, org_id: int | None = None) -> str:
    stripped = query.strip().upper()
    if not stripped.startswith("SELECT"):
        return "Error: Database queries are restricted to read-only SELECT statements for security reasons."
        
    forbidden_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "TRUNCATE"]
    for keyword in forbidden_keywords:
        if re.search(r'\b' + keyword + r'\b', stripped):
            return f"Error: Database mutation keyword '{keyword}' is prohibited."

    tenant_tables = ("products", "transactions", "expenses", "employees", "assets",
                     "compliance_obligations", "tax_recommendations", "anomalies")
    q_lower = query.lower()
    if org_id is not None and any(t in q_lower for t in tenant_tables) and "org_id" not in q_lower:
        return f"Error: Filter this organisation only with WHERE org_id = {int(org_id)}."
            
    try:
        with engine.connect() as connection:
            result = connection.execute(text(query))
            keys = result.keys()
            rows = [dict(zip(keys, row)) for row in result.fetchall()]
            
            def json_serializer(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return str(obj)
                
            return json.dumps(rows, default=json_serializer, indent=2)
    except Exception as e:
        return f"Database query failed: {str(e)}"

# Centralized GST classification
def classify_gst(product_name: str) -> int:
    p = product_name.lower()
    
    # 0% GST items
    zero_gst = ["tomato", "potato", "onion", "carrot", "spinach", "broccoli", "cabbage", 
                "apple", "banana", "orange", "mango", "grapes", "strawberry", "pineapple", 
                "milk", "water", "eggs", "salt", "vegetable", "fruit", "book"]
    if any(x in p for x in zero_gst):
        return 0

    # 5% GST items
    five_gst = ["tea", "coffee", "sugar", "rice", "pasta", "flour", "spices", "bread", 
                "cheese", "butter", "medicine", "food", "paneer"]
    if any(x in p for x in five_gst):
        return 5

    # 18% GST items
    eighteen_gst = ["tv", "laptop", "mobile", "headphones", "smartwatch", "camera", "ac", 
                    "refrigerator", "soap", "shampoo", "toothpaste", "detergent", 
                    "tissue paper", "trash bags", "towel", "bedsheet", "electronics", 
                    "maggi", "lays", "kurkure", "parle g", "good day", "biscuits"]
    if any(x in p for x in eighteen_gst):
        return 18

    # 28% GST items
    twentyeight_gst = ["car", "bike", "watch", "perfume", "jewelry", "beer", "wine", 
                       "soda", "luxury", "coca cola", "pepsi", "sprite", "juice"]
    if any(x in p for x in twentyeight_gst):
        return 28

    return 12  # Standard default rate

def load_tax_rules() -> dict:
    rules_path = str(tax_rules_path())
    if not os.path.exists(rules_path):
        return {}
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Failed to load tax_rules.json: {e}")
        return {}

def normalize_state_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()

def expense_gst_from_rules(org: Organization, category: str, amount: float, supplier_state: str = ""):
    """Authoritative expense GST split from tax_rules.json + org GST registration / place of supply."""
    amount = float(amount or 0)
    result = {
        "ok": True,
        "error": None,
        "gst_applicable": False,
        "treatment": "exempt",
        "rate": 0.0,
        "split": "none",
        "cgst": 0.0,
        "sgst": 0.0,
        "igst": 0.0,
        "total_tax": 0.0,
        "total_amount": amount,
        "note": "",
        "needs_supplier_state": False,
    }
    if amount < 0:
        result["ok"] = False
        result["error"] = "Amount cannot be negative."
        return result

    if not org or not getattr(org, "gst_registered", False):
        result["note"] = "Organisation is not GST-registered. No GST applied."
        return result

    rules = load_tax_rules()
    categories = (rules.get("gst_rules") or {}).get("expense_category_rates", {}).get("categories", {})
    meta = categories.get(category)
    if not meta:
        result["ok"] = False
        result["error"] = f"No GST rate configured for category '{category}'. Add it under gst_rules.expense_category_rates in tax_rules.json."
        result["needs_supplier_state"] = False
        return result

    treatment = (meta.get("treatment") or "taxable").lower()
    rate = float(meta.get("rate") or 0)
    result["treatment"] = treatment
    result["rate"] = rate
    result["note"] = meta.get("note") or ""

    if treatment != "taxable" or rate <= 0:
        result["note"] = result["note"] or "This category is GST-exempt / nil-rated."
        return result

    org_state = normalize_state_name(getattr(org, "state", "") or "")
    vendor_state = normalize_state_name(supplier_state)
    if not org_state:
        result["ok"] = False
        result["error"] = "Set your organisation state in Organisation settings before calculating GST."
        return result
    if not vendor_state:
        result["ok"] = False
        result["error"] = "Enter the supplier / vendor state so AICA can decide CGST+SGST vs IGST."
        result["needs_supplier_state"] = True
        return result

    result["gst_applicable"] = True
    tax = round(amount * rate / 100.0, 2)
    if vendor_state == org_state:
        half = round(tax / 2.0, 2)
        # keep halves adding exactly to tax
        result["cgst"] = half
        result["sgst"] = round(tax - half, 2)
        result["igst"] = 0.0
        result["split"] = "intra"
    else:
        result["cgst"] = 0.0
        result["sgst"] = 0.0
        result["igst"] = tax
        result["split"] = "inter"
    result["total_tax"] = round(result["cgst"] + result["sgst"] + result["igst"], 2)
    result["total_amount"] = round(amount + result["total_tax"], 2)
    return result

# Callback triggered by camera scanner thread
def handle_vision_detection(product_name: str, confidence: float):
    db = SessionLocal()
    try:
        normalized_name = product_name.strip()
        for queue, loop, org_id in list(active_queues):
            product = db.query(Product).filter(
                Product.org_id == org_id,
                func.lower(Product.name) == normalized_name.lower()
            ).first()
            if not product:
                continue
            gst_pct = classify_gst(product.name)
            data = {
                "name": product.name,
                "price": product.price,
                "stock": product.stock,
                "gst_pct": gst_pct,
                "confidence": confidence
            }
            loop.call_soon_threadsafe(queue.put_nowait, data)
    except Exception as e:
        logging.error(f"Error handling vision detection database lookup: {e}")
    finally:
        db.close()

# Dummy financial seeding is disabled — new organisations start at zero.
def seed_dummy_data(db: Session):
    return

def seed_compliance_calendar(db: Session, org: Organization):
    if not org:
        return
    if db.query(ComplianceObligation).filter(ComplianceObligation.org_id == org.id).count() > 0:
        return
    rules_path = str(tax_rules_path())
    if not os.path.exists(rules_path):
        return
    with open(rules_path, "r") as f:
        rules = json.load(f)
    deadlines = rules.get("compliance_calendar", {}).get("deadlines", [])
    for dl in deadlines:
        due_day = dl.get("due_day")
        due_date_str = dl.get("due_date")
        now = datetime.now()
        if due_day:
            due_dt = datetime(now.year, now.month, due_day)
            if due_dt < now:
                if now.month == 12:
                    due_dt = datetime(now.year + 1, 1, due_day)
                else:
                    due_dt = datetime(now.year, now.month + 1, due_day)
        elif due_date_str:
            m, d = map(int, due_date_str.split("-"))
            due_dt = datetime(now.year, m, d)
            if due_dt < now:
                due_dt = datetime(now.year + 1, m, d)
        else:
            due_dt = now
        db.add(ComplianceObligation(
            org_id=org.id,
            title=dl["title"],
            due_date=due_dt,
            category=dl["category"],
            description=dl["description"],
            status="Pending",
            required_documents=",".join(dl.get("required_documents", []))
        ))
    db.commit()
# Automated AI scanner for dashboard updates
def update_ai_insights_and_recommendations(db: Session, org, sales_total, expenses_total, profit_total, input_gst, output_gst):
    rules_path = str(tax_rules_path())
    rules_context = {}
    if os.path.exists(rules_path):
        try:
            with open(rules_path, "r") as f:
                rules_context = json.load(f)
        except Exception as e:
            logging.error(f"Error loading tax rules for scan: {e}")
            
    financial_summary = {
        "currency": "INR",
        "unit": "absolute_INR",
        "unit_note": "All figures are absolute rupees. 1607.80 means INR 1607.80 — never lakhs.",
        "turnover": to_float(sales_total),
        "expenses": to_float(expenses_total),
        "profit": to_float(profit_total),
        "input_gst": to_float(input_gst),
        "output_gst": to_float(output_gst),
        "assets_count": db.query(Asset).filter(Asset.org_id == org.id).count(),
        "employees_count": db.query(Employee).filter(Employee.org_id == org.id).count()
    }
    
    org_data = {
        "name": org.name,
        "business_type": org.business_type,
        "industry": org.industry,
        "state": org.state,
        "tax_regime": org.tax_regime,
        "employees_count": org.employees_count,
        "declared_annual_turnover_inr": to_float(org.business_turnover or 0),
        "declared_turnover_unit": "absolute_INR",
    }
    
    # 1. Keep live rule-based recs, then add AI extras that don't duplicate titles
    try:
        # Pass absolute-INR snapshot facts so Gemini cannot invent scale
        financial_summary["sales_total_absolute_inr"] = to_float(sales_total)
        financial_summary["expenses_absolute_inr"] = to_float(expenses_total)
        financial_summary["profit_absolute_inr"] = to_float(profit_total)
        financial_summary["example"] = (
            f"If turnover is {to_float(sales_total)}, display {format_inr(sales_total)}. "
            f"Never write '{to_float(sales_total)} Lakhs'."
        )
        snap_for_gate = {
            "sales_total": to_float(sales_total),
            "business_expense_total": to_float(expenses_total),
            "expenses_total": to_float(expenses_total),
            "profit_total": to_float(profit_total),
            "total_tax_old": to_float(max(0.0, to_float(profit_total) * 0.25 * 1.04)),
            "total_tax_new": 0,
            "eligible_itc": to_float(input_gst),
        }
        recs = generate_tax_recommendations(org_data, financial_summary, rules_context)
        if recs:
            existing_titles = {r.title.lower() for r in db.query(TaxRecommendation).filter(TaxRecommendation.org_id == org.id).all()}
            for r in recs:
                cleaned = validate_ai_recommendation_payload(r, snap_for_gate)
                if not cleaned:
                    continue
                title = cleaned.get("title", "Optimization Found")
                if title.lower() in existing_titles:
                    continue
                rec = TaxRecommendation(
                    org_id=org.id,
                    title=title,
                    detected_item=cleaned.get("detected_item", ""),
                    reason=cleaned.get("reason", ""),
                    rule_section=cleaned.get("rule_section", "N/A"),
                    eligibility_conditions=cleaned.get("eligibility_conditions", ""),
                    required_documents=cleaned.get("required_documents", ""),
                    estimated_tax_impact=sanitize_ai_amount(cleaned.get("estimated_tax_impact", 0.0)),
                    confidence_level=float(cleaned.get("confidence_level", 70.0)),
                    severity=cleaned.get("severity", "Medium"),
                    status=cleaned.get("status", "Requires Verification"),
                )
                db.add(rec)
                existing_titles.add(title.lower())
            db.commit()
    except Exception as e:
        logging.error(f"Error running automated tax planning: {e}")
        
    # 2. Run anomaly detector
    try:
        txs = db.query(Transaction).filter(Transaction.org_id == org.id).limit(50).all()
        exps = db.query(Expense).filter(Expense.org_id == org.id).limit(50).all()
        data_to_scan = {
            "transactions": [{"id": t.id, "product": t.product_name, "total": t.total_amount, "gst": t.gst_amount} for t in txs],
            "expenses": [{"id": e.id, "vendor": e.vendor, "total": e.total_amount, "tax": e.total_tax, "category": e.category} for e in exps]
        }
        anoms = detect_financial_anomalies(data_to_scan)
        if anoms:
            db.query(Anomaly).filter(Anomaly.org_id == org.id).delete()
            for a in anoms:
                anom = Anomaly(
                    org_id=org.id,
                    severity=a.get("severity", "Medium"),
                    reason=a.get("reason", ""),
                    historical_comparison=a.get("historical_comparison", ""),
                    details=a.get("details", ""),
                    status="Active"
                )
                db.add(anom)
            db.commit()
    except Exception as e:
        logging.error(f"Error running automated anomaly detection: {e}")

def update_ai_insights_and_recommendations_bg(org_id, sales_total, expenses_total, profit_total, input_gst, output_gst):
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if org:
            update_ai_insights_and_recommendations(db, org, sales_total, expenses_total, profit_total, input_gst, output_gst)
    except Exception as e:
        logging.error(f"Error in background tax update task: {e}")
    finally:
        db.close()

# ---------------- SSE CAMERA EVENTS ----------------
@router.get("/camera/events")
async def camera_events(request: Request):
    ensure_camera()
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    org_id = getattr(request.state, "org_id", None)
    if org_id is None:
        return login_redirect()
    entry = (queue, loop, int(org_id))
    active_queues.append(entry)
    
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            if entry in active_queues:
                active_queues.remove(entry)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ---------------- CAMERA VIDEO FEED ----------------
@router.get("/camera/video_feed")
def video_feed():
    ensure_camera()
    if streamer is None:
        return Response("Camera streamer is disabled or not initialized.", status_code=503)
        
    def frame_generator():
        while streamer.running:
            frame_bytes = streamer.get_frame_bytes()
            if frame_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.04)
            
    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

# ---------------- CAMERA STATUS & CONTROL ----------------
@router.get("/camera/status")
def get_camera_status():
    ensure_camera()
    if streamer is None:
        return {
            "active": False,
            "is_simulated": True,
            "camera_error": False,
            "camera_index": 0,
            "camera_powered": False,
        }
    summary = streamer.get_latest_summary() if hasattr(streamer, "get_latest_summary") else {}
    return {
        "active": streamer.running,
        "is_simulated": streamer.is_simulated,
        "camera_error": getattr(streamer, "camera_error", False),
        "camera_index": getattr(streamer, "camera_index", 0),
        "model_ready": summary.get("model_ready", False),
        "preview_available": summary.get("preview_available", True),
        "ai_detection_available": summary.get("ai_detection_available", False),
        "ai_detection_error": summary.get("ai_detection_error"),
        "qr_detection_available": summary.get("qr_detection_available", True),
        "scan_state": summary.get("state"),
        "scan_message": summary.get("message"),
        "auto_add_enabled": summary.get("auto_add_enabled", False),
        "camera_powered": bool(getattr(streamer, "camera_powered", False)),
    }

@router.post("/camera/power")
def set_camera_power(enabled: str = Form("false")):
    ensure_camera()
    if streamer is None:
        return {"success": False, "error": "Camera streamer not initialized"}
    ok = streamer.set_camera_power(parse_form_bool(enabled))
    summary = streamer.get_latest_summary() if hasattr(streamer, "get_latest_summary") else {}
    return {
        "success": ok,
        "camera_powered": streamer.camera_powered,
        "camera_error": getattr(streamer, "camera_error", False),
        "is_simulated": streamer.is_simulated,
        "model_ready": summary.get("model_ready", False),
        "preview_available": summary.get("preview_available", True),
        "ai_detection_available": summary.get("ai_detection_available", False),
        "ai_detection_error": summary.get("ai_detection_error"),
        "qr_detection_available": summary.get("qr_detection_available", True),
        "scan_state": summary.get("state"),
        "scan_message": summary.get("message"),
    }

@router.get("/camera/detections")
def get_camera_detections():
    """Latest honest detection summary for the POS UI (poll ~4–5 Hz)."""
    ensure_camera()
    if streamer is None:
        return {
            "state": "offline",
            "message": "Camera streamer is not initialized.",
            "detections": [],
            "accepted": [],
            "model_ready": False,
            "scan_purpose": "checkout",
        }
    return streamer.get_latest_summary()


@router.post("/camera/scan_purpose")
def set_camera_scan_purpose(purpose: str = Form("checkout")):
    """
    Direct QR events to POS checkout or Weigh verified-cancel.
    Uses the shared OpenCV QRCodeDetector path (desktop-safe).
    """
    ensure_camera()
    if streamer is None:
        return {"success": False, "error": "Camera streamer not initialized"}
    p = streamer.set_scan_purpose(purpose)
    return {"success": True, "scan_purpose": p}


@router.post("/camera/decode")
async def decode_camera_frame(request: Request):
    """
    Decode an uploaded image frame with OpenCV QRCodeDetector.
    Platform-independent fallback / test path (does not require BarcodeDetector).
    Accepts multipart file field "frame" or "image", or raw body bytes.
    """
    ensure_camera()
    if streamer is None:
        return JSONResponse({"ok": False, "error": "Camera streamer not initialized"}, status_code=503)

    import cv2
    import numpy as np

    raw = b""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("frame") or form.get("image") or form.get("file")
        if upload is not None and hasattr(upload, "read"):
            raw = await upload.read()
        elif isinstance(upload, (bytes, bytearray)):
            raw = bytes(upload)
    else:
        raw = await request.body()

    if not raw:
        return JSONResponse(
            {"ok": False, "error": "No image frame provided.", "code": "missing_frame"},
            status_code=400,
        )
    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse(
            {"ok": False, "error": "Could not decode image.", "code": "bad_image"},
            status_code=400,
        )
    token, _ = streamer.detect_aica_qr_token(frame, force=True)
    if not token:
        return {"ok": False, "token": None, "code": "no_aica_qr"}
    return {"ok": True, "token": token}

@router.post("/camera/auto_add")
def set_camera_auto_add(enabled: str = Form("false")):
    """When false (default), user must confirm before a detection enters the cart."""
    ensure_camera()
    if streamer is None:
        return {"success": False, "error": "Camera streamer not initialized"}
    streamer.set_auto_add(parse_form_bool(enabled))
    return {"success": True, "auto_add_enabled": streamer.auto_add_enabled}

@router.post("/camera/confirm_product")
def confirm_camera_product(
    request: Request,
    product_name: str = Form(...),
    confidence: float = Form(0.0),
    db: Session = Depends(get_db),
):
    """
    Map a confidently detected class to the organisation's inventory and return
    SQL-backed price/stock. Does not create a sale — POS cart remains client-side
    until checkout.
    """
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"ok": False, "error": "Please sign in again."}, status_code=401)

    from vision.product_classes import canonicalize_product_name
    canonical = canonicalize_product_name(product_name) or product_name.strip()
    product = db.query(Product).filter(
        Product.org_id == org.id,
        func.lower(Product.name) == canonical.lower()
    ).first()
    if not product:
        return JSONResponse({
            "ok": False,
            "error": f"'{canonical}' is not in this organisation's warehouse. Add it under Warehouse first.",
        }, status_code=404)

    return {
        "ok": True,
        "name": product.name,
        "price": product.price,
        "stock": product.stock,
        "product_type": product_type_of(product),
        "unit": sale_unit_for(product_type_of(product)),
        "gst_pct": classify_gst(product.name),
        "confidence": float(confidence),
    }

@router.post("/camera/toggle_mode")
def toggle_camera_mode(simulate: bool = Form(...)):
    ensure_camera()
    if streamer is None:
        return {"success": False, "error": "Camera streamer not initialized"}
    success = streamer.set_simulation_mode(simulate)
    return {
        "success": success, 
        "is_simulated": streamer.is_simulated, 
        "camera_error": getattr(streamer, "camera_error", False)
    }

@router.post("/camera/set_index")
def set_camera_index(index: int = Form(...)):
    ensure_camera()
    if streamer is None:
        return {"success": False, "error": "Camera streamer not initialized"}
    success = streamer.set_camera_index(index)
    return {
        "success": success,
        "camera_index": streamer.camera_index,
        "is_simulated": streamer.is_simulated,
        "camera_error": streamer.camera_error
    }

# ---------------- AUTH ----------------
def _workspace_home(mode: str) -> str:
    mode_n = (mode or "").strip().lower()
    if mode_n == "pos":
        return "/pos"
    if mode_n == "weigh":
        return "/weigh"
    if mode_n == "org":
        return "/"
    return "/select-interface"


def _post_auth_home(request: Request) -> str:
    mode = get_ui_mode(request)
    if mode in ("pos", "org", "weigh"):
        return _workspace_home(mode)
    return "/select-interface"

@router.get("/select-interface")
def select_interface_page(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    return templates.TemplateResponse(
        request=request,
        name="select_interface.html",
        context=page_ctx(request, db, "select-interface", org=org, user=user),
    )

@router.post("/select-interface")
def select_interface_submit(
    request: Request,
    db: Session = Depends(get_db),
    mode: str | None = Form(None),
    target: str | None = Form(None),
):
    """Legacy landing POST — prefer /switch-interface (used by workspace cards + sidebars)."""
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    mode_n = ((mode or target) or "").strip().lower()
    if mode_n not in ("pos", "org", "weigh"):
        return RedirectResponse("/select-interface", status_code=303)
    response = RedirectResponse(_workspace_home(mode_n), status_code=303)
    set_ui_mode_cookie(response, mode_n)
    return response

@router.post("/switch-interface")
def switch_interface(request: Request, target: str = Form(...), db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    target_n = (target or "").strip().lower()
    if target_n not in ("pos", "org", "weigh"):
        # Legacy two-way toggle fallback
        current = get_ui_mode(request) or "org"
        target_n = "pos" if current != "pos" else "org"
    response = RedirectResponse(_workspace_home(target_n), status_code=303)
    set_ui_mode_cookie(response, target_n)
    return response

@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if user and org:
        return RedirectResponse(_post_auth_home(request), status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": None, "email": ""})

@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember: str = Form(""),
    db: Session = Depends(get_db)
):
    email_n = email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email_n).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=401,
            context={"request": request, "error": "Invalid email or password.", "email": email_n}
        )
    org = db.query(Organization).filter(Organization.id == user.org_id).first()
    if not org:
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=401,
            context={"request": request, "error": "Organisation not found for this account.", "email": email_n}
        )
    # Prefer select-interface on fresh login so user chooses where to start
    response = RedirectResponse("/select-interface", status_code=303)
    set_session_cookie(response, user.id, org.id, remember=parse_form_bool(remember))
    return response

@router.get("/signup")
def signup_page(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if user and org:
        return RedirectResponse(_post_auth_home(request), status_code=303)
    return templates.TemplateResponse(request=request, name="signup.html", context={"request": request, "error": None, "form": {}})

@router.post("/signup")
def signup_submit(
    request: Request,
    org_name: str = Form(...),
    business_type: str = Form(...),
    gst_registered: str = Form("false"),
    gstin: str = Form(""),
    pan: str = Form(""),
    contact_number: str = Form(""),
    registered_address: str = Form(""),
    city: str = Form(""),
    state: str = Form("Karnataka"),
    pincode: str = Form(""),
    business_email: str = Form(""),
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    form = {
        "org_name": org_name, "gstin": gstin, "pan": pan, "contact_number": contact_number,
        "registered_address": registered_address, "city": city, "state": state, "pincode": pincode,
        "business_email": business_email, "full_name": full_name, "email": email
    }
    def fail(msg):
        return templates.TemplateResponse(request=request, name="signup.html", status_code=400, context={"request": request, "error": msg, "form": form})

    if len(password) < 8:
        return fail("Password must be at least 8 characters.")
    if password != confirm_password:
        return fail("Password and confirmation do not match.")
    if not valid_email(email):
        return fail("Enter a valid login email.")
    gst_flag = parse_form_bool(gst_registered)
    gstin_n = gstin.strip().upper()
    if gst_flag:
        if not valid_gstin(gstin_n):
            return fail("GSTIN must be a 15-character GST number (e.g. 29AAAAA0000A1Z5).")
    else:
        gstin_n = gstin_n if gstin_n else ""
    email_n = email.strip().lower()
    if db.query(User).filter(func.lower(User.email) == email_n).first():
        return fail("An account with this email already exists. Sign in instead.")

    org = Organization(
        name=org_name.strip(),
        business_type=business_type,
        gst_registered=gst_flag,
        gstin=gstin_n,
        pan=pan.strip().upper(),
        contact_number=contact_number.strip(),
        registered_address=registered_address.strip(),
        city=city.strip(),
        state=state.strip() or "Karnataka",
        pincode=pincode.strip(),
        business_email=business_email.strip().lower(),
        employees_count=0,
        business_turnover=0.0,
    )
    db.add(org)
    db.flush()
    user = User(
        org_id=org.id,
        full_name=full_name.strip(),
        email=email_n,
        password_hash=hash_password(password),
        role="admin",
    )
    db.add(user)
    db.commit()
    db.refresh(org)
    db.refresh(user)
    seed_compliance_calendar(db, org)

    response = RedirectResponse("/select-interface", status_code=303)
    set_session_cookie(response, user.id, org.id, remember=True)
    return response

@router.get("/logout")
@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    clear_ui_mode_cookie(response)
    return response

# ---------------- EXECUTIVE DASHBOARD ----------------
@router.get("/")
def executive_dashboard(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    snap = get_financial_snapshot(db, org)
    upsert_rule_based_recommendations(db, snap)

    if org and (snap["sales_total"] or snap["expenses_total"] or snap["assets"] or snap["employees"]):
        background_tasks.add_task(
            update_ai_insights_and_recommendations_bg,
            org.id, snap["sales_total"], snap["expenses_total"], snap["profit_total"],
            snap["input_gst"], snap["output_gst"]
        )

    recommendations = db.query(TaxRecommendation).filter(TaxRecommendation.org_id == org.id).order_by(TaxRecommendation.estimated_tax_impact.desc()).limit(6).all()
    recent_anomalies = db.query(Anomaly).filter(Anomaly.org_id == org.id).order_by(Anomaly.created_at.desc()).limit(5).all()
    recent_expenses = db.query(Expense).filter(Expense.org_id == org.id).order_by(Expense.date.desc()).limit(5).all()

    response = templates.TemplateResponse(
        request=request,
        name="executive_dashboard.html",
        context=page_ctx(request, db, "dashboard", {
            "org": org,
            "auth_user": user,
            "sales_total": snap["sales_total"],
            "expenses_total": snap["expenses_total"],
            "business_expense_total": snap["business_expense_total"],
            "personal_expense_total": snap["personal_expense_total"],
            "monthly_payroll": snap["monthly_payroll"],
            "profit_total": snap["profit_total"],
            "output_gst": snap["output_gst"],
            "input_gst": snap["input_gst"],
            "eligible_itc": snap["eligible_itc"],
            "blocked_itc": snap["blocked_itc"],
            "net_gst_payable": snap["net_gst_payable"],
            "recommendations": recommendations,
            "opportunities_count": len(recommendations),
            "anomalies_count": db.query(Anomaly).filter(Anomaly.org_id == org.id).count(),
            "compliance_pending": db.query(ComplianceObligation).filter(ComplianceObligation.org_id == org.id, ComplianceObligation.status == "Pending").count(),
            "compliance_overdue": db.query(ComplianceObligation).filter(ComplianceObligation.org_id == org.id, ComplianceObligation.status == "Overdue").count(),
            "recent_anomalies": recent_anomalies,
            "recent_expenses": recent_expenses,
            "expense_by_category": snap["expense_by_category"],
        }, org=org, user=user)
    )
    set_ui_mode_cookie(response, "org")
    return response

# ---------------- POS WEB RUNNER ----------------
@router.get("/pos")
def pos_system(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    response = templates.TemplateResponse(
        request=request,
        name="pos.html",
        context=page_ctx(request, db, "pos", {"ui_mode": "pos"}, org=org, user=user),
    )
    set_ui_mode_cookie(response, "pos")
    return response

# ---------------- TRANSACTIONS ----------------
@router.get("/transactions")
def transactions(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    txs = db.query(Transaction).filter(Transaction.org_id == org.id).order_by(Transaction.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context=page_ctx(request, db, "transactions", {"transactions": txs})
    )

def _parse_sale_lines(tx: Transaction) -> list:
    if tx.category:
        try:
            items = json.loads(tx.category)
            if isinstance(items, list) and items:
                return items
        except (json.JSONDecodeError, TypeError):
            pass
    return [{
        "product": tx.product_name,
        "price": float(tx.price or 0),
        "qty": float(tx.quantity or 0),
        "subtotal": float(tx.price or 0) * float(tx.quantity or 0),
        "gst_pct": f"{tx.gst_percent or 0}%",
        "gst_amt": float(tx.gst_amount or 0),
        "total": float(tx.total_amount or 0),
    }]

def build_sales_payload(db: Session, org: Organization) -> dict:
    txs = (
        db.query(Transaction)
        .filter(Transaction.org_id == org.id)
        .order_by(Transaction.created_at.desc())
        .all()
    )
    if not txs:
        return {
            "empty": True,
            "overview": {
                "total_revenue": 0.0,
                "transaction_count": 0,
                "average_ticket": 0.0,
                "total_gst": 0.0,
                "taxable_sales": 0.0,
            },
            "history": [],
            "daily": [],
            "weekly": [],
            "monthly": [],
            "products": [],
            "tax_by_rate": {},
            "output_gst_split": {"cgst": 0.0, "sgst": 0.0, "igst": 0.0},
        }

    revenue = sum(float(t.total_amount or 0) for t in txs)
    gst_total = sum(float(t.gst_amount or 0) for t in txs)
    taxable = round(revenue - gst_total, 2)
    count = len(txs)
    avg = round(revenue / count, 2) if count else 0.0

    history = []
    product_stats = {}
    daily = {}
    weekly = {}
    monthly = {}

    for t in txs:
        lines = _parse_sale_lines(t)
        created = t.created_at or datetime.utcnow()
        day_key = created.strftime("%Y-%m-%d")
        week_key = created.strftime("%Y-W%W")
        month_key = created.strftime("%Y-%m")
        daily[day_key] = daily.get(day_key, 0.0) + float(t.total_amount or 0)
        weekly[week_key] = weekly.get(week_key, 0.0) + float(t.total_amount or 0)
        monthly[month_key] = monthly.get(month_key, 0.0) + float(t.total_amount or 0)

        line_summaries = []
        for item in lines:
            name = item.get("product") or "Item"
            qty = float(item.get("qty") or 0)
            line_total = float(item.get("total") or 0)
            line_summaries.append(f"{name} × {qty:g}")
            stats = product_stats.setdefault(name, {"quantity": 0.0, "revenue": 0.0})
            stats["quantity"] += qty
            stats["revenue"] += line_total

        history.append({
            "id": t.id,
            "created_at": created.strftime("%Y-%m-%d %H:%M"),
            "products": ", ".join(line_summaries) if line_summaries else (t.product_name or "—"),
            "gst_amount": round(float(t.gst_amount or 0), 2),
            "total_amount": round(float(t.total_amount or 0), 2),
            "taxable": round(float(t.total_amount or 0) - float(t.gst_amount or 0), 2),
            "payment_method": "PoS",
        })

    products = [
        {"name": name, "quantity": round(v["quantity"], 2), "revenue": round(v["revenue"], 2)}
        for name, v in product_stats.items()
    ]
    products.sort(key=lambda x: x["revenue"], reverse=True)

    # Output GST: assume intra-state (CGST/SGST half) unless org sells inter-state only —
    # POS sales historically store combined GST; split 50/50 for display (same as many GST views).
    half = round(gst_total / 2.0, 2)
    rem = round(gst_total - half, 2)

    def series(d: dict, limit=14):
        items = sorted(d.items())[-limit:]
        return [{"label": k, "value": round(v, 2)} for k, v in items]

    return {
        "empty": False,
        "overview": {
            "total_revenue": round(revenue, 2),
            "transaction_count": count,
            "average_ticket": avg,
            "total_gst": round(gst_total, 2),
            "taxable_sales": taxable,
        },
        "history": history,
        "daily": series(daily, 14),
        "weekly": series(weekly, 12),
        "monthly": series(monthly, 12),
        "products": products[:20],
        "tax_by_rate": gst_output_by_rate(txs),
        "output_gst_split": {"cgst": half, "sgst": rem, "igst": 0.0},
    }

@router.get("/sales")
def sales_view(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    payload = build_sales_payload(db, org)
    return templates.TemplateResponse(
        request=request,
        name="sales.html",
        context=page_ctx(request, db, "sales", {"sales": payload}, org=org, user=user),
    )

@router.get("/api/sales/summary")
def sales_summary_api(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return build_sales_payload(db, org)


def _invoice_number(tx_id: int) -> str:
    return f"INV-{int(tx_id):06d}"


def _tx_payment_method(tx: Transaction) -> str:
    # Payment method is not persisted on Transaction historically; POS records as completed sale.
    return "PoS"


def build_pos_intelligence(db: Session, org: Organization, range_key: str = "7", date_from: str = None, date_to: str = None) -> dict:
    """Aggregated POS sales intelligence from real Transaction + Product rows."""
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    txs = (
        db.query(Transaction)
        .filter(Transaction.org_id == org.id)
        .order_by(Transaction.created_at.desc())
        .all()
    )

    def in_range(created: datetime) -> bool:
        if not created:
            return False
        if range_key == "today":
            return created >= today_start
        if range_key == "7":
            return created >= (now - timedelta(days=7))
        if range_key == "30":
            return created >= (now - timedelta(days=30))
        if range_key == "custom" and date_from and date_to:
            try:
                d0 = datetime.strptime(date_from, "%Y-%m-%d")
                d1 = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
                return d0 <= created < d1
            except ValueError:
                return True
        return created >= (now - timedelta(days=7))

    today_txs = [t for t in txs if t.created_at and t.created_at >= today_start]
    range_txs = [t for t in txs if in_range(t.created_at)]

    def summarize(subset):
        revenue = sum(float(t.total_amount or 0) for t in subset)
        gst = sum(float(t.gst_amount or 0) for t in subset)
        count = len(subset)
        items = 0.0
        product_qty = {}
        product_rev = {}
        for t in subset:
            for line in _parse_sale_lines(t):
                name = line.get("product") or "Item"
                qty = float(line.get("qty") or 0)
                total = float(line.get("total") or 0)
                items += qty
                product_qty[name] = product_qty.get(name, 0.0) + qty
                product_rev[name] = product_rev.get(name, 0.0) + total
        best = None
        if product_qty:
            best_name = max(product_qty, key=product_qty.get)
            best = {"name": best_name, "units": round(product_qty[best_name], 2), "revenue": round(product_rev.get(best_name, 0), 2)}
        return {
            "revenue": round(revenue, 2),
            "gst": round(gst, 2),
            "transactions": count,
            "items_sold": round(items, 2),
            "avg_ticket": round(revenue / count, 2) if count else 0.0,
            "best_seller": best,
            "product_qty": product_qty,
            "product_rev": product_rev,
        }

    today = summarize(today_txs)
    ranged = summarize(range_txs)

    # Revenue + transaction daily series for selected range window
    series_days = 1 if range_key == "today" else (7 if range_key == "7" else (30 if range_key == "30" else 14))
    if range_key == "custom" and date_from and date_to:
        try:
            series_days = max(1, (datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")).days + 1)
            series_days = min(series_days, 90)
        except ValueError:
            series_days = 14

    daily_rev = {}
    daily_tx = {}
    for i in range(series_days - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_rev[day] = 0.0
        daily_tx[day] = 0
    for t in range_txs:
        if not t.created_at:
            continue
        day = t.created_at.strftime("%Y-%m-%d")
        if day in daily_rev:
            daily_rev[day] += float(t.total_amount or 0)
            daily_tx[day] += 1

    revenue_trend = [{"label": k, "value": round(v, 2)} for k, v in sorted(daily_rev.items())]
    tx_trend = [{"label": k, "value": v} for k, v in sorted(daily_tx.items())]

    top_by_units = sorted(
        [{"name": n, "units": round(q, 2), "revenue": round(ranged["product_rev"].get(n, 0), 2)}
         for n, q in ranged["product_qty"].items()],
        key=lambda x: x["units"], reverse=True
    )[:10]
    top_by_revenue = sorted(
        [{"name": n, "units": round(ranged["product_qty"].get(n, 0), 2), "revenue": round(r, 2)}
         for n, r in ranged["product_rev"].items()],
        key=lambda x: x["revenue"], reverse=True
    )[:10]

    # Velocity: units per day over last 14 days (need ≥2 distinct sale days)
    vel_start = now - timedelta(days=14)
    vel_qty = {}
    vel_days = {}
    for t in txs:
        if not t.created_at or t.created_at < vel_start:
            continue
        day = t.created_at.strftime("%Y-%m-%d")
        for line in _parse_sale_lines(t):
            name = line.get("product") or "Item"
            qty = float(line.get("qty") or 0)
            vel_qty[name] = vel_qty.get(name, 0.0) + qty
            vel_days.setdefault(name, set()).add(day)
    fastest = []
    for name, qty in vel_qty.items():
        days = len(vel_days.get(name) or [])
        if days < 2:
            continue
        fastest.append({
            "name": name,
            "units": round(qty, 2),
            "days": days,
            "units_per_day": round(qty / days, 2),
        })
    fastest.sort(key=lambda x: x["units_per_day"], reverse=True)
    fastest = fastest[:10]
    fastest_today = fastest[0] if fastest else None

    recent = []
    for t in txs[:8]:
        lines = _parse_sale_lines(t)
        created = t.created_at or now
        recent.append({
            "id": t.id,
            "invoice_number": _invoice_number(t.id),
            "created_at": created.strftime("%Y-%m-%d %H:%M"),
            "time": created.strftime("%H:%M"),
            "products": ", ".join(f"{x.get('product')} × {float(x.get('qty') or 0):g}" for x in lines) or (t.product_name or "—"),
            "total": round(float(t.total_amount or 0), 2),
            "gst": round(float(t.gst_amount or 0), 2),
        })

    low_stock = []
    for p in db.query(Product).filter(Product.org_id == org.id, Product.stock < 10).order_by(Product.stock.asc()).limit(12).all():
        stock = float(p.stock or 0)
        low_stock.append({
            "name": p.name,
            "stock": stock,
            "status": "critical" if stock <= 3 else "low",
        })

    product_names = sorted(set(list(ranged["product_qty"].keys()) + [p.name for p in db.query(Product).filter(Product.org_id == org.id).all() if p.name]))

    empty = len(txs) == 0
    return {
        "empty": empty,
        "range": range_key,
        "today": {
            "revenue": today["revenue"],
            "transactions": today["transactions"],
            "items_sold": today["items_sold"],
            "avg_ticket": today["avg_ticket"],
            "gst": today["gst"],
            "best_seller": today["best_seller"],
            "fastest_moving": fastest_today,
        },
        "revenue_trend": revenue_trend,
        "tx_trend": tx_trend,
        "top_selling": top_by_units,
        "top_revenue": top_by_revenue,
        "fastest_moving": fastest,
        "distribution": [{"name": x["name"], "value": x["revenue"]} for x in top_by_revenue[:8]],
        "recent": recent,
        "low_stock": low_stock,
        "product_names": product_names,
        "has_velocity": len(fastest) > 0,
    }


def build_pos_history(db: Session, org: Organization, q: str = "", date_from: str = None, date_to: str = None,
                      payment: str = "", page: int = 1, page_size: int = 20) -> dict:
    page = max(1, int(page or 1))
    page_size = min(50, max(5, int(page_size or 20)))
    query = db.query(Transaction).filter(Transaction.org_id == org.id)
    if date_from:
        try:
            query = query.filter(Transaction.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Transaction.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass
    txs = query.order_by(Transaction.created_at.desc()).all()
    qn = (q or "").strip().lower()
    rows = []
    for t in txs:
        lines = _parse_sale_lines(t)
        inv = _invoice_number(t.id)
        products = ", ".join(f"{x.get('product')} × {float(x.get('qty') or 0):g}" for x in lines) or (t.product_name or "")
        pay = _tx_payment_method(t)
        if qn and qn not in inv.lower() and qn not in products.lower() and qn not in str(t.id):
            continue
        if payment and payment.lower() not in ("", "all", "pos") and payment.lower() != pay.lower():
            continue
        created = t.created_at or datetime.utcnow()
        gst = float(t.gst_amount or 0)
        half = round(gst / 2.0, 2)
        rem = round(gst - half, 2)
        rows.append({
            "id": t.id,
            "invoice_number": inv,
            "date": created.strftime("%Y-%m-%d"),
            "time": created.strftime("%H:%M:%S"),
            "created_at": created.strftime("%Y-%m-%d %H:%M"),
            "products": products,
            "lines": lines,
            "subtotal": round(float(t.total_amount or 0) - gst, 2),
            "cgst": half,
            "sgst": rem,
            "igst": 0.0,
            "total_tax": round(gst, 2),
            "grand_total": round(float(t.total_amount or 0), 2),
            "payment_method": pay,
            "status": "Completed",
        })
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size) if total else 1,
        "items": page_rows,
    }


def build_invoice_detail(db: Session, org: Organization, transaction_id: int) -> dict:
    t = db.query(Transaction).filter(Transaction.id == transaction_id, Transaction.org_id == org.id).first()
    if not t:
        return None
    lines = _parse_sale_lines(t)
    created = t.created_at or datetime.utcnow()
    gst = float(t.gst_amount or 0)
    half = round(gst / 2.0, 2)
    rem = round(gst - half, 2)
    addr_parts = [org.registered_address or "", org.city or "", org.state or "", org.pincode or ""]
    address = ", ".join(p for p in addr_parts if p)
    return {
        "id": t.id,
        "invoice_number": _invoice_number(t.id),
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "date": created.strftime("%Y-%m-%d"),
        "time": created.strftime("%H:%M:%S"),
        "organization": {
            "name": org.name,
            "gstin": org.gstin or "",
            "address": address,
            "contact": org.contact_number or "",
            "email": org.business_email or "",
            "state": org.state or "",
        },
        "customer": {"name": "Walk-in Customer"},
        "lines": lines,
        "subtotal": round(float(t.total_amount or 0) - gst, 2),
        "discount": 0.0,
        "cgst": half,
        "sgst": rem,
        "igst": 0.0,
        "total_tax": round(gst, 2),
        "grand_total": round(float(t.total_amount or 0), 2),
        "payment_method": _tx_payment_method(t),
        "status": "Completed",
        "download_url": f"/download_invoice/{t.id}",
    }


def build_product_analytics(db: Session, org: Organization, product_name: str) -> dict:
    name_q = (product_name or "").strip().lower()
    if not name_q:
        return {"empty": True, "message": "Select a product"}
    txs = (
        db.query(Transaction)
        .filter(Transaction.org_id == org.id)
        .order_by(Transaction.created_at.asc())
        .all()
    )
    units = 0.0
    revenue = 0.0
    tx_count = 0
    daily_units = {}
    daily_rev = {}
    recent = []
    for t in txs:
        matched = []
        for line in _parse_sale_lines(t):
            pname = (line.get("product") or "").strip()
            if pname.lower() != name_q:
                continue
            qty = float(line.get("qty") or 0)
            tot = float(line.get("total") or 0)
            matched.append(line)
            units += qty
            revenue += tot
        if not matched:
            continue
        tx_count += 1
        created = t.created_at or datetime.utcnow()
        day = created.strftime("%Y-%m-%d")
        qsum = sum(float(m.get("qty") or 0) for m in matched)
        rsum = sum(float(m.get("total") or 0) for m in matched)
        daily_units[day] = daily_units.get(day, 0.0) + qsum
        daily_rev[day] = daily_rev.get(day, 0.0) + rsum
        recent.append({
            "id": t.id,
            "invoice_number": _invoice_number(t.id),
            "created_at": created.strftime("%Y-%m-%d %H:%M"),
            "qty": round(qsum, 2),
            "total": round(rsum, 2),
        })
    recent.reverse()
    if tx_count == 0:
        return {"empty": True, "message": "Not enough sales history yet.", "name": product_name}
    days = sorted(daily_units.keys())
    return {
        "empty": False,
        "name": product_name,
        "units_sold": round(units, 2),
        "revenue": round(revenue, 2),
        "transactions": tx_count,
        "avg_qty_per_tx": round(units / tx_count, 2) if tx_count else 0,
        "units_trend": [{"label": d, "value": round(daily_units[d], 2)} for d in days[-30:]],
        "revenue_trend": [{"label": d, "value": round(daily_rev[d], 2)} for d in days[-30:]],
        "recent": recent[:15],
        "enough_history": len(days) >= 2,
    }


@router.get("/api/pos/intelligence")
def pos_intelligence_api(
    request: Request,
    range_key: str = "7",
    date_from: str = None,
    date_to: str = None,
    db: Session = Depends(get_db),
):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Accept both ?range= and ?range_key=
    q = request.query_params
    rk = q.get("range") or q.get("range_key") or range_key or "7"
    return build_pos_intelligence(db, org, range_key=rk, date_from=date_from, date_to=date_to)


@router.get("/api/pos/history")
def pos_history_api(
    request: Request,
    q: str = "",
    date_from: str = None,
    date_to: str = None,
    payment: str = "",
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return build_pos_history(db, org, q=q, date_from=date_from, date_to=date_to, payment=payment, page=page, page_size=page_size)


@router.get("/api/pos/invoice/{transaction_id}")
def pos_invoice_api(request: Request, transaction_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    detail = build_invoice_detail(db, org, transaction_id)
    if not detail:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return detail


@router.get("/api/pos/product")
def pos_product_api(request: Request, name: str = "", db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return build_product_analytics(db, org, name)

# ---------------- SUMMARY ----------------
@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    txs = db.query(Transaction).filter(Transaction.org_id == org.id).all()
    total_sales = sum(t.total_amount for t in txs)
    total_gst = sum(t.gst_amount for t in txs)

    gst_labels = []
    gst_values = []
    sales_values = []
    aggregated_data = {}

    for t in txs:
        if t.category:
            try:
                items = json.loads(t.category)
                for item in items:
                    product = item['product'].strip().lower().title()
                    gst_amt = float(item.get('gst_amt', 0))
                    total = float(item.get('total', 0))
                    
                    if product not in aggregated_data:
                        aggregated_data[product] = {'gst': 0.0, 'sales': 0.0}
                    aggregated_data[product]['gst'] += gst_amt
                    aggregated_data[product]['sales'] += total
            except json.JSONDecodeError:
                product = t.product_name.strip().lower().title()
                if product not in aggregated_data:
                    aggregated_data[product] = {'gst': 0.0, 'sales': 0.0}
                aggregated_data[product]['gst'] += float(t.gst_amount)
                aggregated_data[product]['sales'] += float(t.total_amount)
        else:
            product = t.product_name.strip().lower().title()
            if product not in aggregated_data:
                aggregated_data[product] = {'gst': 0.0, 'sales': 0.0}
            aggregated_data[product]['gst'] += float(t.gst_amount)
            aggregated_data[product]['sales'] += float(t.total_amount)

    for product, data in aggregated_data.items():
        gst_labels.append(product)
        gst_values.append(round(data['gst'], 2))
        sales_values.append(round(data['sales'], 2))

    return templates.TemplateResponse(
        request=request,
        name="summary.html",
        context=page_ctx(request, db, "summary", {
            "total_sales": round(total_sales, 2),
            "total_gst": round(total_gst, 2),
            "transaction_count": len(txs),
            "gst_labels": gst_labels,
            "gst_values": gst_values,
            "sales_values": sales_values,
        })
    )

def collect_rag_rules(question: str) -> str:
    rules_context = ""
    rules_path = str(tax_rules_path())
    if not os.path.exists(rules_path):
        return ""
    try:
        with open(rules_path, "r") as f:
            rules = json.load(f)
        q_lower = question.lower()
        matching_rules = []
        gst = rules.get("gst_rules", {})
        if "gst" in q_lower or "itc" in q_lower or "input" in q_lower or "credit" in q_lower:
            matching_rules.append(f"GST Eligibility (Section 16): {json.dumps(gst.get('itc_eligibility'))}")
            matching_rules.append(f"GST Blocked Credits (Section 17(5)): {json.dumps(gst.get('blocked_credits'))}")
        it = rules.get("income_tax_rules", {})
        if "tax" in q_lower or "rate" in q_lower or "corporate" in q_lower or "regime" in q_lower:
            matching_rules.append(f"Corporate Tax Rates: {json.dumps(it.get('corporate_tax_rates'))}")
        if "80jj" in q_lower or "employee" in q_lower or "hire" in q_lower or "deduction" in q_lower or "payroll" in q_lower:
            matching_rules.append(f"Section 80JJAA Employment Deduction: {json.dumps(it.get('deductions', {}).get('section_80jjaa'))}")
        if "depreciation" in q_lower or "asset" in q_lower or "machine" in q_lower or "computers" in q_lower or "furniture" in q_lower:
            matching_rules.append(f"Section 32 Depreciation WDV Rates: {json.dumps(it.get('depreciation_rates'))}")
        if matching_rules:
            rules_context = "\n".join(matching_rules)
    except Exception as e:
        logging.error(f"Failed to load RAG rules context: {e}")
    return rules_context


ASSISTANT_PAGES = {
    "dashboard": {"path": "/", "label": "Dashboard", "focus": "live P&L, GST payable, and recent books"},
    "pos": {"path": "/pos", "label": "PoS checkout", "focus": "recording taxable sales, GST on supplies, and stock deduction"},
    "sales": {"path": "/sales", "label": "Sales", "focus": "sales history, revenue trends, product performance, and GST collected on supplies"},
    "expenses": {"path": "/expenses", "label": "Expenses", "focus": "recording vendor bills, GST ITC, business vs personal, revenue vs capital"},
    "employees": {"path": "/employees", "label": "Payroll", "focus": "employee master, monthly CTC, EPF, and Section 80JJAA"},
    "assets": {"path": "/assets", "label": "Fixed Assets", "focus": "capitalisation and Section 32 depreciation"},
    "gst": {"path": "/gst", "label": "GST & ITC", "focus": "output tax, eligible ITC, blocked credits, net GST payable"},
    "income-tax": {"path": "/income-tax", "label": "Income Tax", "focus": "taxable profit and old vs new regime estimates"},
    "tax-optimization": {"path": "/tax-optimization", "label": "AI Optimization", "focus": "actionable tax and GST opportunities from the books, documents needed, next steps, and related AICA modules"},
    "compliance": {"path": "/compliance", "label": "Compliance", "focus": "return due dates and filing calendar"},
    "forecasting": {"path": "/forecasting", "label": "Forecasting", "focus": "forward-looking sales and expense trends"},
    "what-if": {"path": "/what-if", "label": "What-If Simulator", "focus": "scenario modelling"},
    "reports": {"path": "/reports", "label": "Reports", "focus": "exporting and reviewing ledgers"},
    "warehouse": {"path": "/warehouse", "label": "Warehouse", "focus": "inventory quantities and prices for this organisation"},
    "organization": {"path": "/organization", "label": "Organisation profile", "focus": "GSTIN, PAN, regime, and company identity"},
    "profile": {"path": "/profile", "label": "Profile", "focus": "the signed-in user's name and email"},
    "chat": {"path": "/", "label": "Assistant", "focus": "general accounting questions"},
}

ALLOWED_NAV_PATHS = {meta["path"] for meta in ASSISTANT_PAGES.values()} | {
    "/income-tax", "/tax-optimization", "/employees", "/gst", "/sales", "/pos", "/select-interface"
}


def parse_nav_request(answer: str):
    if not answer:
        return answer, None
    match = re.search(r"NAV_REQUEST:\s*(\{.*\})\s*$", answer.strip(), re.DOTALL)
    if not match:
        return answer, None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return answer, None
    path = str(payload.get("path") or "").strip()
    if path not in ALLOWED_NAV_PATHS:
        return re.sub(r"\n*NAV_REQUEST:\s*\{.*\}\s*$", "", answer, flags=re.DOTALL).strip(), None
    cleaned = re.sub(r"\n*NAV_REQUEST:\s*\{.*\}\s*$", "", answer, flags=re.DOTALL).strip()
    return cleaned, {
        "path": path,
        "label": payload.get("label") or path,
        "reason": payload.get("reason") or "This step lives on another screen.",
    }


# ---------------- CHAT ASSISTANT RAG FLOW ----------------
@router.get("/chat")
def chat(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/", status_code=303)

@router.post("/chat")
def chat_post(request: Request, question: str = Form(...), db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    oid = org.id if org else None
    rules_context = collect_rag_rules(question)
    def run_org_query(sql: str) -> str:
        return run_db_query(sql, oid)

    answer = query_gemini_assistant(
        question, get_db_schema, run_org_query, rules_context
    )
    return {"answer": answer}

@router.post("/api/assistant")
def assistant_api(
    request: Request,
    question: str = Form(...),
    page: str = Form("dashboard"),
    path: str = Form("/"),
    task: str = Form(""),
    history: str = Form("[]"),
    opt_context: str = Form(""),
    db: Session = Depends(get_db),
):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"error": "Please sign in again."}, status_code=401)
    page_meta = ASSISTANT_PAGES.get(page) or ASSISTANT_PAGES["dashboard"]
    snap = get_financial_snapshot(db, org)
    try:
        history_list = json.loads(history) if history else []
        if not isinstance(history_list, list):
            history_list = []
    except json.JSONDecodeError:
        history_list = []

    extra = (
        "You are IRA (Intelligent Revenue Assistant), the voice and chat assistant inside AICA — "
        "the AI Chartered Accountant platform. Speak as IRA, not as a generic chatbot.\n"
        f"CURRENT SCREEN: {page_meta['label']} ({path or page_meta['path']}). "
        f"You are helping with {page_meta['focus']}. Stay on this context unless the user needs another module.\n"
        f"ORGANISATION: {org.name}. GSTIN: {org.gstin or 'not set'}. State: {org.state}.\n"
        f"{INR_UNIT_LOCK}\n"
        "STRUCTURED FINANCE (absolute INR — source of truth; do not rescale):\n"
        f"{json.dumps(snap.get('finance') or structured_finance_block(snap))}\n"
        f"SQL: every query on products, transactions, expenses, employees, assets, compliance, recommendations or anomalies MUST include WHERE org_id = {org.id}. Never read other organisations.\n"
        "You may explain fields and tax implications. You must NOT claim you submitted a form, created a bill, deleted a record, or changed books. "
        "For any request that would modify financial data, ask the user to confirm in the UI before describing steps.\n"
        "Never call a tax liability a benefit or saving. If asked what they owe, use estimated_tax_liability / net_gst payable fields.\n"
        "Do not navigate the user yourself. If another AICA screen is required to finish their request, explain why, ask permission in the reply, then end with exactly one line:\n"
        'NAV_REQUEST: {"path":"/employees","label":"Payroll","reason":"short reason"}\n'
        f"Allowed paths: {sorted(ALLOWED_NAV_PATHS)}.\n"
        "If they are already on the right screen, do not emit NAV_REQUEST."
    )
    if task.strip():
        extra += f"\nONGOING TASK (preserve this until done): {task.strip()}\n"

    opt_payload = None
    if opt_context.strip():
        try:
            opt_payload = json.loads(opt_context)
        except json.JSONDecodeError:
            opt_payload = None
    if isinstance(opt_payload, dict) and opt_payload.get("title"):
        docs = opt_payload.get("documents") or []
        if isinstance(docs, list):
            doc_lines = ", ".join(
                (d.get("label") if isinstance(d, dict) else str(d)) for d in docs[:12]
            )
        else:
            doc_lines = str(docs)
        steps = opt_payload.get("next_steps") or []
        step_txt = " | ".join(str(s) for s in steps[:8]) if isinstance(steps, list) else str(steps)
        internal = opt_payload.get("internal") or {}
        external = opt_payload.get("external") or {}
        extra += (
            "\nSELECTED AI OPTIMIZATION RECOMMENDATION (user opened Ask IRA from this card):\n"
            f"- Title: {opt_payload.get('title')}\n"
            f"- Section: {opt_payload.get('rule_section')}\n"
            f"- Category: {opt_payload.get('category')}\n"
            f"- Detected: {opt_payload.get('detected_item')}\n"
            f"- Why recommended: {opt_payload.get('why_aica') or opt_payload.get('reason')}\n"
            f"- Eligibility: {opt_payload.get('eligibility_conditions')}\n"
            f"- Estimated amount ({opt_payload.get('impact_type') or 'impact'} — NOT a guaranteed figure): "
            f"Rs {opt_payload.get('estimated_tax_impact')} absolute INR\n"
            f"- Impact label: {opt_payload.get('impact_label')}\n"
            f"- Documents: {doc_lines}\n"
            f"- Next steps: {step_txt}\n"
            f"- Internal AICA path (if any): {(internal or {}).get('path') or 'none'}\n"
            f"- Official portal (if any): {(external or {}).get('url') or 'none'} ({(external or {}).get('label') or ''})\n"
            "Explain this recommendation in plain language for a business owner. "
            "Distinguish estimated potential benefit from confirmed savings. "
            "Never claim AICA filed or submitted anything. "
            "If the user asks what to do next, use the next steps above. "
            "If they ask to open an AICA page and an internal path exists, ask permission then emit NAV_REQUEST. "
            "If they ask for an official portal, name the portal and URL from the context — do not invent other URLs.\n"
        )

    extra += (
        "If the user just arrived after agreeing to open this page, continue the ongoing task immediately "
        "and tell them what to complete here."
    )

    def run_org_query(sql: str) -> str:
        return run_db_query(sql, org.id)

    # Hard wall-clock limit so desktop/WebView never hangs on Gemini quota/retries.
    import concurrent.futures

    def _run_ira():
        return query_gemini_assistant(
            question,
            get_db_schema,
            run_org_query,
            collect_rag_rules(question),
            extra_system=extra,
            history=history_list,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run_ira)
            answer = fut.result(timeout=float(os.environ.get("AICA_IRA_TIMEOUT_S", "25")))
    except concurrent.futures.TimeoutError:
        logging.warning("IRA overall timeout — returning controlled unavailable message")
        answer = IRA_UNAVAILABLE_MSG
    except Exception:
        logging.exception("IRA request failed")
        answer = IRA_UNAVAILABLE_MSG

    cleaned, nav = parse_nav_request(answer)
    return {"answer": cleaned, "navigation": nav}

@router.get("/profile")
def profile_view(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    from backend.runtime_paths import app_release_info
    release = app_release_info()
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context=page_ctx(request, db, "profile", {
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "app_version": release.get("version"),
            "app_build": release.get("build"),
            "app_channel": release.get("channel"),
        }, org=org, user=user)
    )

@router.post("/api/profile")
def profile_update(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    preferred_language: str = Form("en"),
    db: Session = Depends(get_db),
):
    user, org = current_user_org(request, db)
    if not user or not org:
        return login_redirect()
    name = full_name.strip()
    email_n = email.strip().lower()
    if not name:
        return RedirectResponse("/profile?error=" + quote("Enter your name."), status_code=303)
    if not valid_email(email_n):
        return RedirectResponse("/profile?error=" + quote("Enter a valid email."), status_code=303)
    taken = db.query(User).filter(func.lower(User.email) == email_n, User.id != user.id).first()
    if taken:
        return RedirectResponse("/profile?error=" + quote("That email is already used by another account."), status_code=303)
    lang = (preferred_language or "en").strip().lower()
    if lang not in ("en", "kn", "hi"):
        lang = "en"
    user.full_name = name
    user.email = email_n
    user.phone = phone.strip()
    user.preferred_language = lang
    db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)

@router.post("/api/language")
def set_language_api(request: Request, language: str = Form("en"), db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not user or not org:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    lang = (language or "en").strip().lower()
    if lang not in ("en", "kn", "hi"):
        lang = "en"
    user.preferred_language = lang
    db.commit()
    return {"ok": True, "language": lang}

# ---------------- TAX DETAILS ----------------
@router.get("/tax_details")
def tax_details(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    txs = db.query(Transaction).filter(Transaction.org_id == org.id).all()
    tax_splits = {}
    
    for t in txs:
        if t.category:
            try:
                items = json.loads(t.category)
                for item in items:
                    pct = float(str(item['gst_pct']).strip('%'))
                    if pct not in tax_splits:
                        tax_splits[pct] = {"amount": 0, "products": set()}
                    tax_splits[pct]["amount"] += item['gst_amt']
                    tax_splits[pct]["products"].add(item['product'])
            except json.JSONDecodeError:
                pct = t.gst_percent
                if pct not in tax_splits:
                    tax_splits[pct] = {"amount": 0, "products": set()}
                tax_splits[pct]["amount"] += t.gst_amount
                tax_splits[pct]["products"].add(t.product_name)
        else:
            pct = t.gst_percent
            if pct not in tax_splits:
                tax_splits[pct] = {"amount": 0, "products": set()}
            tax_splits[pct]["amount"] += t.gst_amount
            tax_splits[pct]["products"].add(t.product_name)

    for pct in tax_splits:
        tax_splits[pct]["products"] = list(tax_splits[pct]["products"])
        tax_splits[pct]["amount"] = round(tax_splits[pct]["amount"], 2)

    return templates.TemplateResponse(
        request=request,
        name="tax_details.html",
        context=page_ctx(request, db, "tax_details", {"tax_splits": tax_splits})
    )

# ---------------- CART CHECKOUT & INVOICING ----------------
class CartItem(BaseModel):
    product: str
    price: float
    quantity: float
    # When set, checkout ignores client price/qty and uses authoritative WeighTicket snapshots.
    weigh_ticket_token: Optional[str] = None

from fpdf import FPDF

def _pdf_text(value) -> str:
    text_val = "" if value is None else str(value)
    return text_val.encode("latin-1", "replace").decode("latin-1")

def create_pdf_bytes(items_list, date_str, grand_total, total_tax, invoice_id="N/A"):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, "AICA Smart GST Invoice", 0, 1, "C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 10, _pdf_text(f"Date: {date_str}"), 0, 1)
    pdf.cell(0, 10, _pdf_text(f"Invoice No: {invoice_id}"), 0, 1)
    pdf.ln(8)

    pdf.set_font("Helvetica", "B", 11)
    col_widths = [50, 25, 15, 25, 20, 25, 25]
    headers = ["Product", "Price", "Qty", "Subtotal", "GST %", "Tax", "Total"]
    for i in range(len(headers)):
        pdf.cell(col_widths[i], 10, headers[i], 1, 0, "C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for item in items_list:
        qty_val = item.get("qty", 0)
        if isinstance(qty_val, float):
            qty_str = f"{qty_val:.2f}".rstrip("0").rstrip(".") or "0"
        else:
            qty_str = str(qty_val)
        pdf.cell(col_widths[0], 10, _pdf_text(str(item.get("product", ""))[:25]), 1, 0)
        pdf.cell(col_widths[1], 10, f"Rs {float(item.get('price') or 0):.2f}", 1, 0, "C")
        pdf.cell(col_widths[2], 10, qty_str, 1, 0, "C")
        pdf.cell(col_widths[3], 10, f"Rs {float(item.get('subtotal') or 0):.2f}", 1, 0, "C")
        pdf.cell(col_widths[4], 10, _pdf_text(str(item.get("gst_pct", ""))), 1, 0, "C")
        pdf.cell(col_widths[5], 10, f"Rs {float(item.get('gst_amt') or 0):.2f}", 1, 0, "C")
        pdf.cell(col_widths[6], 10, f"Rs {float(item.get('total') or 0):.2f}", 1, 0, "C")
        pdf.ln()

    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(135, 10, "Total Tax:", 0, 0, "R")
    pdf.cell(50, 10, f"Rs {float(total_tax):.2f}", 0, 1, "C")
    pdf.cell(135, 10, "Grand Total:", 0, 0, "R")
    pdf.cell(50, 10, f"Rs {float(grand_total):.2f}", 0, 1, "C")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        return raw.encode("latin-1")
    return bytes(raw)

@router.post("/add_multiple")
def add_multiple_transactions(request: Request, items: List[CartItem] = Body(...), db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"error": "Please sign in again to complete this sale."}, status_code=401)
    if not items:
        return JSONResponse({"error": "Cart is empty."}, status_code=400)

    prepared = []
    seen_ticket_tokens = set()
    try:
        for item in items:
            token = (item.weigh_ticket_token or "").strip() or None

            if token:
                if token in seen_ticket_tokens:
                    db.rollback()
                    return JSONResponse(
                        {
                            "error": "This QR ticket is already in the cart.",
                            "code": "duplicate_ticket",
                        },
                        status_code=400,
                    )
                seen_ticket_tokens.add(token)

                try:
                    resolved = resolve_weigh_ticket(db, org_id=org.id, public_token=token)
                except WeighTicketError as e:
                    db.rollback()
                    return JSONResponse(
                        {"error": e.message, "code": e.code},
                        status_code=resolve_error_http_status(e.code),
                    )

                ticket = resolved.ticket
                product = resolved.product
                qty = float(ticket.weight)
                unit_price = float(ticket.unit_price_snapshot)
                # Authoritative pre-GST line amount from ticket snapshots (not client).
                subtotal = float(ticket.total_amount_snapshot)
                if qty <= 0:
                    db.rollback()
                    return JSONResponse({"error": "Invalid weigh ticket weight."}, status_code=400)

                available = float(product.stock or 0)
                if available < qty:
                    qty_label = int(available) if available == int(available) else round(available, 2)
                    db.rollback()
                    return JSONResponse(
                        {
                            "error": f"Insufficient stock for {product.name}. Available quantity: {qty_label}.",
                            "code": "insufficient_stock",
                        },
                        status_code=400,
                    )

                gst = classify_gst(product.name)
                gst_amt = round(subtotal * gst / 100, 2)
                total = round(subtotal + gst_amt, 2)
                prepared.append({
                    "product_row": product,
                    "qty": qty,
                    "weigh_ticket_token": token,
                    "pdf": {
                        "product": ticket.product_name_snapshot,
                        "price": unit_price,
                        "qty": qty,
                        "subtotal": subtotal,
                        "gst_pct": f"{gst}%",
                        "gst_amt": gst_amt,
                        "total": total,
                        "source": "weigh_ticket",
                        "weigh_ticket_id": ticket.id,
                        "unit": ticket.unit,
                    },
                })
                continue

            if item.quantity is None or item.quantity <= 0:
                db.rollback()
                return JSONResponse({"error": "Enter a valid quantity greater than zero."}, status_code=400)
            if item.price is None or item.price < 0:
                db.rollback()
                return JSONResponse({"error": f"Invalid price for {item.product}."}, status_code=400)
            product = db.query(Product).filter(
                Product.org_id == org.id,
                func.lower(Product.name) == item.product.strip().lower()
            ).first()
            if not product:
                db.rollback()
                return JSONResponse({"error": f"Product not found: {item.product}."}, status_code=400)
            try:
                qty = validate_sale_quantity(product_type_of(product), item.quantity)
            except ProductTypeError as e:
                db.rollback()
                return JSONResponse({"error": e.message, "code": e.code}, status_code=400)
            available = float(product.stock or 0)
            if available < qty:
                qty_label = int(available) if available == int(available) else round(available, 2)
                db.rollback()
                return JSONResponse(
                    {"error": f"Insufficient stock for {product.name}. Available quantity: {qty_label}."},
                    status_code=400
                )
            gst = classify_gst(product.name)
            subtotal = float(item.price) * qty
            gst_amt = round(subtotal * gst / 100, 2)
            total = round(subtotal + gst_amt, 2)
            prepared.append({
                "product_row": product,
                "qty": qty,
                "weigh_ticket_token": None,
                "pdf": {
                    "product": product.name,
                    "price": float(item.price),
                    "qty": qty,
                    "subtotal": subtotal,
                    "gst_pct": f"{gst}%",
                    "gst_amt": gst_amt,
                    "total": total,
                },
            })

        # Aggregate demand per product (mixed normal + ticket lines).
        demand: Dict[int, float] = {}
        for row in prepared:
            pid = int(row["product_row"].id)
            demand[pid] = demand.get(pid, 0.0) + float(row["qty"])
        for pid, need in demand.items():
            product_row = next(r["product_row"] for r in prepared if int(r["product_row"].id) == pid)
            available = float(product_row.stock or 0)
            if available < need:
                db.rollback()
                qty_label = int(available) if available == int(available) else round(available, 2)
                return JSONResponse(
                    {
                        "error": f"Insufficient stock for {product_row.name}. Available quantity: {qty_label}.",
                        "code": "insufficient_stock",
                    },
                    status_code=400,
                )

        grand_total = sum(p["pdf"]["total"] for p in prepared)
        total_tax = sum(p["pdf"]["gst_amt"] for p in prepared)
        pdf_items = [p["pdf"] for p in prepared]

        for row in prepared:
            row["product_row"].stock = float(row["product_row"].stock or 0) - row["qty"]
            if row["product_row"].stock < -1e-9:
                db.rollback()
                return JSONResponse({"error": "Unable to complete the sale. Please try again."}, status_code=409)

        transaction = Transaction(
            org_id=org.id,
            product_name=f"Bill ({len(prepared)} items)",
            price=0.0,
            quantity=0.0,
            gst_percent=0.0,
            category=json.dumps(pdf_items),
            gst_amount=total_tax,
            total_amount=grand_total,
        )
        db.add(transaction)
        db.flush()  # allocate transaction.id before consuming tickets

        for row in prepared:
            token = row.get("weigh_ticket_token")
            if not token:
                continue
            try:
                claim_active_ticket_for_checkout(
                    db,
                    org_id=org.id,
                    public_token=token,
                    transaction_id=transaction.id,
                )
            except WeighTicketError as e:
                db.rollback()
                return JSONResponse(
                    {"error": e.message, "code": e.code},
                    status_code=resolve_error_http_status(e.code),
                )

        db.commit()
        db.refresh(transaction)
    except Exception:
        db.rollback()
        logging.exception("POS checkout failed before invoice")
        return JSONResponse({"error": "Unable to complete the sale. Please try again."}, status_code=500)

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"invoice_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    try:
        pdf_bytes = create_pdf_bytes(pdf_items, date_str, round(grand_total, 2), round(total_tax, 2), _invoice_number(transaction.id))
    except Exception:
        logging.exception("Invoice PDF failed after committed sale")
        return JSONResponse({
            "ok": True,
            "transaction_id": transaction.id,
            "message": "Sale saved. Invoice could not be generated; download it from Reports if needed.",
        })

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.get("/download_invoice/{transaction_id}")
def download_invoice(request: Request, transaction_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        accept = (request.headers.get("accept") or "").lower()
        if "application/pdf" in accept or "application/json" in accept:
            return JSONResponse({"error": "Please sign in again."}, status_code=401)
        return login_redirect()
    t = db.query(Transaction).filter(Transaction.id == transaction_id, Transaction.org_id == org.id).first()
    if not t:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
        
    date_str = t.created_at.strftime('%Y-%m-%d %H:%M:%S') if t.created_at else "N/A"
    
    if t.category:
        try:
            pdf_items = json.loads(t.category)
        except json.JSONDecodeError:
            subtotal = t.price * t.quantity
            pdf_items = [{
                "product": t.product_name,
                "price": t.price,
                "qty": t.quantity,
                "subtotal": round(subtotal, 2),
                "gst_pct": f"{t.gst_percent}%",
                "gst_amt": t.gst_amount,
                "total": t.total_amount
            }]
    else:
        subtotal = t.price * t.quantity
        pdf_items = [{
            "product": t.product_name,
            "price": t.price,
            "qty": t.quantity,
            "subtotal": round(subtotal, 2),
            "gst_pct": f"{t.gst_percent}%",
            "gst_amt": t.gst_amount,
            "total": t.total_amount
        }]
    
    pdf_bytes = create_pdf_bytes(pdf_items, date_str, round(t.total_amount, 2), round(t.gst_amount, 2), _invoice_number(t.id))
    filename = f"{_invoice_number(t.id)}.pdf"
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# ---------------- WAREHOUSE MANAGEMENT ----------------
@router.get("/warehouse")
def warehouse(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    products = db.query(Product).filter(Product.org_id == org.id).order_by(Product.name).all()
    return templates.TemplateResponse(
        request=request,
        name="warehouse.html",
        context=page_ctx(request, db, "warehouse", {"products": products}, org=org, user=user)
    )


@router.get("/weigh")
def weigh_page(request: Request, db: Session = Depends(get_db)):
    """Weigh & QR workspace — generate ACTIVE QR weigh tickets (no stock deduction)."""
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    products = (
        db.query(Product)
        .filter(Product.org_id == org.id, Product.product_type == PRODUCT_TYPE_LOOSE)
        .order_by(Product.name)
        .all()
    )
    response = templates.TemplateResponse(
        request=request,
        name="weigh.html",
        context=page_ctx(request, db, "weigh", {"products": products, "ui_mode": "weigh"}, org=org, user=user),
    )
    set_ui_mode_cookie(response, "weigh")
    return response


def _org_weigh_ticket_or_404(db: Session, org_id: int, ticket_id: int) -> WeighTicket:
    ticket = (
        db.query(WeighTicket)
        .filter(WeighTicket.id == int(ticket_id), WeighTicket.org_id == int(org_id))
        .first()
    )
    if not ticket:
        raise WeighTicketError("not_found", "Weigh ticket not found.")
    return ticket


@router.get("/api/weigh-tickets/{ticket_id}/qr.png")
def api_weigh_ticket_qr_png(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        ticket = _org_weigh_ticket_or_404(db, org.id, ticket_id)
    except WeighTicketError as e:
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    png = qr_png_bytes(ticket.public_token)
    return Response(content=png, media_type="image/png")


@router.get("/api/weigh-tickets/{ticket_id}/label.pdf")
def api_weigh_ticket_label_pdf(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        ticket = _org_weigh_ticket_or_404(db, org.id, ticket_id)
        pdf_bytes = create_weigh_label_pdf(ticket)
    except WeighTicketError as e:
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    filename = f"aica_weigh_label_{ticket.id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@router.post("/add_product")
def add_product(
    request: Request,
    name: str = Form(...),
    price: float = Form(...),
    stock: float = Form(...),
    product_type: str = Form(...),
    db: Session = Depends(get_db)
):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()

    def _warehouse_error(msg: str):
        return RedirectResponse(f"/warehouse?error={quote(msg)}", status_code=303)

    normalized_name = name.strip().title()
    if not normalized_name:
        return _warehouse_error("Enter a product name.")
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return _warehouse_error("Enter a valid price.")
    if price_f < 0:
        return _warehouse_error("Price cannot be negative.")

    try:
        ptype = normalize_product_type(product_type, required=True)
        stock_delta = validate_stock_value(ptype, stock, allow_zero=True)
    except ProductTypeError as e:
        return _warehouse_error(e.message)

    product = db.query(Product).filter(
        Product.org_id == org.id,
        func.lower(Product.name) == normalized_name.lower(),
    ).first()

    if product:
        try:
            can_change_product_type(
                current_type=product_type_of(product),
                new_type=ptype,
                current_stock=float(product.stock or 0),
            )
            # Validate the stock *delta* under the selected type.
            # Do not re-validate legacy absolute stock for already-packaged rows that
            # may carry pre-migration fractional values (defaulted to packaged).
            validate_stock_value(ptype, stock_delta, allow_zero=True)
            resulting = float(product.stock or 0) + float(stock_delta)
            if resulting < -1e-9:
                raise ProductTypeError("invalid_stock", "Stock cannot be negative.")
            if (
                ptype == PRODUCT_TYPE_PACKAGED
                and product_type_of(product) != PRODUCT_TYPE_PACKAGED
            ):
                # Converting into packaged: final stock must be whole.
                validate_stock_value(ptype, resulting, allow_zero=True)
        except ProductTypeError as e:
            return _warehouse_error(e.message)
        product.price = price_f
        product.product_type = ptype
        product.stock = float(product.stock or 0) + float(stock_delta)
    else:
        product = Product(
            org_id=org.id,
            name=normalized_name,
            price=price_f,
            stock=stock_delta,
            product_type=ptype,
        )
        db.add(product)

    db.commit()
    return RedirectResponse("/warehouse", status_code=303)

@router.post("/delete_product/{product_id}")
def delete_product(request: Request, product_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    product = db.query(Product).filter(Product.id == product_id, Product.org_id == org.id).first()
    if product:
        db.delete(product)
        db.commit()
    return RedirectResponse("/warehouse", status_code=303)

@router.get("/api/products")
def get_api_products(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return []
    products = db.query(Product).filter(Product.org_id == org.id).order_by(Product.name).all()
    out = []
    for p in products:
        ptype = product_type_of(p)
        out.append({
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "stock": p.stock,
            "product_type": ptype,
            "unit": sale_unit_for(ptype),
            "is_loose": ptype == PRODUCT_TYPE_LOOSE,
        })
    return out


class WeighTicketCreateBody(BaseModel):
    product_id: int
    weight: float
    unit: str = "kg"
    # Client-supplied expires_at is ignored; server always assigns a 12-hour TTL.


class WeighTicketResolveBody(BaseModel):
    public_token: str


class WeighTicketCancelByTokenBody(BaseModel):
    token: str


@router.get("/api/weigh-tickets")
def api_list_weigh_tickets(
    request: Request,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Org-scoped QR ticket history for Weigh & POS status views."""
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        rows, total = list_weigh_tickets(
            db, org_id=org.id, status=status, limit=limit, offset=offset
        )
        db.commit()  # persist any lazy timeout cancellations from the list sweep
    except WeighTicketError as e:
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    return {
        "ok": True,
        "total": total,
        "limit": max(1, min(int(limit or 50), 200)),
        "offset": max(0, int(offset or 0)),
        "tickets": [ticket_public_dict(t, include_token=False) for t in rows],
    }


@router.post("/api/weigh-tickets")
def api_create_weigh_ticket(
    request: Request,
    body: WeighTicketCreateBody = Body(...),
    db: Session = Depends(get_db),
):
    """Create an ACTIVE weigh ticket. Does not deduct stock. QR payload = public_token only."""
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        ticket = create_weigh_ticket(
            db,
            org_id=org.id,
            product_id=body.product_id,
            weight=body.weight,
            unit=body.unit or "kg",
            created_by_user_id=user.id if user else None,
            expires_at=None,  # always 12h server TTL
            commit=True,
        )
    except WeighTicketError as e:
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    return {"ok": True, "ticket": ticket_public_dict(ticket, include_token=True)}


@router.post("/api/weigh-tickets/resolve")
def api_resolve_weigh_ticket(
    request: Request,
    body: WeighTicketResolveBody = Body(...),
    db: Session = Depends(get_db),
):
    """Resolve opaque token for POS. Does not deduct stock or consume the ticket."""
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        resolved = resolve_weigh_ticket(db, org_id=org.id, public_token=body.public_token)
        # Persist lazy expiry transitions without consuming.
        db.commit()
    except WeighTicketError as e:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    ticket = resolved.ticket
    product = resolved.product
    return {
        "ok": True,
        "ticket": ticket_public_dict(ticket, include_token=False),
        "product": {
            "id": product.id,
            "name": product.name,
            "price": float(product.price or 0),
            "stock": float(product.stock or 0),
        },
        # Authoritative line values for cart (client must not invent price/weight).
        "line": {
            "product_id": product.id,
            "product": ticket.product_name_snapshot,
            "weight": ticket.weight,
            "unit": ticket.unit,
            "unit_price": ticket.unit_price_snapshot,
            "total_amount": ticket.total_amount_snapshot,
            "public_token": ticket.public_token,
        },
    }


@router.post("/api/weigh-tickets/cancel-by-token")
def api_cancel_weigh_ticket_by_token(
    request: Request,
    body: WeighTicketCancelByTokenBody = Body(...),
    db: Session = Depends(get_db),
):
    """
    Verified cancellation: requires scanning the opaque QR token.
    Does not accept ticket IDs. Never changes stock.
    """
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    try:
        ticket = cancel_weigh_ticket_by_token(
            db,
            org_id=org.id,
            public_token=body.token,
            commit=True,
        )
    except WeighTicketError as e:
        return JSONResponse(
            {"ok": False, "error": e.message, "code": e.code},
            status_code=resolve_error_http_status(e.code),
        )
    return {"ok": True, "ticket": ticket_public_dict(ticket, include_token=False)}


@router.post("/api/weigh-tickets/{ticket_id}/cancel")
def api_cancel_weigh_ticket_by_id_blocked(
    request: Request,
    ticket_id: int,
    db: Session = Depends(get_db),
):
    """Legacy ID-based cancel is disabled — physical QR token verification is required."""
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"ok": False, "error": "Please sign in again.", "code": "unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "ok": False,
            "error": "Cancel requires scanning the QR ticket. Use POST /api/weigh-tickets/cancel-by-token.",
            "code": "cancel_requires_token",
        },
        status_code=403,
    )


# ---------------- EXPORT AND CLEAR ----------------
@router.get("/export_and_clear")
def export_and_clear(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    transactions = db.query(Transaction).filter(Transaction.org_id == org.id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bill ID", "Description", "Items JSON", "Total Tax", "Grand Total", "Date"])
    
    for t in transactions:
        writer.writerow([
            t.id, t.product_name, t.category, 
            round(t.gst_amount, 2), round(t.total_amount, 2), 
            t.created_at.strftime('%Y-%m-%d %H:%M:%S') if t.created_at else ''
        ])
        
    db.query(Transaction).filter(Transaction.org_id == org.id).delete()
    db.commit()
    
    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aica_transactions_export.csv"}
    )

# ---------------- GEMINI VOICE ASSIST ----------------
@router.post("/api/voice_assist")
def voice_assist(text_query: str = Form(...)):
    if not client:
        return {"error": "Gemini API client not initialized or offline."}
        
    try:
        prompt = (
            f"You are the voice assistant for an AI PoS billing system. The user verbally said: '{text_query}'. "
            "Interpret this spoken sentence and translate it into a JSON action block. "
            "Available actions:\n"
            "1. 'add_to_cart' with parameters: 'product' (string, e.g. 'Maggi', 'Lays', 'Milk'), 'quantity' (float, default 1.0)\n"
            "2. 'remove_from_cart' with parameters: 'product' (string)\n"
            "3. 'checkout' with parameters: 'payment_method' ('cash' or 'card')\n"
            "4. 'search' with parameters: 'query' (string)\n"
            "5. 'none' (conversational response) with parameters: 'reply' (conversational text string, friendly and concise)\n\n"
            "Constraints:\n"
            "- Always identify grocery product names accurately. "
            "- Output ONLY valid raw JSON. Do not include markdown codeblocks (no ```json) or explanations."
        )
        
        response = generate_content_with_fallback(
            contents=prompt
        )
        
        resp_text = response.text.strip()
        resp_text = re.sub(r"^```json\s*|\s*```$", "", resp_text, flags=re.MULTILINE).strip()
        
        action_data = json.loads(resp_text)
        return {"success": True, "action": action_data}
    except Exception as e:
        return {"error": f"Failed to interpret voice instructions: {str(e)}"}

# ---------------- ORGANIZATION PROFILE ROUTES ----------------
@router.get("/organization")
def organization_view(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="organization.html",
        context=page_ctx(request, db, "organization")
    )

@router.post("/api/organization")
def organization_update(
    request: Request,
    name: str = Form(...),
    business_type: str = Form(...),
    industry: str = Form(...),
    pan: str = Form(""),
    gstin: str = Form(""),
    gst_registered: str = Form("false"),
    registered_address: str = Form(""),
    state: str = Form(""),
    financial_year: str = Form("2026-27"),
    accounting_period: str = Form("Monthly"),
    tax_regime: str = Form("Regular (Old Regime)"),
    employees_count: int = Form(0),
    business_turnover: float = Form(0.0),
    organization_size: str = Form("Micro"),
    branches: str = Form(""),
    bank_accounts: str = Form(""),
    db: Session = Depends(get_db)
):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    
    org.name = name
    org.business_type = business_type
    org.industry = industry
    org.pan = pan.strip().upper()
    org.gstin = gstin.strip().upper()
    org.gst_registered = parse_form_bool(gst_registered)
    org.registered_address = registered_address
    org.state = state
    org.financial_year = financial_year
    org.accounting_period = accounting_period
    org.tax_regime = tax_regime
    org.employees_count = employees_count
    org.business_turnover = business_turnover
    org.organization_size = organization_size
    org.branches = branches
    org.bank_accounts = bank_accounts
    
    db.commit()
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/organization", status_code=303)

# ---------------- EXPENSE MANAGEMENT ROUTES ----------------
@router.get("/expenses")
def expenses_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    exps = db.query(Expense).filter(Expense.org_id == snap["org"].id).order_by(Expense.date.desc()).all() if snap.get("org") else []
    return templates.TemplateResponse(
        request=request,
        name="expenses.html",
        context=page_ctx(request, db, "expenses", {
            "expenses": exps,
            "business_expense_total": snap["business_expense_total"],
            "personal_expense_total": snap["personal_expense_total"],
            "eligible_itc": snap["eligible_itc"],
            "blocked_itc": snap["blocked_itc"],
            "expense_by_category": snap["expense_by_category"],
            "error": request.query_params.get("error"),
            "org_state": (snap["org"].state if snap.get("org") else "") or "",
            "gst_registered": bool(getattr(snap["org"], "gst_registered", False)) if snap.get("org") else False,
            "expense_categories": list(
                (
                    ((load_tax_rules().get("gst_rules") or {}).get("expense_category_rates") or {}).get("categories")
                    or {}
                ).keys()
            ),
        })
    )

@router.get("/api/expenses/tax-preview")
def expense_tax_preview(
    request: Request,
    category: str = "",
    amount: float = 0.0,
    supplier_state: str = "",
    db: Session = Depends(get_db),
):
    user, org = current_user_org(request, db)
    if not org:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return expense_gst_from_rules(org, category, amount, supplier_state)

@router.post("/api/expenses/create")
def expense_create(
    request: Request,
    vendor: str = Form(...),
    invoice_number: str = Form(""),
    category: str = Form(...),
    subcategory: str = Form(""),
    amount: float = Form(...),
    supplier_state: str = Form(""),
    payment_method: str = Form("Cash"),
    is_business: str = Form("true"),
    classification: str = Form("Revenue"),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    business_flag = parse_form_bool(is_business)
    tax = expense_gst_from_rules(org, category, amount, supplier_state)
    if not tax["ok"]:
        return RedirectResponse(
            "/expenses?error=" + quote(tax["error"] or "Unable to determine GST for this expense."),
            status_code=303
        )

    exp = Expense(
        org_id=org.id,
        vendor=vendor,
        invoice_number=invoice_number,
        category=category,
        subcategory=subcategory,
        amount=amount,
        cgst=tax["cgst"],
        sgst=tax["sgst"],
        igst=tax["igst"],
        total_tax=tax["total_tax"],
        total_amount=tax["total_amount"],
        payment_method=payment_method,
        is_business=business_flag,
        classification=classification,
        supplier_state=(supplier_state or "").strip(),
        gst_rate=tax["rate"],
        description=description,
        status="Approved"
    )
    db.add(exp)
    db.commit()
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/expenses", status_code=303)

@router.post("/api/expenses/upload")
async def expense_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    filename = file.filename
    
    # 1. OCR Extract
    extracted = ocr_and_analyze_invoice(contents, filename)
    
    # 2. AI Classification based on product description
    desc = extracted.get("description", filename)
    classification_meta = classify_expense_ai(desc)
    
    # Compute totals
    taxable_val = sanitize_ai_amount(extracted.get("taxable_value", 0.0))
    cgst = sanitize_ai_amount(extracted.get("cgst", 0.0))
    sgst = sanitize_ai_amount(extracted.get("sgst", 0.0))
    igst = sanitize_ai_amount(extracted.get("igst", 0.0))
    total_tax = to_float(D(cgst) + D(sgst) + D(igst))
    total_amount = sanitize_ai_amount(extracted.get("total_amount", taxable_val + total_tax))
    if total_amount <= 0:
        total_amount = to_float(D(taxable_val) + D(total_tax))
    
    # Save Uploaded Invoice
    user, org = current_user_org(request, db)
    if not org:
        return {"success": False, "error": "Unauthorized"}
    exp = Expense(
        org_id=org.id,
        vendor=extracted.get("vendor", "Unknown Vendor"),
        invoice_number=extracted.get("invoice_number", ""),
        category=classification_meta.get("category", "Technology"),
        subcategory=classification_meta.get("subcategory", "Software"),
        amount=taxable_val,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        total_tax=total_tax,
        total_amount=total_amount,
        payment_method="Bank Transfer",
        is_business=bool(classification_meta.get("is_business", True)),
        classification=classification_meta.get("classification", "Revenue"),
        description=f"{desc} (OCR Audit: {classification_meta.get('explanation', '')})",
        status="Approved"
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    
    # Write OCR errors as anomalies if any
    anomalies = extracted.get("anomalies", [])
    if anomalies:
        for a in anomalies:
            anom = Anomaly(
                org_id=org.id,
                severity="High",
                reason=f"OCR Flagged Invoice INV#{exp.invoice_number} from {exp.vendor}",
                details=a,
                status="Active"
            )
            db.add(anom)
        db.commit()
        
        db.commit()

    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return {"success": True, "expense_id": exp.id, "extracted": extracted, "classification": classification_meta}

@router.post("/api/expenses/delete/{expense_id}")
def expense_delete(request: Request, expense_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    exp = db.query(Expense).filter(Expense.id == expense_id, Expense.org_id == org.id).first()
    if exp:
        db.delete(exp)
        db.commit()
        snap = get_financial_snapshot(db, request=request)
        upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/expenses", status_code=303)

# ---------------- EMPLOYEE & PAYROLL ROUTES ----------------
@router.get("/employees")
def employees_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    fy_start, fy_end = snap["fy_start"], snap["fy_end"]
    employees = snap["employees"]
    eligibility = {}
    for emp in employees:
        joined_in_fy = emp.joining_date and fy_start <= emp.joining_date <= fy_end
        eligibility[emp.id] = bool(emp.status == "Active" and emp.salary <= 25000 and joined_in_fy)

    return templates.TemplateResponse(
        request=request,
        name="employees.html",
        context=page_ctx(request, db, "employees", {
            "employees": employees,
            "eligible_count": snap["eligible_count"],
            "potential_deduction": snap["eligible_80jjaa"],
            "monthly_payroll": snap["monthly_payroll"],
            "annual_payroll": snap["annual_payroll"],
            "eligibility": eligibility,
            "financial_year": snap["org"].financial_year if snap["org"] else "2026-27",
            "error": request.query_params.get("error"),
        })
    )

@router.post("/api/employees/create")
def employee_create(
    request: Request,
    employee_id: str = Form(...),
    name: str = Form(...),
    department: str = Form("Sales"),
    designation: str = Form("Associate"),
    salary: float = Form(...),
    basic_salary: float = Form(...),
    allowances: float = Form(0.0),
    bonuses: float = Form(0.0),
    incentives: float = Form(0.0),
    employer_contribution: float = Form(0.0),
    benefits: float = Form(0.0),
    joining_date: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        j_date = datetime.strptime(joining_date, "%Y-%m-%d")
    except ValueError:
        j_date = datetime.now()

    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    code = employee_id.strip().upper()
    existing = db.query(Employee).filter(
        Employee.org_id == org.id,
        func.upper(Employee.employee_id) == code
    ).first()
    if existing:
        return RedirectResponse(
            "/employees?error=" + quote("This employee ID is already registered. Use a different ID."),
            status_code=303
        )
    emp = Employee(
        org_id=org.id,
        employee_id=code,
        name=name.strip(),
        department=department,
        designation=designation,
        salary=salary,
        basic_salary=basic_salary,
        allowances=allowances,
        bonuses=bonuses,
        incentives=incentives,
        employer_contribution=employer_contribution,
        benefits=benefits,
        joining_date=j_date,
        status="Active"
    )
    db.add(emp)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            "/employees?error=" + quote("This employee ID is already registered. Use a different ID."),
            status_code=303
        )
    sync_org_headcount(db, org)
    snap = get_financial_snapshot(db, org)
    upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/employees", status_code=303)

@router.post("/api/employees/delete/{employee_id}")
def employee_delete(request: Request, employee_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    emp = db.query(Employee).filter(Employee.id == employee_id, Employee.org_id == org.id).first()
    if emp:
        db.delete(emp)
        db.commit()
        sync_org_headcount(db, org)
        snap = get_financial_snapshot(db, org)
        upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/employees", status_code=303)

# ---------------- FIXED ASSETS ROUTES ----------------
@router.get("/assets")
def assets_view(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    assets = db.query(Asset).filter(Asset.org_id == org.id).order_by(Asset.purchase_date.desc()).all()
    
    # Calculate accumulated depreciation and WDV
    total_cost = 0.0
    accumulated_dep = 0.0
    for a in assets:
        total_cost += a.purchase_value
        dep_amount = a.purchase_value * (a.depreciation_rate / 100.0)
        accumulated_dep += dep_amount
        a.current_value = a.purchase_value - dep_amount
        
    db.commit()
    
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return templates.TemplateResponse(
        request=request,
        name="assets.html",
        context=page_ctx(request, db, "assets", {
            "assets": assets,
            "total_cost": round(total_cost, 2),
            "accumulated_dep": round(accumulated_dep, 2),
            "net_wdv": round(total_cost - accumulated_dep, 2),
        })
    )

@router.post("/api/assets/create")
def asset_create(
    request: Request,
    name: str = Form(...),
    purchase_date: str = Form(...),
    purchase_value: float = Form(...),
    gst_amount: float = Form(0.0),
    cgst: float = Form(0.0),
    sgst: float = Form(0.0),
    igst: float = Form(0.0),
    supplier: str = Form(""),
    category: str = Form(...),
    useful_life_years: float = Form(5.0),
    depreciation_rate: float = Form(15.0),
    db: Session = Depends(get_db)
):
    try:
        p_date = datetime.strptime(purchase_date, "%Y-%m-%d")
    except ValueError:
        p_date = datetime.now()
        
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    asset = Asset(
        org_id=org.id,
        name=name.strip(),
        purchase_date=p_date,
        purchase_value=purchase_value,
        gst_amount=gst_amount,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        supplier=supplier,
        category=category,
        useful_life_years=useful_life_years,
        depreciation_rate=depreciation_rate,
        current_value=purchase_value
    )
    db.add(asset)
    db.commit()
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/assets", status_code=303)

@router.post("/api/assets/delete/{asset_id}")
def asset_delete(request: Request, asset_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.org_id == org.id).first()
    if asset:
        db.delete(asset)
        db.commit()
        snap = get_financial_snapshot(db, request=request)
        upsert_rule_based_recommendations(db, snap)
    return RedirectResponse("/assets", status_code=303)

# ---------------- GST / ITC OPPORTUNITIES ROUTES ----------------
@router.get("/gst")
def gst_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    return templates.TemplateResponse(
        request=request,
        name="gst_itc.html",
        context=page_ctx(request, db, "gst", {
            "output_gst": snap["output_gst"],
            "total_input_gst": snap["input_gst"],
            "blocked_itc": snap["blocked_itc"],
            "blocked_by_category": snap["blocked_by_category"],
            "eligible_itc": snap["eligible_itc"],
            "net_gst_payable": snap["net_gst_payable"],
            "output_gst_splits": snap["output_gst_splits"],
            "personal_expense_total": snap["personal_expense_total"],
        })
    )

# ---------------- INCOME TAX ENGINE ROUTES ----------------
@router.get("/income-tax")
def income_tax_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    org = snap["org"]
    return templates.TemplateResponse(
        request=request,
        name="income_tax.html",
        context=page_ctx(request, db, "income-tax", {
            "org": org,
            "sales_total": snap["sales_total"],
            "total_expenses": snap["expenses_total"],
            "business_expense_total": snap["business_expense_total"],
            "personal_expense_total": snap["personal_expense_total"],
            "monthly_payroll": snap["monthly_payroll"],
            "profit": snap["profit_total"],
            "eligible_80jjaa": snap["eligible_80jjaa"],
            "total_depreciation": snap["total_depreciation"],
            "taxable_old": snap["taxable_old"],
            "total_tax_old": snap["total_tax_old"],
            "taxable_new": snap["taxable_new"],
            "total_tax_new": snap["total_tax_new"],
        })
    )

# ---------------- TAX OPTIMIZATION ENGINE ROUTES ----------------
@router.get("/tax-optimization")
def tax_optimization_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    upsert_rule_based_recommendations(db, snap)
    org = snap.get("org")
    oid = org.id if org else None
    # Purge Gemini rows that treated absolute INR as lakhs (and other implausible impacts)
    if oid:
        removed = scrub_optimization_recommendations(db, oid, snap)
        if removed:
            logging.warning(
                "AI Optimization scrubbed %s corrupt recommendation(s) for org_id=%s debug=%s",
                removed, oid, optimization_debug_snapshot(snap),
            )
    recs = []
    cards = []
    tax_position = {
        "turnover": 0, "taxable_income": 0, "tax_liability": 0, "tax_liability_115baa": 0,
        "eligible_itc": 0, "net_gst_payable": 0, "output_gst": 0,
        "currency": "INR", "money_unit": "absolute_INR", "finance": {},
    }
    potential_savings = 0
    if oid:
        recs = (
            db.query(TaxRecommendation)
            .filter(TaxRecommendation.org_id == oid)
            .order_by(TaxRecommendation.estimated_tax_impact.desc())
            .all()
        )
        invoice_count = (
            db.query(Expense)
            .filter(
                Expense.org_id == oid,
                or_(
                    (Expense.invoice_number.isnot(None)) & (Expense.invoice_number != ""),
                    (Expense.doc_path.isnot(None)) & (Expense.doc_path != ""),
                ),
            )
            .count()
        )
        counts = {
            "invoices": invoice_count,
            "employees": db.query(Employee).filter(Employee.org_id == oid).count(),
            "assets": db.query(Asset).filter(Asset.org_id == oid).count(),
        }
        cards = [enrich_recommendation(r, org, snap, counts) for r in recs]
        tax_position = {
            "turnover": snap.get("sales_total", 0),
            "taxable_income": snap.get("taxable_old", 0),
            "tax_liability": snap.get("total_tax_old", 0),
            "tax_liability_115baa": snap.get("total_tax_new", 0),
            "eligible_itc": snap.get("eligible_itc", 0),
            "net_gst_payable": snap.get("net_gst_payable", 0),
            "output_gst": snap.get("output_gst", 0),
            "currency": "INR",
            "money_unit": "absolute_INR",
            "finance": snap.get("finance") or {},
        }
        potential_savings = sum(
            c["estimated_tax_impact"]
            for c in cards
            if c.get("impact_type") in ("tax_saving", "tax_credit")
        )
    return templates.TemplateResponse(
        request=request,
        name="tax_optimization.html",
        context=page_ctx(request, db, "tax-optimization", {
            "recommendations": recs,
            "opt_cards": cards,
            "tax_position": tax_position,
            "potential_savings": potential_savings,
        })
    )

# ---------------- WHAT-IF SIMULATOR ROUTES ----------------
@router.get("/what-if")
def what_if_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    return templates.TemplateResponse(
        request=request,
        name="what_if.html",
        context=page_ctx(request, db, "what-if", {
            "sales_total": snap["sales_total"],
            "expenses_total": snap["expenses_total"],
            "profit_total": snap["profit_total"],
        })
    )

@router.post("/api/simulate")
def simulate_scenario(
    request: Request,
    scenario_type: str = Form(...),
    param1: str = Form(""),
    param2: str = Form(""),
    db: Session = Depends(get_db)
):
    # Compile current financials
    snap = get_financial_snapshot(db, request=request)
    financial_summary = {
        "turnover": snap["sales_total"],
        "expenses": snap["expenses_total"],
        "profit": snap["profit_total"],
        "assets_count": len(snap["assets"]),
        "employees_count": len(snap["active_employees"]),
        "eligible_itc": snap["eligible_itc"],
        "estimated_tax": snap["total_tax_old"],
    }
    
    params = {"param1": param1, "param2": param2}
    
    result = simulate_what_if_scenario(scenario_type, params, financial_summary)
    return {"success": True, "result": result}

# ---------------- FINANCIAL FORECASTING ROUTES ----------------
@router.get("/forecasting")
def forecasting_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    return templates.TemplateResponse(
        request=request,
        name="forecasting.html",
        context=page_ctx(request, db, "forecasting", {
            "sales_total": snap["sales_total"],
            "expenses_total": snap["expenses_total"],
            "profit_total": snap["profit_total"],
        })
    )

@router.get("/api/forecasts")
def forecasts_api(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    months = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
    if not snap["sales_total"] and not snap["expenses_total"]:
        zeros = [0] * 12
        return {
            "months": months,
            "revenue_actual_vs_forecast": zeros,
            "expenses_actual_vs_forecast": zeros,
            "profit_actual_vs_forecast": zeros,
            "tax_actual_vs_forecast": zeros,
            "cashflow_actual_vs_forecast": zeros,
            "insights": ["No financial data available yet. Record a sale or expense to start forecasting from your books."]
        }
    monthly_sales = snap["sales_total"] / 12.0 if snap["sales_total"] else 0
    monthly_exp = snap["expenses_total"]
    months = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
    revenue = [round(monthly_sales * (1 + 0.015 * i), 2) for i in range(12)]
    expenses = [round(monthly_exp * (1 + 0.01 * i), 2) for i in range(12)]
    profit = [round(revenue[i] - expenses[i], 2) for i in range(12)]
    tax = [round(max(p, 0) * 0.25, 2) for p in profit]
    cashflow = [round(profit[i] + (snap["total_depreciation"] / 12.0), 2) for i in range(12)]
    local_forecast = {
        "months": months,
        "revenue_actual_vs_forecast": revenue,
        "expenses_actual_vs_forecast": expenses,
        "profit_actual_vs_forecast": profit,
        "tax_actual_vs_forecast": tax,
        "cashflow_actual_vs_forecast": cashflow,
        "insights": [
            f"Current books: sales ₹{snap['sales_total']:,.2f}, monthly operating cost ₹{snap['expenses_total']:,.2f} (business spends + this month's payroll).",
            f"Personal spends of ₹{snap['personal_expense_total']:,.2f} are excluded from profit so owner drawings do not shrink taxable income.",
            f"GST net position (output − eligible ITC) is ₹{snap['net_gst_payable']:,.2f}. File GSTR-3B on this working, after matching GSTR-2B.",
            f"Estimated old-regime tax on current profit after 80JJAA and depreciation is ₹{snap['total_tax_old']:,.2f}.",
        ]
    }
    historical_data = {
        "sales_total": snap["sales_total"],
        "expense_total": snap["expenses_total"],
        "profit": snap["profit_total"]
    }
    forecast = generate_forecasting_data(historical_data)
    if not forecast or forecast.get("insights") == ["Forecast default fallback due to system issue."]:
        return local_forecast
    return forecast

# ---------------- COMPLIANCE CALENDAR ROUTES ----------------
@router.get("/compliance")
def compliance_view(request: Request, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    obligations = db.query(ComplianceObligation).filter(ComplianceObligation.org_id == org.id).order_by(ComplianceObligation.due_date.asc()).all()
    return templates.TemplateResponse(
        request=request,
        name="compliance.html",
        context=page_ctx(request, db, "compliance", {"obligations": obligations})
    )

@router.post("/api/compliance/toggle/{comp_id}")
def compliance_toggle(request: Request, comp_id: int, db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    obj = db.query(ComplianceObligation).filter(ComplianceObligation.id == comp_id, ComplianceObligation.org_id == org.id).first()
    if obj:
        if obj.status == "Pending":
            obj.status = "Completed"
            obj.completed_date = datetime.utcnow()
        else:
            obj.status = "Pending"
            obj.completed_date = None
        db.commit()
    return RedirectResponse("/compliance", status_code=303)

# ---------------- AUDIT REPORT GENERATOR ----------------
@router.get("/reports")
def reports_view(request: Request, db: Session = Depends(get_db)):
    snap = get_financial_snapshot(db, request=request)
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context=page_ctx(request, db, "reports", {
            "sales_total": snap["sales_total"],
            "expenses_total": snap["expenses_total"],
            "profit_total": snap["profit_total"],
            "eligible_itc": snap["eligible_itc"],
            "total_tax_old": snap["total_tax_old"],
        })
    )

@router.get("/reports/generate")
def report_generate(request: Request, format_type: str = "json", db: Session = Depends(get_db)):
    user, org = current_user_org(request, db)
    if not org:
        return login_redirect()
    snap = get_financial_snapshot(db, org)
    sales_total = snap["sales_total"]
    expenses_total = snap["expenses_total"]
    payroll_total = snap["annual_payroll"]
    assets_total = snap["assets_cost"]
    
    recs = db.query(TaxRecommendation).filter(TaxRecommendation.org_id == org.id).all()
    anoms = db.query(Anomaly).filter(Anomaly.org_id == org.id).all()
    
    report_data = {
        "report_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "financial_summary": {
            "total_turnover": round(sales_total, 2),
            "total_expenses": round(expenses_total, 2),
            "projected_annual_payroll": round(payroll_total, 2),
            "total_capital_assets": round(assets_total, 2),
            "net_profit": round(sales_total - expenses_total, 2)
        },
        "tax_planning_insights": [
            {"title": r.title, "section": r.rule_section, "impact": r.estimated_tax_impact, "confidence": r.confidence_level}
            for r in recs
        ],
        "financial_compliance_anomalies": [
            {"severity": a.severity, "reason": a.reason, "details": a.details}
            for a in anoms
        ]
    }
    
    if format_type == "json":
        return report_data
    
    # Generate audit-friendly export format
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["AICA Audit Report Financial Statement", datetime.now().strftime("%Y-%m-%d")])
    writer.writerow([])
    writer.writerow(["FINANCIAL SUMMARY"])
    writer.writerow(["Metric", "Value (Rs)"])
    writer.writerow(["Total Turnover / Sales", sales_total])
    writer.writerow(["Total Recorded Expenses", expenses_total])
    writer.writerow(["Projected Payroll Costs", payroll_total])
    writer.writerow(["Capital Assets Value", assets_total])
    writer.writerow(["Net Operating Profit", sales_total - expenses_total])
    writer.writerow([])
    writer.writerow(["TAX OPPORTUNITIES FOUND"])
    writer.writerow(["Title", "Section", "Estimated Savings", "Confidence Level"])
    for r in recs:
        writer.writerow([r.title, r.rule_section, r.estimated_tax_impact, r.confidence_level])
        
    csv_content = output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aica_financial_audit_report.csv"}
    )
