"""
Secure QR weigh-ticket domain logic.

The QR / client may carry only an opaque public_token. Product, weight, and
price are always resolved from the database. Creating or resolving a ticket
never deducts stock; consumption belongs in the same DB transaction as
checkout (see claim_active_ticket_for_checkout).

Status machine (DB values):
  active  → consumed  (successful checkout claim only)
  active  → cancelled (verified cancel-by-token OR 12h timeout)
  consumed / cancelled are terminal

User-facing labels: active→Active, consumed→Redeemed, cancelled→Cancelled.
Timed-out ACTIVE tickets become CANCELLED (not a separate "Expired" status).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.money import money_round, to_float
from models.db_models import Product, WeighTicket

TOKEN_PREFIX = "AICA1."
TOKEN_BYTES = 32  # urlsafe entropy (~256 bits)

STATUS_ACTIVE = "active"
STATUS_RESERVED = "reserved"
STATUS_CONSUMED = "consumed"
STATUS_CANCELLED = "cancelled"
# Legacy: older codepaths may still have written this; treat as cancelled for UX.
STATUS_EXPIRED = "expired"

ALLOWED_STATUSES = frozenset({
    STATUS_ACTIVE,
    STATUS_RESERVED,
    STATUS_CONSUMED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
})

CANCEL_REASON_TIMEOUT = "timeout_12h"
CANCEL_REASON_VERIFIED = "verified_manual_cancel"

TICKET_TTL = timedelta(hours=12)

STATUS_LABELS = {
    STATUS_ACTIVE: "Active",
    STATUS_RESERVED: "Active",
    STATUS_CONSUMED: "Redeemed",
    STATUS_CANCELLED: "Cancelled",
    STATUS_EXPIRED: "Cancelled",
}


class WeighTicketError(Exception):
    """Domain error with a stable machine code for API / checkout."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedWeighTicket:
    ticket: WeighTicket
    product: Product


def generate_public_token() -> str:
    """High-entropy opaque token suitable for QR payloads."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def normalize_public_token(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        raise WeighTicketError("invalid_token", "Ticket token is required.")
    return token


def _utc_now() -> datetime:
    return datetime.utcnow()


def default_expires_at(*, created_at: Optional[datetime] = None) -> datetime:
    base = created_at or _utc_now()
    return base + TICKET_TTL


def status_label(status: Optional[str]) -> str:
    return STATUS_LABELS.get((status or "").strip().lower(), "Unknown")


def token_ref(public_token: Optional[str]) -> str:
    token = (public_token or "").strip()
    if len(token) <= 8:
        return token or "—"
    return "…" + token[-8:]


def maybe_expire_ticket(db: Session, ticket: WeighTicket, *, now: Optional[datetime] = None) -> WeighTicket:
    """
    Lazily cancel overdue ACTIVE/RESERVED tickets (timeout_12h).
    Atomic conditional update — never overwrites consumed/cancelled.
    No commit (caller controls transaction).
    """
    now = now or _utc_now()
    if ticket.status not in (STATUS_ACTIVE, STATUS_RESERVED):
        return ticket
    if ticket.expires_at is None or ticket.expires_at > now:
        return ticket

    updated = (
        db.query(WeighTicket)
        .filter(
            WeighTicket.id == int(ticket.id),
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
            WeighTicket.expires_at.isnot(None),
            WeighTicket.expires_at <= now,
        )
        .update(
            {
                WeighTicket.status: STATUS_CANCELLED,
                WeighTicket.cancelled_at: now,
                WeighTicket.cancel_reason: CANCEL_REASON_TIMEOUT,
            },
            synchronize_session="fetch",
        )
    )
    if updated:
        db.refresh(ticket)
    return ticket


def cancel_timed_out_tickets(db: Session, *, limit: int = 500, commit: bool = True) -> int:
    """Background sweep: ACTIVE/RESERVED past expires_at → CANCELLED (timeout_12h)."""
    now = _utc_now()
    overdue = (
        db.query(WeighTicket.id)
        .filter(
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
            WeighTicket.expires_at.isnot(None),
            WeighTicket.expires_at <= now,
        )
        .limit(max(1, int(limit)))
        .all()
    )
    if not overdue:
        return 0
    ids = [row[0] for row in overdue]
    updated = (
        db.query(WeighTicket)
        .filter(
            WeighTicket.id.in_(ids),
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
            WeighTicket.expires_at.isnot(None),
            WeighTicket.expires_at <= now,
        )
        .update(
            {
                WeighTicket.status: STATUS_CANCELLED,
                WeighTicket.cancelled_at: now,
                WeighTicket.cancel_reason: CANCEL_REASON_TIMEOUT,
            },
            synchronize_session=False,
        )
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return int(updated or 0)


def create_weigh_ticket(
    db: Session,
    *,
    org_id: int,
    product_id: int,
    weight: float,
    unit: str = "kg",
    created_by_user_id: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    commit: bool = True,
) -> WeighTicket:
    """
    Create an ACTIVE ticket using authoritative product price from the DB.
    Does not change Product.stock.
    Always assigns a 12-hour expires_at (client override ignored unless testing passes one).
    """
    unit_norm = (unit or "kg").strip().lower() or "kg"
    try:
        weight_f = float(weight)
    except (TypeError, ValueError) as e:
        raise WeighTicketError("invalid_weight", "Enter a valid weight greater than zero.") from e
    if weight_f <= 0:
        raise WeighTicketError("invalid_weight", "Enter a valid weight greater than zero.")

    product = (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.org_id == int(org_id))
        .first()
    )
    if not product:
        raise WeighTicketError("product_not_found", "Product not found in this organization.")

    from backend.product_types import ProductTypeError, assert_loose_for_weigh

    try:
        assert_loose_for_weigh(product)
    except ProductTypeError as e:
        raise WeighTicketError(e.code, e.message) from e

    unit_price = to_float(money_round(product.price or 0))
    total = to_float(money_round(unit_price * weight_f))
    created_at = _utc_now()
    # Production path always uses 12h TTL. Explicit expires_at is for tests only.
    exp = expires_at if expires_at is not None else default_expires_at(created_at=created_at)

    ticket = None
    last_err: Optional[Exception] = None
    for _ in range(5):
        candidate = WeighTicket(
            org_id=int(org_id),
            product_id=int(product.id),
            public_token=generate_public_token(),
            product_name_snapshot=str(product.name or ""),
            weight=weight_f,
            unit=unit_norm,
            unit_price_snapshot=unit_price,
            total_amount_snapshot=total,
            status=STATUS_ACTIVE,
            created_by_user_id=created_by_user_id,
            created_at=created_at,
            expires_at=exp,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            ticket = candidate
            break
        except IntegrityError as e:
            last_err = e
            ticket = None
            continue
    if ticket is None:
        raise WeighTicketError(
            "token_collision",
            "Could not allocate a unique ticket token. Try again.",
        ) from last_err

    if commit:
        db.commit()
        db.refresh(ticket)
    return ticket


def resolve_weigh_ticket(
    db: Session,
    *,
    org_id: int,
    public_token: str,
) -> ResolvedWeighTicket:
    """
    Resolve opaque token for POS. Never deducts stock. Never consumes.
    Lazily cancels overdue ACTIVE tickets before evaluating status.
    """
    token = normalize_public_token(public_token)
    ticket = db.query(WeighTicket).filter(WeighTicket.public_token == token).first()
    if not ticket:
        raise WeighTicketError("not_found", "Weigh ticket not found.")

    if int(ticket.org_id) != int(org_id):
        raise WeighTicketError("not_found", "Weigh ticket not found.")

    maybe_expire_ticket(db, ticket)

    if ticket.status == STATUS_CONSUMED:
        raise WeighTicketError(
            "already_purchased",
            "Already purchased. This weigh ticket was already used in a sale.",
        )
    if ticket.status in (STATUS_CANCELLED, STATUS_EXPIRED):
        reason = (ticket.cancel_reason or "").strip()
        if reason == CANCEL_REASON_TIMEOUT:
            raise WeighTicketError(
                "cancelled",
                "This weigh ticket was cancelled after 12 hours without redemption.",
            )
        raise WeighTicketError("cancelled", "This weigh ticket was cancelled.")
    if ticket.status == STATUS_RESERVED:
        raise WeighTicketError("reserved", "This weigh ticket is temporarily held.")
    if ticket.status != STATUS_ACTIVE:
        raise WeighTicketError("inactive", f"This weigh ticket is not active ({ticket.status}).")

    product = (
        db.query(Product)
        .filter(Product.id == ticket.product_id, Product.org_id == org_id)
        .first()
    )
    if not product:
        raise WeighTicketError("product_invalid", "The product for this ticket is no longer available.")

    return ResolvedWeighTicket(ticket=ticket, product=product)


def cancel_weigh_ticket(
    db: Session,
    *,
    org_id: int,
    ticket_id: int,
    reason: str = "",
    commit: bool = True,
) -> WeighTicket:
    """
    Internal/test helper: cancel by ticket id within org.
    Prefer cancel_weigh_ticket_by_token for user-facing cancellation.
    """
    ticket = (
        db.query(WeighTicket)
        .filter(WeighTicket.id == int(ticket_id), WeighTicket.org_id == int(org_id))
        .first()
    )
    if not ticket:
        raise WeighTicketError("not_found", "Weigh ticket not found.")
    maybe_expire_ticket(db, ticket)
    if ticket.status == STATUS_CONSUMED:
        raise WeighTicketError("already_purchased", "Cannot cancel a ticket that was already purchased.")
    if ticket.status in (STATUS_CANCELLED, STATUS_EXPIRED):
        return ticket

    now = _utc_now()
    updated = (
        db.query(WeighTicket)
        .filter(
            WeighTicket.id == int(ticket.id),
            WeighTicket.org_id == int(org_id),
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
        )
        .update(
            {
                WeighTicket.status: STATUS_CANCELLED,
                WeighTicket.cancelled_at: now,
                WeighTicket.cancel_reason: (reason or "").strip()[:500],
            },
            synchronize_session="fetch",
        )
    )
    if not updated:
        db.refresh(ticket)
        if ticket.status == STATUS_CONSUMED:
            raise WeighTicketError("already_purchased", "Cannot cancel a ticket that was already purchased.")
        if ticket.status in (STATUS_CANCELLED, STATUS_EXPIRED):
            return ticket
        raise WeighTicketError("inactive", "This weigh ticket cannot be cancelled.")
    db.refresh(ticket)
    if commit:
        db.commit()
        db.refresh(ticket)
    else:
        db.flush()
    return ticket


def cancel_weigh_ticket_by_token(
    db: Session,
    *,
    org_id: int,
    public_token: str,
    reason: str = CANCEL_REASON_VERIFIED,
    commit: bool = True,
) -> WeighTicket:
    """
    Verified manual cancellation: requires the opaque QR token (from a scan).
    Atomic ACTIVE/RESERVED → CANCELLED. Never touches stock.
    """
    token = normalize_public_token(public_token)
    if not token.startswith(TOKEN_PREFIX):
        raise WeighTicketError("invalid_token", "Scan a valid AICA weigh QR ticket.")

    ticket = (
        db.query(WeighTicket)
        .filter(WeighTicket.public_token == token, WeighTicket.org_id == int(org_id))
        .first()
    )
    if not ticket:
        # Same message for missing / wrong-org — no leakage.
        raise WeighTicketError("not_found", "Weigh ticket not found.")

    maybe_expire_ticket(db, ticket)

    if ticket.status == STATUS_CONSUMED:
        raise WeighTicketError(
            "already_purchased",
            "This ticket was already redeemed and cannot be cancelled.",
        )
    if ticket.status in (STATUS_CANCELLED, STATUS_EXPIRED):
        if (ticket.cancel_reason or "") == CANCEL_REASON_TIMEOUT:
            raise WeighTicketError(
                "cancelled",
                "This weigh ticket was already cancelled after 12 hours.",
            )
        raise WeighTicketError("cancelled", "This weigh ticket is already cancelled.")

    now = _utc_now()
    updated = (
        db.query(WeighTicket)
        .filter(
            WeighTicket.id == int(ticket.id),
            WeighTicket.org_id == int(org_id),
            WeighTicket.public_token == token,
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
        )
        .update(
            {
                WeighTicket.status: STATUS_CANCELLED,
                WeighTicket.cancelled_at: now,
                WeighTicket.cancel_reason: (reason or CANCEL_REASON_VERIFIED).strip()[:500],
            },
            synchronize_session="fetch",
        )
    )
    if not updated:
        db.refresh(ticket)
        if ticket.status == STATUS_CONSUMED:
            raise WeighTicketError(
                "already_purchased",
                "This ticket was already redeemed and cannot be cancelled.",
            )
        raise WeighTicketError("cancelled", "This weigh ticket is already cancelled.")

    db.refresh(ticket)
    if commit:
        db.commit()
        db.refresh(ticket)
    else:
        db.flush()
    return ticket


def claim_active_ticket_for_checkout(
    db: Session,
    *,
    org_id: int,
    public_token: str,
    transaction_id: Optional[int] = None,
) -> WeighTicket:
    """
    Mark an ACTIVE ticket CONSUMED in the current session (flush, no commit).

    Intended to run inside the same DB transaction as stock deduction and
    Transaction insert. Caller must commit or rollback the whole unit.

    Uses a status-conditioned update so a concurrent checkout cannot double-consume.
    """
    token = normalize_public_token(public_token)
    ticket = (
        db.query(WeighTicket)
        .filter(WeighTicket.public_token == token, WeighTicket.org_id == int(org_id))
        .first()
    )
    if not ticket:
        raise WeighTicketError("not_found", "Weigh ticket not found.")

    maybe_expire_ticket(db, ticket)

    if ticket.status == STATUS_CONSUMED:
        raise WeighTicketError(
            "already_purchased",
            "Already purchased. This weigh ticket was already used in a sale.",
        )
    if ticket.status in (STATUS_CANCELLED, STATUS_EXPIRED):
        raise WeighTicketError("cancelled", "This weigh ticket was cancelled.")
    if ticket.status not in (STATUS_ACTIVE, STATUS_RESERVED):
        raise WeighTicketError("inactive", "This weigh ticket cannot be checked out.")

    updated = (
        db.query(WeighTicket)
        .filter(
            WeighTicket.id == ticket.id,
            WeighTicket.org_id == int(org_id),
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
        )
        .update(
            {
                WeighTicket.status: STATUS_CONSUMED,
                WeighTicket.consumed_at: _utc_now(),
                WeighTicket.transaction_id: transaction_id,
            },
            synchronize_session="fetch",
        )
    )
    if not updated:
        raise WeighTicketError(
            "already_purchased",
            "Already purchased. This weigh ticket was already used in a sale.",
        )
    db.refresh(ticket)
    return ticket


def list_weigh_tickets(
    db: Session,
    *,
    org_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[WeighTicket], int]:
    """
    Org-scoped history. status filter accepts:
      all | active | redeemed | cancelled
    (redeemed → consumed; cancelled includes legacy expired)
    Lazily times out overdue rows for this org before listing.
    """
    # Lazy sweep for this org (bounded)
    now = _utc_now()
    (
        db.query(WeighTicket)
        .filter(
            WeighTicket.org_id == int(org_id),
            WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]),
            WeighTicket.expires_at.isnot(None),
            WeighTicket.expires_at <= now,
        )
        .update(
            {
                WeighTicket.status: STATUS_CANCELLED,
                WeighTicket.cancelled_at: now,
                WeighTicket.cancel_reason: CANCEL_REASON_TIMEOUT,
            },
            synchronize_session=False,
        )
    )
    db.flush()

    q = db.query(WeighTicket).filter(WeighTicket.org_id == int(org_id))
    status_n = (status or "all").strip().lower()
    if status_n in ("active",):
        q = q.filter(WeighTicket.status.in_([STATUS_ACTIVE, STATUS_RESERVED]))
    elif status_n in ("redeemed", "consumed"):
        q = q.filter(WeighTicket.status == STATUS_CONSUMED)
    elif status_n in ("cancelled", "canceled", "expired"):
        q = q.filter(WeighTicket.status.in_([STATUS_CANCELLED, STATUS_EXPIRED]))
    elif status_n not in ("", "all"):
        raise WeighTicketError("invalid_status_filter", "status must be all, active, redeemed, or cancelled.")

    total = q.count()
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    rows = q.order_by(WeighTicket.created_at.desc(), WeighTicket.id.desc()).offset(off).limit(lim).all()
    return rows, total


def ticket_public_dict(ticket: WeighTicket, *, include_token: bool = True) -> dict:
    data = {
        "id": ticket.id,
        "org_id": ticket.org_id,
        "product_id": ticket.product_id,
        "product_name": ticket.product_name_snapshot,
        "product_name_snapshot": ticket.product_name_snapshot,
        "weight": ticket.weight,
        "unit": ticket.unit,
        "unit_price": ticket.unit_price_snapshot,
        "unit_price_snapshot": ticket.unit_price_snapshot,
        "total_amount": ticket.total_amount_snapshot,
        "total_amount_snapshot": ticket.total_amount_snapshot,
        "status": ticket.status,
        "status_label": status_label(ticket.status),
        "token_ref": token_ref(ticket.public_token),
        "created_at": ticket.created_at.isoformat() + "Z" if ticket.created_at else None,
        "expires_at": ticket.expires_at.isoformat() + "Z" if ticket.expires_at else None,
        "consumed_at": ticket.consumed_at.isoformat() + "Z" if ticket.consumed_at else None,
        "cancelled_at": ticket.cancelled_at.isoformat() + "Z" if ticket.cancelled_at else None,
        "cancel_reason": ticket.cancel_reason or "",
        "transaction_id": ticket.transaction_id,
    }
    if include_token:
        data["public_token"] = ticket.public_token
        data["qr_payload"] = ticket.public_token
    return data


def resolve_error_http_status(code: str) -> int:
    if code in ("not_found", "product_not_found", "product_invalid"):
        return 404
    if code in ("already_purchased", "cancelled", "expired", "inactive", "reserved"):
        return 409
    if code in (
        "invalid_token",
        "invalid_weight",
        "invalid_expires_at",
        "token_collision",
        "not_loose",
        "invalid_status_filter",
        "cancel_requires_token",
    ):
        return 400
    return 400
