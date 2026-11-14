"""Payments: booking != visit != payment (ТЗ §14.1-14.2).

* A payment is money actually received (status confirmed). Partial, prepayment and multiple payments per
  booking/visit are allowed. Refunds are separate rows (kind="refund", positive amount, refund_of_id) dated by
  their own paid_at, so revenue(period) = Σ confirmed payments − Σ refunds, each by its own date (AT-14).
* An original payment keeps counting as revenue on its own date even if later refunded
  (status becomes "refunded" for display only when fully refunded).
* cancel = the row was entered by mistake: it disappears from every report.
* Finance actions are audited with separate ``finance.*`` actions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Booking, Payment, Tenant, Visit
from app.services.audit import audit
from app.services.common import DomainError, emit_event, get_or_404, utcnow

PAYMENT_KINDS = ("payment", "prepayment", "adjustment")
METHODS = ("cash", "card", "transfer", "online")
REVENUE_STATUSES = ("confirmed", "refunded")  # statuses of non-refund rows that count as received money
CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(422, "BAD_AMOUNT", "Amount is not a valid number") from exc
    if d <= 0:
        raise DomainError(422, "BAD_AMOUNT", "Amount must be positive")
    return d


def refunded_amount(db: Session, payment_id: uuid.UUID) -> Decimal:
    return Decimal(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.refund_of_id == payment_id, Payment.kind == "refund", Payment.status == "confirmed"
            )
        )
        or 0
    )


def booking_paid_amount(db: Session, tenant_id: uuid.UUID, booking_id: uuid.UUID) -> Decimal:
    paid = (
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.tenant_id == tenant_id,
                Payment.booking_id == booking_id,
                Payment.kind != "refund",
                Payment.status.in_(REVENUE_STATUSES),
            )
        )
        or 0
    )
    refunds = (
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.tenant_id == tenant_id,
                Payment.booking_id == booking_id,
                Payment.kind == "refund",
                Payment.status == "confirmed",
            )
        )
        or 0
    )
    return Decimal(paid) - Decimal(refunds)


def payment_to_dict(db: Session, p: Payment) -> dict[str, Any]:
    return {
        "id": p.id,
        "booking_id": p.booking_id,
        "visit_id": p.visit_id,
        "contact_id": p.contact_id,
        "branch_id": p.branch_id,
        "staff_id": p.staff_id,
        "kind": p.kind,
        "amount": str(p.amount),
        "currency": p.currency,
        "method": p.method,
        "status": p.status,
        "confirmation_source": p.confirmation_source,
        "transaction_ref": p.transaction_ref,
        "paid_at": p.paid_at,
        "comment": p.comment,
        "created_by": p.created_by,
        "refund_of_id": p.refund_of_id,
        "refunded_amount": str(refunded_amount(db, p.id)) if p.kind != "refund" else None,
        "created_at": p.created_at,
    }


def _event_payload(p: Payment) -> dict[str, Any]:
    return {
        "payment_id": str(p.id),
        "kind": p.kind,
        "amount": str(p.amount),
        "currency": p.currency,
        "method": p.method,
        "booking_id": str(p.booking_id) if p.booking_id else None,
        "visit_id": str(p.visit_id) if p.visit_id else None,
        "refund_of_id": str(p.refund_of_id) if p.refund_of_id else None,
        "branch_id": str(p.branch_id) if p.branch_id else None,
        "paid_at": p.paid_at.isoformat(),
    }


def confirm_payment(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    amount: Any,
    method: str = "cash",
    kind: str = "payment",
    booking_id: uuid.UUID | None = None,
    visit_id: uuid.UUID | None = None,
    transaction_ref: str | None = None,
    comment: str = "",
    actor_id: uuid.UUID | None = None,
    paid_at: datetime | None = None,
    confirmation_source: str = "admin",
) -> Payment:
    """Record received money for a booking and/or visit. Same transaction_ref -> same payment (idempotent)."""
    if kind not in PAYMENT_KINDS:
        raise DomainError(422, "BAD_PAYMENT_KIND", f"kind must be one of {PAYMENT_KINDS}")
    if method not in METHODS:
        raise DomainError(422, "BAD_METHOD", f"method must be one of {METHODS}")
    amt = money(amount)
    if not booking_id and not visit_id:
        raise DomainError(422, "PAYMENT_TARGET_REQUIRED", "booking_id or visit_id is required")
    if transaction_ref:
        existing = db.scalar(
            select(Payment).where(
                Payment.tenant_id == tenant_id,
                Payment.transaction_ref == transaction_ref,
                Payment.kind != "refund",
            )
        )
        if existing is not None:
            return existing
    booking = get_or_404(db, Booking, booking_id, tenant_id) if booking_id else None
    visit = get_or_404(db, Visit, visit_id, tenant_id) if visit_id else None
    if visit is not None and booking is None and visit.booking_id:
        booking = db.get(Booking, visit.booking_id)
    if booking is not None and visit is not None and visit.booking_id not in (None, booking.id):
        raise DomainError(422, "VISIT_BOOKING_MISMATCH", "Visit belongs to another booking")
    if (
        booking is not None
        and booking.status in ("held", "cancelled", "rescheduled")
        and kind != "prepayment"
    ):
        raise DomainError(409, "BOOKING_NOT_PAYABLE", f"Booking is {booking.status}")
    src = visit or booking
    tenant = db.get(Tenant, tenant_id)
    p = Payment(
        tenant_id=tenant_id,
        booking_id=booking.id if booking else None,
        visit_id=visit.id if visit else None,
        contact_id=src.contact_id,
        branch_id=src.branch_id,
        staff_id=src.staff_id,
        kind=kind,
        amount=amt,
        currency=tenant.currency or "KZT",
        method=method,
        status="confirmed",
        confirmation_source=confirmation_source,
        transaction_ref=transaction_ref or None,
        paid_at=paid_at or utcnow(),
        comment=comment or "",
        created_by=actor_id,
    )
    db.add(p)
    db.flush()
    audit(
        db,
        tenant_id,
        "user" if actor_id else "system",
        str(actor_id) if actor_id else None,
        "finance.payment.confirmed",
        "payment",
        str(p.id),
        {"amount": str(amt), "method": method, "kind": kind, "booking_id": str(p.booking_id or "")},
    )
    emit_event(db, tenant_id, "payment.confirmed", "payment", p.id, _event_payload(p))
    return p


def refund_payment(
    db: Session,
    tenant_id: uuid.UUID,
    payment_id: uuid.UUID,
    *,
    amount: Any,
    reason: str,
    actor_id: uuid.UUID | None = None,
    method: str | None = None,
    paid_at: datetime | None = None,
) -> Payment:
    """Refund (part of) a confirmed payment; total refunds can never exceed the paid amount."""
    db.flush()
    get_or_404(db, Payment, payment_id, tenant_id)
    orig = db.scalars(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if orig.kind == "refund":
        raise DomainError(422, "CANNOT_REFUND_REFUND", "A refund cannot be refunded")
    if orig.status not in REVENUE_STATUSES:
        raise DomainError(409, "PAYMENT_NOT_REFUNDABLE", f"Payment is {orig.status}")
    amt = money(amount)
    available = orig.amount - refunded_amount(db, orig.id)
    if amt > available:
        raise DomainError(
            422, "REFUND_EXCEEDS_PAID", "Refund exceeds the refundable amount", refundable=str(available)
        )
    r = Payment(
        tenant_id=tenant_id,
        booking_id=orig.booking_id,
        visit_id=orig.visit_id,
        contact_id=orig.contact_id,
        branch_id=orig.branch_id,
        staff_id=orig.staff_id,
        kind="refund",
        amount=amt,
        currency=orig.currency,
        method=method or orig.method,
        status="confirmed",
        confirmation_source="admin",
        paid_at=paid_at or utcnow(),
        comment=reason or "",
        created_by=actor_id,
        refund_of_id=orig.id,
    )
    db.add(r)
    if amt == available:
        orig.status = "refunded"
    db.flush()
    audit(
        db,
        tenant_id,
        "user" if actor_id else "system",
        str(actor_id) if actor_id else None,
        "finance.payment.refunded",
        "payment",
        str(orig.id),
        {"refund_id": str(r.id), "amount": str(amt), "reason": reason},
    )
    emit_event(db, tenant_id, "payment.refunded", "payment", r.id, _event_payload(r))
    return r


def cancel_payment(
    db: Session,
    tenant_id: uuid.UUID,
    payment_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    reason: str = "",
) -> Payment:
    """Void an erroneous entry (payment or refund). A payment with active refunds must be un-refunded first."""
    db.flush()
    get_or_404(db, Payment, payment_id, tenant_id)
    p = db.scalars(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if p.status == "cancelled":
        return p
    if p.kind != "refund" and refunded_amount(db, p.id) > 0:
        raise DomainError(409, "PAYMENT_HAS_REFUNDS", "Cancel the refunds of this payment first")
    p.status = "cancelled"
    if p.kind == "refund" and p.refund_of_id:
        orig = db.get(Payment, p.refund_of_id)
        if orig is not None and orig.status == "refunded":
            orig.status = "confirmed"
    db.flush()
    audit(
        db,
        tenant_id,
        "user" if actor_id else "system",
        str(actor_id) if actor_id else None,
        "finance.payment.cancelled",
        "payment",
        str(p.id),
        {"amount": str(p.amount), "kind": p.kind, "reason": reason},
    )
    return p
