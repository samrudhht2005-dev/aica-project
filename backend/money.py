"""
Authoritative money utilities for AICA.

Rules:
- Numeric amount + currency is the source of truth (default INR).
- Formatted strings are display-only.
- Indian scale words (lakh/crore/thousand) apply ONLY when parsing natural-language input.
- Database numeric fields must NEVER be re-parsed as "lakh".
- Do not invent FX rates; conversion requires an explicit verified rate.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Optional, Union

NumberLike = Union[int, float, str, Decimal, None]

CURRENCY_INR = "INR"
DEFAULT_CURRENCY = CURRENCY_INR

# Absolute INR scale multipliers — applied only when the unit word is present in text.
SCALE_WORDS = {
    "thousand": Decimal("1000"),
    "thousands": Decimal("1000"),
    "k": Decimal("1000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "cr": Decimal("10000000"),
    "million": Decimal("1000000"),
    "millions": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "billions": Decimal("1000000000"),
}

CURRENCY_SYMBOLS = {
    "₹": CURRENCY_INR,
    "inr": CURRENCY_INR,
    "rs": CURRENCY_INR,
    "rs.": CURRENCY_INR,
    "rupee": CURRENCY_INR,
    "rupees": CURRENCY_INR,
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "aed": "AED",
}

TWOPLACES = Decimal("0.01")

# Semantic types for UI / IRA (not derived from sign alone)
SEMANTIC_TYPES = (
    "REVENUE",
    "EXPENSE",
    "ASSET",
    "LIABILITY",
    "TAX_LIABILITY",
    "TAX_PAID",
    "TAX_CREDIT",
    "TAX_REFUND",
    "TAX_SAVING",
    "PROFIT",
    "LOSS",
    "CASH_INFLOW",
    "CASH_OUTFLOW",
    "NEUTRAL",
    "RISK_AVOIDED",
    "LIABILITY_DELTA",
)

INR_UNIT_LOCK = (
    "MONEY RULES (mandatory): All monetary figures are absolute Indian Rupees (INR), "
    "not lakhs/crores/thousands. Example: 1607.80 means one thousand six hundred seven rupees "
    "and eighty paise — NEVER '1607.80 lakh'. When writing prose you may say "
    "'approximately ₹1.61 thousand' ONLY as explanation of the same absolute number; "
    "never rescale JSON numeric fields. JSON money fields must be bare floats in INR "
    "(e.g. 160780.00 for one lakh sixty thousand seven hundred eighty). "
    "Do not invent exchange rates. Do not call a tax liability a 'benefit' or 'saving'."
)


class MoneyParseError(ValueError):
    pass


class ExchangeRateRequired(ValueError):
    pass


def D(value: NumberLike, default: Decimal = Decimal("0")) -> Decimal:
    """Safe Decimal from int/float/str/Decimal. Floats go via str to reduce binary noise."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return default


def money_round(value: NumberLike) -> Decimal:
    return D(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def to_float(value: NumberLike) -> float:
    """ORM / JSON bridge — still 2-dp INR."""
    return float(money_round(value))


def money_dict(
    amount: NumberLike,
    *,
    currency: str = DEFAULT_CURRENCY,
    semantic_type: str = "NEUTRAL",
    label: str = "",
) -> dict:
    amt = money_round(amount)
    st = semantic_type if semantic_type in SEMANTIC_TYPES else "NEUTRAL"
    return {
        "amount": float(amt),
        "currency": (currency or DEFAULT_CURRENCY).upper(),
        "type": st,
        "label": label or "",
        "display": format_inr(amt, currency=(currency or DEFAULT_CURRENCY).upper()),
    }


def _indian_group(integer_str: str) -> str:
    if len(integer_str) <= 3:
        return integer_str
    last3 = integer_str[-3:]
    rest = integer_str[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts + [last3])


def format_inr(
    amount: NumberLike,
    *,
    currency: str = DEFAULT_CURRENCY,
    symbol: bool = True,
    compact: bool = False,
) -> str:
    """
    Display-only Indian formatting. Never changes the numeric value.
    compact=True may show ₹1.61K / ₹1.61L / ₹2.00Cr as aliases of the same amount.
    """
    amt = money_round(amount)
    cur = (currency or DEFAULT_CURRENCY).upper()
    if compact:
        abs_amt = abs(amt)
        sign = "-" if amt < 0 else ""
        prefix = "₹" if symbol and cur == CURRENCY_INR else (f"{cur} " if symbol else "")
        if abs_amt >= Decimal("10000000"):
            return f"{sign}{prefix}{money_round(abs_amt / Decimal('10000000'))}Cr"
        if abs_amt >= Decimal("100000"):
            return f"{sign}{prefix}{money_round(abs_amt / Decimal('100000'))}L"
        if abs_amt >= Decimal("1000"):
            return f"{sign}{prefix}{money_round(abs_amt / Decimal('1000'))}K"
        # fall through to full format

    negative = amt < 0
    abs_amt = abs(amt)
    whole = int(abs_amt)
    paise = int((abs_amt - Decimal(whole)) * 100)
    grouped = _indian_group(str(whole))
    body = f"{grouped}.{paise:02d}"
    if negative:
        body = f"-{body}"
    if not symbol:
        return body
    if cur == CURRENCY_INR:
        return f"₹{body}"
    return f"{cur} {body}"


def detect_currency(text: str) -> str:
    low = (text or "").lower()
    for token, code in CURRENCY_SYMBOLS.items():
        if token in ("$", "€", "£", "₹"):
            if token in (text or ""):
                return code
        elif token in low.split() or f" {token} " in f" {low} ":
            return code
    if "₹" in (text or "") or "rupee" in low or "inr" in low or "rs" in low:
        return CURRENCY_INR
    return DEFAULT_CURRENCY


def parse_money(
    raw: NumberLike,
    *,
    default_currency: str = DEFAULT_CURRENCY,
    allow_scale_words: bool = True,
) -> dict:
    """
    Parse a number or natural-language money string into absolute currency units.

    Returns:
      { amount: Decimal, currency: str, scale: str|None, original: str }

    Scale words only apply when present in the text (e.g. "1.6 lakh" → 160000).
    Plain "1607.80" or "₹1,607.80" → 1607.80 INR (NOT lakhs).
    """
    if isinstance(raw, (int, float, Decimal)):
        return {
            "amount": money_round(raw),
            "currency": default_currency,
            "scale": None,
            "original": str(raw),
        }

    text = str(raw or "").strip()
    if not text:
        raise MoneyParseError("Empty money value")

    original = text
    currency = detect_currency(text) or default_currency

    # Strip currency tokens for numeric extraction
    cleaned = text
    for sym in ("₹", "$", "€", "£"):
        cleaned = cleaned.replace(sym, " ")
    # Remove thousands separators (Indian/Western) without splitting the number
    cleaned_for_num = cleaned.replace(",", "")
    low = cleaned.lower()

    scale = None
    scale_factor = Decimal("1")
    if allow_scale_words:
        tokens = low.replace("/", " ").replace(",", " ").split()
        for word, factor in sorted(SCALE_WORDS.items(), key=lambda x: -len(x[0])):
            if word in tokens:
                scale = word
                scale_factor = factor
                break

    import re
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned_for_num)
    if not m:
        raise MoneyParseError(f"Could not parse money from: {original!r}")
    try:
        base = Decimal(m.group(0))
    except InvalidOperation as e:
        raise MoneyParseError(f"Invalid number in: {original!r}") from e

    amount = money_round(base * scale_factor)
    return {
        "amount": amount,
        "currency": currency.upper(),
        "scale": scale,
        "original": original,
    }


def to_inr(
    amount: NumberLike,
    currency: str = DEFAULT_CURRENCY,
    *,
    exchange_rate: Optional[NumberLike] = None,
) -> Decimal:
    """
    Normalize to INR. Foreign currencies require an explicit verified exchange_rate
    (units of INR per 1 unit of foreign currency). Never invents rates.
    """
    amt = money_round(amount)
    cur = (currency or DEFAULT_CURRENCY).upper()
    if cur == CURRENCY_INR:
        return amt
    if exchange_rate is None:
        raise ExchangeRateRequired(
            f"Cannot convert {cur} to INR without a verified exchange rate. "
            "Do not invent rates."
        )
    return money_round(amt * D(exchange_rate))


def sanitize_ai_amount(value: Any, *, field_name: str = "amount") -> float:
    """
    Coerce Gemini/OCR JSON money fields to absolute INR float.
    Rejects absurd rescale if a scale word slipped into a numeric string.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float, Decimal)):
        return to_float(value)
    text = str(value).strip()
    # If pure number (optional ₹ and commas), parse without treating as lakh
    try:
        parsed = parse_money(text, allow_scale_words=True)
    except MoneyParseError:
        return 0.0
    if parsed["currency"] not in (CURRENCY_INR, "INR"):
        # Do not silently convert FX from AI text
        return 0.0
    return to_float(parsed["amount"])


def structured_finance_block(snap: dict) -> dict:
    """Structured snapshot for IRA / APIs — numbers first, display secondary."""
    def m(key, semantic, label):
        return money_dict(snap.get(key, 0), semantic_type=semantic, label=label)

    net = D(snap.get("net_gst_payable", 0))
    tax_old = D(snap.get("total_tax_old", 0))
    return {
        "currency": CURRENCY_INR,
        "unit": "absolute_INR",
        "unit_note": "All amounts are absolute rupees. 1607.80 means ₹1,607.80 — not lakhs.",
        "turnover": m("sales_total", "REVENUE", "Recorded turnover (sales)"),
        "operating_costs": m("expenses_total", "EXPENSE", "Operating costs (business expenses + annualised payroll)"),
        "profit": m(
            "profit_total",
            "PROFIT" if D(snap.get("profit_total", 0)) >= 0 else "LOSS",
            "Estimated profit",
        ),
        "output_gst": m("output_gst", "TAX_LIABILITY", "Output GST collected"),
        "eligible_itc": m("eligible_itc", "TAX_CREDIT", "Eligible input tax credit"),
        "net_gst": money_dict(
            net,
            semantic_type="TAX_LIABILITY" if net >= 0 else "TAX_CREDIT",
            label="Net GST payable" if net >= 0 else "Net GST credit (carry forward)",
        ),
        "taxable_income_old": m("taxable_old", "NEUTRAL", "Taxable income (old regime working)"),
        "estimated_tax_liability_old": m("total_tax_old", "TAX_LIABILITY", "Estimated tax liability (old regime)"),
        "estimated_tax_liability_115baa": m("total_tax_new", "TAX_LIABILITY", "Estimated tax liability (115BAA working)"),
        "primary_estimated_liability": money_dict(
            tax_old,
            semantic_type="TAX_LIABILITY",
            label="Primary estimated income-tax liability (old-regime working)",
        ),
    }
