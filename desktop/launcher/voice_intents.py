"""
Deterministic voice intent registry for AICA navigation.

Simple nav commands must NOT go to Gemini. Uses normalization + fuzzy matching.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class VoiceIntent:
    name: str
    path: str
    label: str
    speak: str


@dataclass
class IntentMatch:
    intent: VoiceIntent
    score: float
    method: str  # exact | contains | fuzzy


INTENTS: list[VoiceIntent] = [
    VoiceIntent("OPEN_DASHBOARD", "/", "Dashboard", "Sure, opening your dashboard."),
    VoiceIntent("OPEN_EXPENSES", "/expenses", "Expenses", "Sure, opening your expenses."),
    VoiceIntent("OPEN_SALES", "/sales", "Sales", "Sure, opening sales."),
    VoiceIntent("OPEN_POS", "/pos", "POS", "Sure, switching you to POS."),
    VoiceIntent("OPEN_POS_CHECKOUT", "/pos#checkout", "Checkout", "Sure, opening POS checkout."),
    VoiceIntent("OPEN_POS_OVERVIEW", "/pos#overview", "Sales Overview", "Sure, opening sales overview."),
    VoiceIntent("OPEN_POS_HISTORY", "/pos#history", "Sales History", "Sure, opening sales history."),
    VoiceIntent("OPEN_INVOICES", "/pos#invoices", "Invoices", "Sure, opening invoices."),
    VoiceIntent("OPEN_POS_PRODUCTS", "/pos#products", "Product Performance", "Sure, opening product performance."),
    VoiceIntent("OPEN_INVENTORY", "/warehouse", "Inventory", "Sure, opening inventory."),
    VoiceIntent("OPEN_BILLING", "/pos#checkout", "Billing", "Sure, opening the billing counter."),
    VoiceIntent("OPEN_REPORTS", "/reports", "Reports", "Sure, opening reports."),
    VoiceIntent("OPEN_ANALYTICS", "/sales", "Sales Analytics", "Sure, opening sales analytics."),
    VoiceIntent("OPEN_POS_ANALYTICS", "/pos#analytics", "POS Analytics", "Sure, opening POS analytics."),
    VoiceIntent(
        "OPEN_ORGANIZATION",
        "/",
        "Organization",
        "Sure, switching to organization.",
    ),
    VoiceIntent(
        "OPEN_ORGANIZATION_SETTINGS",
        "/organization",
        "Organization Settings",
        "Sure, opening organization settings.",
    ),
    VoiceIntent(
        "OPEN_INTERFACE",
        "/select-interface",
        "Interface",
        "Sure, opening interface selection.",
    ),
]

# Canonical phrases per intent (lowercase). Used for fuzzy matching.
INTENT_PHRASES: dict[str, list[str]] = {
    "OPEN_DASHBOARD": [
        "open dashboard",
        "go to dashboard",
        "show dashboard",
        "take me to dashboard",
        "show me the dashboard",
        "main screen",
        "home page",
        "go home",
    ],
    "OPEN_EXPENSES": [
        "open expenses",
        "open expense",
        "go to expenses",
        "show expenses",
        "take me to expenses",
        "take me to the expenses page",
        "can you take me to the expenses page",
        "show me expenses",
        "expense ledger",
    ],
    "OPEN_SALES": [
        "open sales",
        "go to sales",
        "show sales",
        "take me to sales",
        "take me to the sales page",
        "show me sales",
    ],
    "OPEN_POS": [
        "switch to pos",
        "open pos",
        "go to pos",
        "take me to pos",
        "point of sale",
        "open scanner",
    ],
    "OPEN_POS_CHECKOUT": [
        "open checkout",
        "go to checkout",
        "show checkout",
        "take me to checkout",
        "new sale",
        "open new sale",
    ],
    "OPEN_POS_OVERVIEW": [
        "open sales overview",
        "go to sales overview",
        "show sales overview",
        "take me to sales overview",
        "pos overview",
        "open overview",
    ],
    "OPEN_POS_HISTORY": [
        "open sales history",
        "go to sales history",
        "show sales history",
        "take me to sales history",
        "open history",
        "show history",
        "pos history",
    ],
    "OPEN_INVOICES": [
        "open invoice",
        "open invoices",
        "go to invoice",
        "go to invoices",
        "show invoice",
        "show invoices",
        "take me to invoice",
        "take me to invoices",
        "open the invoices tab",
        "show me the invoice section",
        "show me invoices",
        "show the invoices",
        "invoices tab",
        "invoice section",
        "pos invoices",
    ],
    "OPEN_POS_PRODUCTS": [
        "open product performance",
        "show product performance",
        "go to product performance",
        "take me to product performance",
        "open products",
        "show products",
        "pos products",
    ],
    "OPEN_BILLING": [
        "open billing",
        "go to billing",
        "billing counter",
        "open the billing counter",
        "show billing",
        "take me to billing",
    ],
    "OPEN_INVENTORY": [
        "open inventory",
        "open warehouse",
        "go to inventory",
        "show inventory",
        "show stock",
        "take me to inventory",
    ],
    "OPEN_REPORTS": [
        "open reports",
        "go to reports",
        "show reports",
        "take me to reports",
    ],
    "OPEN_ANALYTICS": [
        "open organization analytics",
        "open organisation analytics",
        "open sales analytics",
        "organization analytics",
        "organisation analytics",
        "sales analytics",
        "show sales analytics",
        "take me to sales analytics",
        "go to sales analytics",
        "show organization analytics",
    ],
    "OPEN_POS_ANALYTICS": [
        "open pos analytics",
        "go to pos analytics",
        "show pos analytics",
        "take me to pos analytics",
        "pos analytics",
        "open analytics",
        "show analytics",
        "take me to analytics",
        "go to analytics",
        "show me analytics",
        "here are open analytics",
    ],
    "OPEN_ORGANIZATION": [
        "switch to organization",
        "switch to organisation",
        "open organization",
        "open organisation",
        "go to organization",
        "go to organisation",
        "take me to organization",
        "take me to organisation",
        "organization home",
        "organisation home",
        "organization dashboard",
        "organisation dashboard",
    ],
    "OPEN_ORGANIZATION_SETTINGS": [
        "open organization settings",
        "open organisation settings",
        "go to organization settings",
        "go to organisation settings",
        "take me to organization settings",
        "take me to organisation settings",
        "organization settings",
        "organisation settings",
        "company settings",
        "open settings",
        "go to settings",
        "show settings",
    ],
    "OPEN_INTERFACE": [
        "switch to organization interface",
        "switch to organisation interface",
        "switch interface",
        "select interface",
    ],
}

WAKE_RE = re.compile(
    r"\b(hey|hay|hi|he)\s*[,.\-]?\s*"
    r"(ira|aira|aaira|aida|eira|era|ara|eera|eye\s*ra)\b"
    r"|\bheira\b|\bhaira\b|\bhey\s+ira\b",
    re.I,
)

_ASSISTANT_BLOCK = frozenset({"siri", "google", "alexa", "cortana", "bixby"})
_WAKE_STARTERS = frozenset({"hey", "hay", "hi", "he"})
_IRA_VARIANTS = ("ira", "aira", "aaira", "aida", "eira", "era", "eera", "ara", "eye ra", "i ra")

# Common STT mis-hears mapped before matching
_HOMOPHONE_FIXES = (
    (re.compile(r"\bopen\s+6\b", re.I), "open sales"),
    (re.compile(r"\bopen\s+sails\b", re.I), "open sales"),
    (re.compile(r"\bopen\s+viewing\b", re.I), "open billing"),
    (re.compile(r"\bopen\s+building\b", re.I), "open billing"),
    (re.compile(r"\bhere are open\b", re.I), "open"),
    (re.compile(r"\bhere are your open\b", re.I), "open"),
    (re.compile(r"\bhere i have opened\b", re.I), "open"),
    (re.compile(r"\bup\s+and\s+", re.I), "open "),
    (re.compile(r"\bup\s+in\s+", re.I), "open "),
    (re.compile(r"\bswitch\s+to\s+bls\b", re.I), "switch to pos"),
    (re.compile(r"\bswitch\s+to\s+boss\b", re.I), "switch to pos"),
    (re.compile(r"\bswitch\s+to\s+the\s+u\.?s\.?\b", re.I), "switch to pos"),
    (re.compile(r"\borganisation\b", re.I), "organization"),
)

FUZZY_THRESHOLD = 0.72
CONTAINS_MIN_LEN = 4


def _intent_by_name(name: str) -> VoiceIntent | None:
    for it in INTENTS:
        if it.name == name:
            return it
    return None


def normalize_transcript(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.lower().strip()
    t = WAKE_RE.sub(" ", t)
    for pat, repl in _HOMOPHONE_FIXES:
        t = pat.sub(repl, t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(
        r"\b(can you|could you|please|just|the|a|an|my|me to|to the|page)\b",
        " ",
        t,
    )
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_wake(text: str) -> str:
    return WAKE_RE.sub(" ", text or "").strip()


def _fuzzy_wake_match(text: str) -> bool:
    """Catch STT variants like 'Hey, I already' for short wake-only clips."""
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    words = t.split()
    if any(w in _ASSISTANT_BLOCK for w in words):
        return False
    if words[0] not in _WAKE_STARTERS:
        return False
    if len(words) > 5:
        return False
    prefix = " ".join(words[:3])
    for variant in _IRA_VARIANTS:
        target = f"{words[0]} {variant}"
        if SequenceMatcher(None, prefix, target).ratio() >= 0.72:
            return True
        if variant.replace(" ", "") in prefix.replace(" ", ""):
            return True
    tail = " ".join(words[1:3])
    if "ira" in tail or "aira" in tail or " era" in f" {tail}":
        return True
    if len(words) >= 2 and words[1] in ("ira", "aira", "aaira", "aida", "eira", "era", "eera"):
        return True
    if len(words) >= 3 and words[1] == "i" and words[2][:2] in ("ir", "ar", "er", "al"):
        return True
    return False


def detect_wake(text: str) -> bool:
    if WAKE_RE.search(text or ""):
        return True
    return _fuzzy_wake_match(text)


def match_intent(text: str, *, ui_mode: str = "org") -> IntentMatch | None:
    """Return best navigation intent or None (caller may route to Gemini)."""
    raw = strip_wake(text)
    norm = normalize_transcript(raw)
    if not norm:
        return None

    mode = (ui_mode or "org").strip().lower()
    if mode not in ("pos", "org"):
        mode = "org"

    best: IntentMatch | None = None

    for intent_name, phrases in INTENT_PHRASES.items():
        intent = _intent_by_name(intent_name)
        if not intent:
            continue
        # Context-sensitive destinations.
        path = intent.path
        speak = intent.speak
        label = intent.label
        if intent_name == "OPEN_POS_ANALYTICS":
            # Bare "open analytics" follows UI mode; explicit POS phrases always POS.
            if mode != "pos" and not re.search(r"\bpos\b", norm):
                continue
        if intent_name == "OPEN_ANALYTICS":
            # Org/sales analytics only for explicit org/sales phrasing, or when in org mode
            # for leftover matches (OPEN_POS_ANALYTICS skipped in org for bare "analytics").
            pass
        if mode == "pos" and intent_name == "OPEN_SALES":
            path = "/pos#overview"
            speak = "Sure, opening sales overview."
            label = "Sales Overview"

        intent = VoiceIntent(intent.name, path, label, speak)

        for phrase in phrases:
            if norm == phrase:
                cand = IntentMatch(intent=intent, score=1.0, method="exact")
                if not best or cand.score > best.score:
                    best = cand
                continue
            if phrase in norm or norm in phrase:
                if len(phrase) >= CONTAINS_MIN_LEN:
                    score = min(len(phrase), len(norm)) / max(len(phrase), len(norm))
                    cand = IntentMatch(intent=intent, score=0.85 + 0.1 * score, method="contains")
                    if not best or cand.score > best.score:
                        best = cand
                    continue
            ratio = SequenceMatcher(None, norm, phrase).ratio()
            if ratio >= FUZZY_THRESHOLD:
                cand = IntentMatch(intent=intent, score=ratio, method="fuzzy")
                if not best or cand.score > best.score:
                    best = cand

    # Explicit analytics routing (overrides fuzzy collisions).
    if re.search(r"\banalytics\b", norm):
        if re.search(r"\b(organization|organisation|sales)\b", norm) and not re.search(
            r"\bpos\b", norm
        ):
            alt = _intent_by_name("OPEN_ANALYTICS")
            if alt:
                best = IntentMatch(intent=alt, score=1.0, method="org_analytics_priority")
        elif re.search(r"\bpos\b", norm) or mode == "pos":
            alt = _intent_by_name("OPEN_POS_ANALYTICS")
            if alt:
                best = IntentMatch(intent=alt, score=1.0, method="pos_analytics_priority")
        else:
            # Org mode bare "open analytics" → organization sales analytics page.
            alt = _intent_by_name("OPEN_ANALYTICS")
            if alt:
                best = IntentMatch(intent=alt, score=1.0, method="org_analytics_default")

    # Prefer analytics when OPEN_SALES matched but user said analytics.
    if best and best.intent.name == "OPEN_SALES" and re.search(r"\banalytics\b", norm):
        if mode == "pos" or re.search(r"\bpos\b", norm):
            alt = _intent_by_name("OPEN_POS_ANALYTICS")
        else:
            alt = _intent_by_name("OPEN_ANALYTICS")
        if alt:
            best = IntentMatch(intent=alt, score=max(best.score, 0.95), method="analytics_priority")

    # Invoice phrases must navigate to POS Invoices tab — never Gemini chat.
    if re.search(r"\binvoices?\b", norm):
        inv = _intent_by_name("OPEN_INVOICES")
        if inv:
            best = IntentMatch(intent=inv, score=1.0, method="invoice_priority")

    # Settings phrases must never resolve to org dashboard.
    if re.search(r"\bsettings?\b", norm) and re.search(
        r"\b(organization|organisation|company)\b", norm
    ):
        alt = _intent_by_name("OPEN_ORGANIZATION_SETTINGS")
        if alt:
            best = IntentMatch(intent=alt, score=1.0, method="org_settings_priority")
    elif re.search(r"\b(open|go to|show)\s+settings\b", norm) or norm in (
        "open settings",
        "go to settings",
        "show settings",
    ):
        alt = _intent_by_name("OPEN_ORGANIZATION_SETTINGS")
        if alt:
            best = IntentMatch(intent=alt, score=1.0, method="settings_priority")

    return best


def intent_response_text(match: IntentMatch) -> str:
    return match.intent.speak
