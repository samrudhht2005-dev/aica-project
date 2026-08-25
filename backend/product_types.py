"""Product sale/inventory type: loose (by weight) vs packaged (by piece).

Stock remains Float in the DB for both types. Packaged products enforce
whole-number semantics in validation only — no destructive schema rewrite.
"""
from __future__ import annotations

from typing import Optional

PRODUCT_TYPE_LOOSE = "loose"
PRODUCT_TYPE_PACKAGED = "packaged"
PRODUCT_TYPES = frozenset({PRODUCT_TYPE_LOOSE, PRODUCT_TYPE_PACKAGED})

# Existing rows / unspecified → packaged (safer quantity semantics).
DEFAULT_PRODUCT_TYPE = PRODUCT_TYPE_PACKAGED

UNIT_KG = "kg"
UNIT_PIECE = "unit"

_INT_EPS = 1e-9


class ProductTypeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_product_type(raw: Optional[str], *, required: bool = True) -> str:
    value = (raw or "").strip().lower()
    if not value:
        if required:
            raise ProductTypeError(
                "product_type_required",
                "Select product type: Loose (sold by weight) or Packaged (sold by quantity).",
            )
        return DEFAULT_PRODUCT_TYPE
    if value in ("loose", "weight", "weigh", "kg"):
        return PRODUCT_TYPE_LOOSE
    if value in ("packaged", "package", "packed", "unit", "piece", "qty", "quantity"):
        return PRODUCT_TYPE_PACKAGED
    raise ProductTypeError(
        "invalid_product_type",
        "Product type must be 'loose' or 'packaged'.",
    )


def product_type_of(product) -> str:
    """Read type from a Product ORM row (or dict), defaulting safely."""
    if product is None:
        return DEFAULT_PRODUCT_TYPE
    if isinstance(product, dict):
        raw = product.get("product_type")
    else:
        raw = getattr(product, "product_type", None)
    try:
        return normalize_product_type(raw, required=False)
    except ProductTypeError:
        return DEFAULT_PRODUCT_TYPE


def sale_unit_for(product_type: str) -> str:
    return UNIT_KG if product_type_of({"product_type": product_type}) == PRODUCT_TYPE_LOOSE else UNIT_PIECE


def is_whole_number(value: float) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if not (v == v):  # NaN
        return False
    return abs(v - round(v)) <= _INT_EPS


def parse_quantity(raw, *, field: str = "quantity") -> float:
    try:
        q = float(raw)
    except (TypeError, ValueError) as e:
        raise ProductTypeError("invalid_quantity", f"Enter a valid {field}.") from e
    if q != q:  # NaN
        raise ProductTypeError("invalid_quantity", f"Enter a valid {field}.")
    return q


def validate_stock_value(product_type: str, stock, *, allow_zero: bool = True) -> float:
    """Validate a stock level or stock delta for the given product type."""
    q = parse_quantity(stock, field="stock")
    if allow_zero:
        if q < 0:
            raise ProductTypeError("invalid_stock", "Stock cannot be negative.")
    else:
        if q <= 0:
            raise ProductTypeError("invalid_stock", "Stock must be greater than zero.")

    ptype = normalize_product_type(product_type, required=False)
    if ptype == PRODUCT_TYPE_PACKAGED and not is_whole_number(q):
        raise ProductTypeError(
            "fractional_packaged_stock",
            "Packaged products must use whole-number stock (pieces). Example: 1, 2, 50 — not 2.5.",
        )
    if ptype == PRODUCT_TYPE_PACKAGED:
        return float(int(round(q)))
    return float(q)


def validate_sale_quantity(product_type: str, quantity) -> float:
    """Validate a POS/checkout sale quantity (must be > 0)."""
    q = parse_quantity(quantity, field="quantity")
    if q <= 0:
        raise ProductTypeError("invalid_quantity", "Enter a valid quantity greater than zero.")
    ptype = normalize_product_type(product_type, required=False)
    if ptype == PRODUCT_TYPE_PACKAGED and not is_whole_number(q):
        raise ProductTypeError(
            "fractional_packaged_qty",
            "Packaged products must be sold in whole pieces. Example: 1, 2, 5 — not 2.5.",
        )
    if ptype == PRODUCT_TYPE_PACKAGED:
        return float(int(round(q)))
    return float(q)


def assert_loose_for_weigh(product) -> str:
    ptype = product_type_of(product)
    if ptype != PRODUCT_TYPE_LOOSE:
        name = getattr(product, "name", None) or "This product"
        raise ProductTypeError(
            "not_loose",
            f"{name} is packaged and cannot be weighed. Only loose (by-weight) products can generate QR weigh tickets.",
        )
    return ptype


def can_change_product_type(*, current_type: str, new_type: str, current_stock: float) -> None:
    """Reject unsafe type changes that would silently corrupt fractional stock."""
    cur = normalize_product_type(current_type, required=False)
    nxt = normalize_product_type(new_type, required=True)
    if cur == nxt:
        return
    stock = float(current_stock or 0)
    if cur == PRODUCT_TYPE_LOOSE and nxt == PRODUCT_TYPE_PACKAGED:
        if not is_whole_number(stock):
            raise ProductTypeError(
                "type_change_fractional_stock",
                (
                    f"Cannot change to Packaged while stock is {stock:g} "
                    "(not a whole number). Adjust stock to a whole quantity first, then change type."
                ),
            )


def format_stock_display(product_type: str, stock: float) -> str:
    ptype = product_type_of({"product_type": product_type})
    s = float(stock or 0)
    if ptype == PRODUCT_TYPE_PACKAGED:
        if is_whole_number(s):
            return f"{int(round(s))} units"
        return f"{s:g} units"
    if is_whole_number(s):
        return f"{int(round(s))} kg"
    return f"{s:.3f}".rstrip("0").rstrip(".") + " kg"


def price_label(product_type: str) -> str:
    return "Price per kg" if product_type_of({"product_type": product_type}) == PRODUCT_TYPE_LOOSE else "Price per unit"
