"""
Action metadata for AI Optimization cards.

Does not generate recommendations — only maps existing TaxRecommendation rows
to internal routes, verified official portals, next steps, and document checks.
"""
from __future__ import annotations

from typing import Any

# Verified official destinations only (do not invent URLs).
OFFICIAL_PORTALS = {
    "gst": {
        "id": "gst",
        "label": "GST Portal",
        "url": "https://www.gst.gov.in/",
        "purpose": "View GSTR-2B, file GSTR-1 / GSTR-3B, and manage GST registration.",
    },
    "income_tax": {
        "id": "income_tax",
        "label": "Income Tax e-Filing Portal",
        "url": "https://www.incometax.gov.in/iec/foportal/",
        "purpose": "File income-tax returns and forms such as Form 10-IC or Form 10DA.",
    },
    "epfo": {
        "id": "epfo",
        "label": "EPFO",
        "url": "https://www.epfindia.gov.in/",
        "purpose": "Check EPF employer filings and contribution records.",
    },
}

# Synonyms → availability keys computed from AICA data
DOC_SYNONYMS = [
    ("pan", ["pan"]),
    ("gst_registration", ["gst registration", "gstin", "gst details", "gst certificate", "gst registration certificate"]),
    ("purchase_invoices", ["tax invoice", "tax invoices", "purchase invoice", "purchase invoices", "invoice", "invoices", "gst invoice"]),
    ("gstr2b", ["gstr-2b", "gstr2b", "gstr 2b"]),
    ("eway", ["e-way", "eway"]),
    ("payroll", ["payroll", "payroll register", "form 12ba", "appointment letter", "joining"]),
    ("epf", ["epf", "pf contribution", "provident fund"]),
    ("asset_register", ["fixed asset", "asset register", "purchase invoice", "installation", "put-to-use"]),
    ("bank", ["bank statement", "bank statements", "bank details", "drawings"]),
    ("form_10ic", ["form 10-ic", "form 10ic", "10-ic"]),
    ("form_10da", ["form 10da", "10da"]),
    ("computation", ["computation of income", "financial statement", "financial statements"]),
]


def impact_meta_for(rule_section: str, title: str, impact_type: str | None = None) -> dict:
    """Map recommendation to semantic impact label (liability vs saving vs credit)."""
    section = rule_section or ""
    title_l = (title or "").lower()
    itype = (impact_type or "").strip().lower()
    if not itype:
        if "80JJAA" in section or "depreciation" in title_l or section.startswith("Sec 32"):
            itype = "tax_saving"
        elif "GST" in section or "itc" in title_l:
            itype = "tax_credit"
        elif "personal" in title_l:
            itype = "risk_avoided"
        elif "115BAA" in section or "regime" in title_l:
            itype = "liability_delta"
        else:
            itype = "tax_saving"

    labels = {
        "tax_saving": ("Estimated potential tax saving", "TAX_SAVING", "money-gain"),
        "tax_credit": ("Estimated eligible tax credit", "TAX_CREDIT", "money-gain"),
        "risk_avoided": ("Estimated tax exposure if wrongly claimed", "RISK_AVOIDED", "money-owe"),
        "liability_delta": ("Estimated tax liability difference (regimes)", "LIABILITY_DELTA", "money-neutral"),
        "tax_liability": ("Estimated tax liability", "TAX_LIABILITY", "money-owe"),
    }
    label, semantic, css = labels.get(itype, labels["tax_saving"])
    return {"impact_type": itype, "impact_label": label, "semantic_type": semantic, "css_class": css}


def _split_documents(raw: str) -> list[str]:
    if not raw:
        return []
    text = str(raw).replace(";", ",").replace("\n", ",")
    parts = []
    for chunk in text.split(","):
        item = chunk.strip(" •-\t")
        if item:
            parts.append(item)
    return parts


def _match_doc_key(label: str) -> str | None:
    low = label.lower()
    for key, synonyms in DOC_SYNONYMS:
        if any(s in low for s in synonyms):
            return key
    return None


def compute_availability(org, snap: dict, counts: dict | None = None) -> dict[str, bool]:
    """Only mark Ready when AICA actually has related data."""
    counts = counts or {}
    return {
        "pan": bool(org and (org.pan or "").strip()),
        "gst_registration": bool(org and ((org.gstin or "").strip() or getattr(org, "gst_registered", False))),
        "purchase_invoices": bool(counts.get("invoices", 0) > 0 or (snap or {}).get("input_gst", 0) > 0),
        "gstr2b": False,  # portal-side; AICA does not store GSTR-2B
        "eway": False,
        "payroll": bool(counts.get("employees", 0) > 0 or (snap or {}).get("eligible_count", 0) > 0),
        "epf": bool(counts.get("employees", 0) > 0),
        "asset_register": bool(counts.get("assets", 0) > 0 or (snap or {}).get("assets_cost", 0) > 0),
        "bank": bool(org and (org.bank_accounts or "").strip()),
        "form_10ic": False,
        "form_10da": False,
        "computation": False,
    }


def _base_action(
    *,
    category: str,
    icon: str,
    primary: dict | None,
    internal: dict | None = None,
    external: dict | None = None,
    next_steps: list[str],
    why_extra: str = "",
) -> dict:
    return {
        "category": category,
        "icon": icon,
        "primary": primary,
        "internal": internal,
        "external": external,
        "next_steps": next_steps,
        "why_extra": why_extra,
        "disclaimer": (
            "Figures are estimated potential amounts in absolute INR based on information currently in AICA — "
            "not guaranteed. A tax liability is money you may owe; a saving/credit is different. "
            "Verify with a qualified tax professional before filing."
        ),
    }


def action_profile_for_rule(rule_section: str, title: str) -> dict:
    section = (rule_section or "").strip()
    title_l = (title or "").lower()

    if "80JJAA" in section or "80jjaa" in title_l:
        return _base_action(
            category="Payroll Optimization",
            icon="bi-people",
            primary={"type": "internal", "label": "Open Payroll", "path": "/employees"},
            internal={"label": "Open Payroll", "path": "/employees"},
            external=OFFICIAL_PORTALS["income_tax"],
            next_steps=[
                "Review new hires and monthly emoluments on the Payroll page in AICA.",
                "Confirm each hire meets 80JJAA conditions (pay limit, days employed, PF).",
                "Gather appointment letters, payroll register, and EPF contribution proof.",
                "Prepare Form 10DA with your tax professional.",
                "File through the Income Tax e-Filing portal when ready.",
                "Keep the acknowledgement / reference number with your records.",
            ],
            why_extra="AICA compared new hire payroll against Section 80JJAA thresholds in your books.",
        )

    if section.startswith("Sec 32") or "depreciation" in title_l:
        return _base_action(
            category="Fixed Assets",
            icon="bi-building",
            primary={"type": "internal", "label": "Open Fixed Assets", "path": "/assets"},
            internal={"label": "Open Fixed Assets", "path": "/assets"},
            external=OFFICIAL_PORTALS["income_tax"],
            next_steps=[
                "Open Fixed Assets in AICA and confirm each asset is owned and put to use.",
                "Check WDV rates and capital block totals against purchase invoices.",
                "Update the fixed asset register if any asset is missing.",
                "Include the depreciation figure in your income-tax computation.",
                "File via the Income Tax e-Filing portal with your return.",
                "Retain invoices and put-to-use notes for audit support.",
            ],
            why_extra="AICA calculated Section 32 WDV depreciation from your capitalised assets.",
        )

    if "GST" in section or "itc" in title_l or "input tax" in title_l:
        return _base_action(
            category="GST Optimization",
            icon="bi-receipt",
            primary={"type": "internal", "label": "Open GST & ITC", "path": "/gst"},
            internal={"label": "Open GST & ITC", "path": "/gst"},
            external=OFFICIAL_PORTALS["gst"],
            next_steps=[
                "Open GST & ITC in AICA to review eligible vs blocked input credit.",
                "Match purchase invoices in AICA to GSTR-2B on the GST portal.",
                "Remove or reclassify personal / blocked spends so they are not claimed.",
                "Claim eligible ITC when filing GSTR-3B on the official GST portal.",
                "Keep tax invoices and the portal acknowledgement with your records.",
            ],
            why_extra="AICA separated eligible business input GST from blocked categories under Section 17(5).",
        )

    if "personal" in title_l or "P&L" in title or "IT Act / GST" in section:
        return _base_action(
            category="Expense Optimization",
            icon="bi-wallet2",
            primary={"type": "internal", "label": "Review Expenses", "path": "/expenses"},
            internal={"label": "Review Expenses", "path": "/expenses"},
            external=None,
            next_steps=[
                "Open Expenses and filter or scan for personal / owner spends.",
                "Reclassify personal items so they stay out of business P&L and ITC.",
                "Keep bank statements and drawings ledger as supporting evidence.",
                "Re-check profit and GST figures on the Dashboard after corrections.",
            ],
            why_extra="AICA found expenses marked as personal that should not reduce taxable profit or create ITC.",
        )

    if "115BAA" in section or "regime" in title_l:
        return _base_action(
            category="Tax Optimization",
            icon="bi-percent",
            primary={"type": "internal", "label": "Open Income Tax", "path": "/income-tax"},
            internal={"label": "Open Income Tax", "path": "/income-tax"},
            external=OFFICIAL_PORTALS["income_tax"],
            next_steps=[
                "Open Income Tax in AICA and compare old vs 115BAA estimates.",
                "List deductions you would give up under 115BAA before deciding.",
                "Discuss the irreversible nature of the 115BAA option with your CA.",
                "If you opt in, file Form 10-IC on the Income Tax e-Filing portal.",
                "Keep both regime computations with your tax papers.",
            ],
            why_extra="AICA compared estimated tax under the regular regime and Section 115BAA using current books.",
        )

    # Generic fallback for Gemini / unknown cards — informational + Ask IRA
    internal = None
    external = None
    primary = {"type": "details", "label": "View Details", "path": ""}
    if any(k in title_l for k in ("expense", "vendor", "bill")):
        internal = {"label": "Review Expenses", "path": "/expenses"}
        primary = {"type": "internal", "label": "Review Expenses", "path": "/expenses"}
    elif any(k in title_l for k in ("gst", "itc", "input")):
        internal = {"label": "Open GST & ITC", "path": "/gst"}
        external = OFFICIAL_PORTALS["gst"]
        primary = {"type": "internal", "label": "Open GST & ITC", "path": "/gst"}
    elif any(k in title_l for k in ("employee", "payroll", "salary", "80jj")):
        internal = {"label": "Open Payroll", "path": "/employees"}
        primary = {"type": "internal", "label": "Open Payroll", "path": "/employees"}
    elif any(k in title_l for k in ("asset", "depreciation", "capital")):
        internal = {"label": "Open Fixed Assets", "path": "/assets"}
        primary = {"type": "internal", "label": "Open Fixed Assets", "path": "/assets"}
    elif any(k in title_l for k in ("tax", "deduction", "regime")):
        internal = {"label": "Open Income Tax", "path": "/income-tax"}
        external = OFFICIAL_PORTALS["income_tax"]
        primary = {"type": "internal", "label": "Open Income Tax", "path": "/income-tax"}

    return _base_action(
        category="Tax Optimization",
        icon="bi-lightbulb",
        primary=primary,
        internal=internal,
        external=external,
        next_steps=[
            "Read the eligibility conditions and required documents on this card.",
            "Confirm the facts against your ledgers in AICA.",
            "Prepare the documents listed before taking any filing step.",
            "Complete filing only through the appropriate official channel if required.",
            "Ask IRA if you need the recommendation explained in plain language.",
        ],
        why_extra="AICA surfaced this from your organisation data and applicable Indian tax rules.",
    )


def enrich_recommendation(rec, org, snap: dict, counts: dict | None = None) -> dict[str, Any]:
    """Build a JSON-serialisable action payload for one recommendation."""
    profile = action_profile_for_rule(rec.rule_section, rec.title)
    availability = compute_availability(org, snap or {}, counts)
    docs_raw = _split_documents(rec.required_documents)
    documents = []
    for label in docs_raw:
        key = _match_doc_key(label)
        if key is None:
            status = "unknown"  # user can mark Ready/Missing themselves; AICA has no signal
            ready = None
        else:
            ready = bool(availability.get(key))
            status = "ready" if ready else "missing"
        documents.append({
            "label": label,
            "key": key or "",
            "status": status,
            "ready": ready,
        })

    fingerprint = f"{rec.rule_section}|{rec.title}".strip()
    why = (rec.reason or "").strip()
    if profile.get("why_extra"):
        why_aica = profile["why_extra"]
    else:
        why_aica = "Based on the information currently available in your AICA books."

    impact = impact_meta_for(rec.rule_section, rec.title)

    return {
        "id": rec.id,
        "fingerprint": fingerprint,
        "title": rec.title,
        "detected_item": rec.detected_item or "",
        "reason": why,
        "why_aica": why_aica,
        "rule_section": rec.rule_section,
        "eligibility_conditions": rec.eligibility_conditions or "",
        "required_documents_raw": rec.required_documents or "",
        "estimated_tax_impact": float(rec.estimated_tax_impact or 0),
        "impact_type": impact["impact_type"],
        "impact_label": impact["impact_label"],
        "semantic_type": impact["semantic_type"],
        "css_class": impact["css_class"],
        "confidence_level": float(rec.confidence_level or 0),
        "severity": rec.severity or "Medium",
        "audit_status": rec.status or "",
        "created_at": rec.created_at.isoformat() if getattr(rec, "created_at", None) else "",
        "category": profile["category"],
        "icon": profile["icon"],
        "primary": profile["primary"],
        "internal": profile["internal"],
        "external": profile["external"],
        "next_steps": profile["next_steps"],
        "disclaimer": profile["disclaimer"],
        "documents": documents,
        "currency": "INR",
        "money_unit": "absolute_INR",
    }
