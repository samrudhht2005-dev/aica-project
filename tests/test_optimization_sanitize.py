"""Tests for AI Optimization lakh-scale corruption detection."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.optimization_sanitize import (
    prose_mistakenly_scales_absolute,
    impact_plausible,
    recommendation_is_corrupt,
    validate_ai_recommendation_payload,
)


SNAP_GAT = {
    "sales_total": 1670.80,
    "business_expense_total": 5000.0,
    "expenses_total": 5000.0,
    "profit_total": -3329.2,
    "total_tax_old": 0.0,
    "total_tax_new": 0.0,
    "eligible_itc": 0.0,
}


def test_prose_detects_1670_lakhs_bug():
    text = "Zero expenses reported against a turnover of ₹1,670.8 Lakhs, leading to inflated profit of ₹1,670.8 Lakhs"
    assert prose_mistakenly_scales_absolute(text, 1670.80) is True


def test_prose_allows_real_lakh_when_books_are_lakhs():
    # Absolute turnover 1,67,080 → saying "1.67 lakh" is OK (N != absolute)
    text = "Turnover is approximately ₹1.67 lakh"
    assert prose_mistakenly_scales_absolute(text, 167080.0) is False


def test_impact_41770000_implausible_for_1670_sales():
    assert impact_plausible(41_770_000.0, SNAP_GAT) is False
    assert impact_plausible(400.0, SNAP_GAT) is False  # loss-making books: no IT saving
    snap_profit = dict(SNAP_GAT, profit_total=50_000.0, total_tax_old=13_000.0, sales_total=80_000.0)
    assert impact_plausible(5_000.0, snap_profit) is True


def test_corrupt_rec_id_303_shape():
    rec = SimpleNamespace(
        title="Review of Business Expenses and Deductibility under P&L",
        detected_item="Zero expenses reported against a turnover of ₹1,670.8 Lakhs, leading to an artificially inflated taxable profit of ₹1,670.8 Lakhs under the Old Tax Regime.",
        reason="Failing to book expenses results in overpayment at 25%.",
        estimated_tax_impact=41_770_000.0,
        status="Confirmed calculation",
        confidence_level=99.0,
    )
    assert recommendation_is_corrupt(rec, SNAP_GAT) is True


def test_validate_rejects_payload():
    row = {
        "title": "Bad",
        "detected_item": "turnover of ₹1,670.8 Lakhs",
        "reason": "save tax",
        "estimated_tax_impact": 41_770_000,
        "status": "Confirmed calculation",
        "confidence_level": 99,
    }
    assert validate_ai_recommendation_payload(row, SNAP_GAT) is None


def test_format_expectations():
    from backend.money import format_inr, sanitize_ai_amount
    assert format_inr(1670.80) == "₹1,670.80"
    assert "lakh" not in format_inr(1670.80).lower()
    assert sanitize_ai_amount(1670.80) == 1670.8


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all optimization sanitize tests passed")
