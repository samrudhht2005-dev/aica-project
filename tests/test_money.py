"""Regression tests for AICA money normalization (prevents lakh/scale bugs)."""
from decimal import Decimal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.money import (
    parse_money,
    format_inr,
    sanitize_ai_amount,
    money_round,
    to_inr,
    ExchangeRateRequired,
    money_dict,
)


def test_plain_rupees_not_lakh():
    p = parse_money("₹1,607.80")
    assert p["amount"] == Decimal("1607.80")
    assert p["currency"] == "INR"
    assert p["scale"] is None


def test_numeric_authoritative():
    p = parse_money(1607.8)
    assert p["amount"] == Decimal("1607.80")
    assert sanitize_ai_amount(1607.8) == 1607.8
    assert sanitize_ai_amount("1607.80") == 1607.8


def test_lakh_scale_only_when_present():
    p = parse_money("1.6 lakh")
    assert p["amount"] == Decimal("160000.00")
    p2 = parse_money("₹1.60 lakhs")
    assert p2["amount"] == Decimal("160000.00")


def test_thousand_and_crore():
    assert parse_money("48 thousand")["amount"] == Decimal("48000.00")
    assert parse_money("2 crore")["amount"] == Decimal("20000000.00")


def test_format_indian_grouping():
    assert format_inr(1607.80) == "₹1,607.80"
    assert format_inr(10000) == "₹10,000.00"
    assert format_inr(100000) == "₹1,00,000.00"
    assert format_inr(1000000) == "₹10,00,000.00"
    assert format_inr(160000) == "₹1,60,000.00"


def test_format_does_not_change_value():
    assert money_round(1607.8) == Decimal("1607.80")
    # compact is display alias of same absolute value
    from backend.money import format_inr as fmt
    assert "L" in fmt(160000, compact=True) or "l" in fmt(160000, compact=True).lower()


def test_no_silent_fx():
    try:
        to_inr(100, "USD")
        assert False, "should require rate"
    except ExchangeRateRequired:
        pass
    assert to_inr(100, "USD", exchange_rate=83) == Decimal("8300.00")


def test_ai_string_with_lakh_normalizes():
    # If AI wrongly writes scale into a field, parse applies scale once from words
    assert sanitize_ai_amount("1.6 lakh") == 160000.0
    # Absolute rupee string must stay absolute
    assert sanitize_ai_amount("₹1,607.80") == 1607.8


def test_money_dict_semantics():
    d = money_dict(48000, semantic_type="TAX_LIABILITY", label="Estimated tax payable")
    assert d["amount"] == 48000.0
    assert d["currency"] == "INR"
    assert d["type"] == "TAX_LIABILITY"
    assert d["display"] == "₹48,000.00"


def test_regression_not_1607_lakh():
    """The production bug: 1607.8 INR must never become 1607.8 lakh (1.6e8)."""
    assert sanitize_ai_amount(1607.8) == 1607.8
    assert parse_money(1607.8)["amount"] == Decimal("1607.80")
    assert parse_money("1607.8")["scale"] is None
    assert format_inr(1607.8) == "₹1,607.80"
    assert "lakh" not in format_inr(1607.8).lower()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all money tests passed")
