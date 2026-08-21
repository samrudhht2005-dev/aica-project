"""
Curated AICA POS product classes for reliable recognition.

Quality over quantity: only products you can photograph and keep in stock.
Extend this list only after collecting new real photos and retraining.
"""

# Class index → inventory product name (must match Product.name in SQL)
PRODUCT_CLASSES = [
    "Ketchup",            # 0
    "Fevicol",            # 1
    "Dairy Milk",         # 2
    "Lipton Green Tea",   # 3
]

CLASS_TO_ID = {name: i for i, name in enumerate(PRODUCT_CLASSES)}
ID_TO_CLASS = {i: name for i, name in enumerate(PRODUCT_CLASSES)}

# Aliases that may appear in model labels or user typing → canonical inventory name
NAME_ALIASES = {
    "dairy milk": "Dairy Milk",
    "dairymilk": "Dairy Milk",
    "cadbury dairy milk": "Dairy Milk",
    "lipton": "Lipton Green Tea",
    "lipton green tea": "Lipton Green Tea",
    "green tea": "Lipton Green Tea",
    "tomato ketchup": "Ketchup",
    "ketchup bottle": "Ketchup",
}


def canonicalize_product_name(name: str) -> str | None:
    """Map a raw detector/Gemini label to a curated inventory name, or None."""
    if not name:
        return None
    raw = name.strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower in NAME_ALIASES:
        return NAME_ALIASES[lower]
    for cls in PRODUCT_CLASSES:
        if cls.lower() == lower:
            return cls
    return None
