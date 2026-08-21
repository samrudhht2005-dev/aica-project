"""
Sanitize AI Optimization recommendations.

Gemini previously appended 'Lakhs' to absolute INR figures (e.g. sales=1670.80 →
'₹1,670.8 Lakhs') and computed tax on the inflated amount (→ ₹4.17 Cr 'saving').

Database numeric values are absolute INR. This module rejects / deletes corrupt
AI rows so the AI Optimization tab cannot display them.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from backend.money import format_inr, to_float, D
from models.db_models import TaxRecommendation

log = logging.getLogger(__name__)

# Rule-engine rows are trusted; AI rows must pass validation.
TRUSTED_STATUSES = {"Live from your books"}

_LAKH_NUM = re.compile(
    r"₹?\s*([\d,]+(?:\.\d+)?)\s*(?:lakh|lakhs|lac|lacs)\b",
    re.IGNORECASE,
)
_CRORE_NUM = re.compile(
    r"₹?\s*([\d,]+(?:\.\d+)?)\s*(?:crore|crores|cr)\b",
    re.IGNORECASE,
)


def _near(a: float, b: float, rel: float = 0.08, abs_tol: float = 2.0) -> bool:
    if b == 0:
        return abs(a) <= abs_tol
    return abs(a - b) <= max(abs_tol, abs(b) * rel)


def prose_mistakenly_scales_absolute(text: str, absolute_inr: float) -> bool:
    """
    True when prose says 'N lakh/crore' and N ≈ the absolute INR book figure
    (the classic 1670.80 → '1670.8 Lakhs' bug).
    """
    if not text or absolute_inr <= 0:
        return False
    for rx, factor in ((_LAKH_NUM, 100_000.0), (_CRORE_NUM, 10_000_000.0)):
        for m in rx.finditer(text):
            try:
                n = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            # N matches absolute rupees → they appended a scale word wrongly
            if _near(n, absolute_inr):
                return True
            # Also catch '1,670.8 Lakhs' with Indian commas already stripped above
    return False


def impact_plausible(impact: float, snap: dict) -> bool:
    """Tax saving/credit cannot exceed a generous bound of the org's absolute books."""
    impact = abs(to_float(impact))
    if impact <= 0:
        return True
    sales = abs(to_float(snap.get("sales_total", 0)))
    biz = abs(to_float(snap.get("business_expense_total", 0)))
    profit = abs(to_float(snap.get("profit_total", 0)))
    tax = max(to_float(snap.get("total_tax_old", 0)), to_float(snap.get("total_tax_new", 0)))
    itc = abs(to_float(snap.get("eligible_itc", 0)))
    jjaa_save = abs(to_float(snap.get("eligible_80jjaa", 0))) * 0.25
    dep_save = abs(to_float(snap.get("total_depreciation", 0))) * 0.25
    book = max(sales, biz, profit, tax, itc, 1.0)

    # Classic lakh-bug: impact ≈ absolute_turnover * 1e5 * tax_rate
    if sales > 0 and impact > max(sales * 10, 1.0):
        return False

    # Bound by what the books could possibly support
    ceiling = max(tax * 2.0, itc * 1.5, jjaa_save * 1.5, dep_save * 1.5, book * 0.35, 5_000.0)
    # Micro books: never allow five-figure+ fantasies beyond a hard cap tied to book size
    if book < 50_000:
        ceiling = min(ceiling, max(book * 0.5, itc * 1.5, 10_000.0))
    if book < 10_000:
        ceiling = min(ceiling, max(book, itc * 1.5, 5_000.0))
    # No positive taxable profit / liability → income-tax "savings" are invented
    profit_signed = to_float(snap.get("profit_total", 0))
    if profit_signed <= 0 and tax <= 0 and impact > max(itc * 1.5, 1.0):
        return False

    if impact > ceiling:
        return False
    return True


def recommendation_is_corrupt(rec: TaxRecommendation, snap: dict) -> bool:
    impact = float(rec.estimated_tax_impact or 0)
    sales = to_float(snap.get("sales_total", 0))
    text = f"{rec.detected_item or ''} {rec.reason or ''}"

    if prose_mistakenly_scales_absolute(text, sales):
        return True
    # Any 'N lakh' claim when absolute sales is under ₹1 lakh is almost certainly wrong scale
    if sales < 100_000 and _LAKH_NUM.search(text):
        return True

    # Rule-engine rows are computed from absolute INR snapshot — keep them
    if (rec.status or "") in TRUSTED_STATUSES:
        return False

    if not impact_plausible(impact, snap):
        return True

    if sales <= 0 and to_float(snap.get("business_expense_total", 0)) <= 0 and impact > 0:
        return True
    return False


def validate_ai_recommendation_payload(row: dict, snap: dict) -> dict | None:
    """
    Gate Gemini JSON before DB insert. Returns cleaned row or None to skip.
    Recalculates nothing inventively — drops corrupt monetary claims.
    """
    if not isinstance(row, dict):
        return None
    impact = to_float(row.get("estimated_tax_impact", 0))
    sales = to_float(snap.get("sales_total", 0))
    text = f"{row.get('detected_item', '')} {row.get('reason', '')}"

    if prose_mistakenly_scales_absolute(text, sales):
        log.warning("Skipping AI rec (lakh-scale prose on absolute INR): %s", row.get("title"))
        return None
    if not impact_plausible(impact, snap):
        log.warning(
            "Skipping AI rec (implausible impact %s vs books sales=%s): %s",
            impact, sales, row.get("title"),
        )
        return None

    # Never let Gemini self-certify
    status = str(row.get("status") or "Requires Verification")
    if "confirm" in status.lower():
        status = "Requires Verification"
    conf = float(row.get("confidence_level") or 70)
    conf = min(conf, 75.0)

    # Rewrite detected_item if it invents zero expenses while books have expenses
    biz = to_float(snap.get("business_expense_total", 0))
    detected = str(row.get("detected_item") or "")
    if biz > 0 and re.search(r"zero expenses|no expenses|without any expense", detected, re.I):
        detected = (
            f"Books show business expenses of {format_inr(biz)} against turnover "
            f"{format_inr(sales)}. Review deductibility and documentation."
        )

    out = dict(row)
    out["estimated_tax_impact"] = impact
    out["status"] = status
    out["confidence_level"] = conf
    out["detected_item"] = detected
    # Discourage lakh wording in stored prose — replace mistaken patterns defensively
    for field in ("reason", "detected_item", "eligibility_conditions"):
        val = str(out.get(field) or "")
        if prose_mistakenly_scales_absolute(val, sales):
            val = _LAKH_NUM.sub(lambda m: format_inr(float(m.group(1).replace(",", ""))), val)
            val = _CRORE_NUM.sub(lambda m: format_inr(float(m.group(1).replace(",", ""))), val)
            out[field] = val
    return out


def scrub_optimization_recommendations(db: Session, org_id: int, snap: dict) -> int:
    """Delete corrupt AI Optimization rows for this org. Returns delete count."""
    if not org_id:
        return 0
    rows = db.query(TaxRecommendation).filter(TaxRecommendation.org_id == org_id).all()
    deleted = 0
    for rec in rows:
        if recommendation_is_corrupt(rec, snap):
            log.warning(
                "Deleting corrupt optimization rec id=%s title=%r impact=%s",
                rec.id, rec.title, rec.estimated_tax_impact,
            )
            db.delete(rec)
            deleted += 1
        else:
            # Soft-fix: demote fake 'Confirmed' AI confidence
            if (rec.status or "") not in TRUSTED_STATUSES:
                if "confirm" in (rec.status or "").lower():
                    rec.status = "Requires Verification"
                if (rec.confidence_level or 0) > 75:
                    rec.confidence_level = 75.0
    if deleted:
        db.commit()
    else:
        db.commit()
    return deleted


def optimization_debug_snapshot(snap: dict) -> dict[str, Any]:
    """Structured debug block (absolute INR) for AI Optimization."""
    return {
        "currency": "INR",
        "unit": "absolute_INR",
        "sales_total": to_float(snap.get("sales_total", 0)),
        "business_expense_total": to_float(snap.get("business_expense_total", 0)),
        "personal_expense_total": to_float(snap.get("personal_expense_total", 0)),
        "expenses_total": to_float(snap.get("expenses_total", 0)),
        "profit_total": to_float(snap.get("profit_total", 0)),
        "taxable_old": to_float(snap.get("taxable_old", 0)),
        "total_tax_old": to_float(snap.get("total_tax_old", 0)),
        "eligible_itc": to_float(snap.get("eligible_itc", 0)),
        "net_gst_payable": to_float(snap.get("net_gst_payable", 0)),
        "display_sales": format_inr(snap.get("sales_total", 0)),
        "display_tax_liability": format_inr(snap.get("total_tax_old", 0)),
    }
