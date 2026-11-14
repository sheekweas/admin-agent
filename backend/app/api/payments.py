"""Payments (ТЗ §14). Recording is finance.write (owner/admin at the front desk); listing everything needs
finance.read; refund/cancel change the books and are owner-only (finance.read + finance.write)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth, require
from app.models import Payment
from app.services import payments as svc
from app.services.common import DomainError, get_or_404, tz
from app.services.reports import default_tz_name

router = APIRouter()


class PaymentIn(BaseModel):
    booking_id: uuid.UUID | None = None
    visit_id: uuid.UUID | None = None
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    method: str = "cash"
    kind: str = "payment"
    transaction_ref: str | None = Field(default=None, max_length=200)
    comment: str = ""
    paid_at: datetime | None = None


class RefundIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str = Field(min_length=1, max_length=1000)


class CancelIn(BaseModel):
    reason: str = ""


def _owner_finance(ctx: AuthContext) -> None:
    if not (ctx.can("finance.read") and ctx.can("finance.write")):
        raise DomainError(403, "FORBIDDEN", "Only the owner can refund or cancel payments")


def _branch_check(ctx: AuthContext, p: Payment) -> Payment:
    if not ctx.branch_allowed(p.branch_id):
        raise DomainError(404, "NOT_FOUND", "Payment not found")
    return p


@router.get("")
def list_payments(
    date_from: date | None = None,
    date_to: date | None = None,
    booking_id: uuid.UUID | None = None,
    visit_id: uuid.UUID | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    # front desk may see the payments of one booking/visit; full ledger is finance.read
    if not (ctx.can("finance.read") or (ctx.can("finance.write") and (booking_id or visit_id))):
        raise DomainError(403, "FORBIDDEN", "Missing permission: finance.read")
    q = select(Payment).where(Payment.tenant_id == ctx.tenant_id)
    zone = tz(default_tz_name(db, ctx.tenant_id))
    if date_from:
        q = q.where(Payment.paid_at >= datetime.combine(date_from, time.min, zone))
    if date_to:
        q = q.where(Payment.paid_at < datetime.combine(date_to + timedelta(days=1), time.min, zone))
    if booking_id:
        q = q.where(Payment.booking_id == booking_id)
    if visit_id:
        q = q.where(Payment.visit_id == visit_id)
    if status:
        q = q.where(Payment.status == status)
    if kind:
        q = q.where(Payment.kind == kind)
    if ctx.branch_scope:
        q = q.where(or_(Payment.branch_id.is_(None), Payment.branch_id.in_(ctx.branch_scope)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Payment.paid_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [svc.payment_to_dict(db, p) for p in rows], "total": total}


@router.post("", status_code=201)
def create_payment(
    body: PaymentIn, ctx: AuthContext = Depends(require("finance.write")), db: Session = Depends(get_db)
):
    p = svc.confirm_payment(
        db,
        ctx.tenant_id,
        amount=body.amount,
        method=body.method,
        kind=body.kind,
        booking_id=body.booking_id,
        visit_id=body.visit_id,
        transaction_ref=body.transaction_ref,
        comment=body.comment,
        actor_id=ctx.user.id,
        paid_at=body.paid_at,
    )
    if not ctx.branch_allowed(p.branch_id):
        db.rollback()
        raise DomainError(403, "BRANCH_FORBIDDEN", "No access to this branch")
    db.commit()
    return svc.payment_to_dict(db, p)


@router.get("/{payment_id}")
def get_payment(
    payment_id: uuid.UUID, ctx: AuthContext = Depends(require("finance.write")), db: Session = Depends(get_db)
):
    return svc.payment_to_dict(db, _branch_check(ctx, get_or_404(db, Payment, payment_id, ctx.tenant_id)))


@router.post("/{payment_id}/refund", status_code=201)
def refund(
    payment_id: uuid.UUID, body: RefundIn, ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
):
    _owner_finance(ctx)
    _branch_check(ctx, get_or_404(db, Payment, payment_id, ctx.tenant_id))
    r = svc.refund_payment(
        db, ctx.tenant_id, payment_id, amount=body.amount, reason=body.reason, actor_id=ctx.user.id
    )
    db.commit()
    return svc.payment_to_dict(db, r)


@router.post("/{payment_id}/cancel")
def cancel(
    payment_id: uuid.UUID,
    body: CancelIn | None = None,
    ctx: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    _owner_finance(ctx)
    _branch_check(ctx, get_or_404(db, Payment, payment_id, ctx.tenant_id))
    p = svc.cancel_payment(
        db, ctx.tenant_id, payment_id, actor_id=ctx.user.id, reason=body.reason if body else ""
    )
    db.commit()
    return svc.payment_to_dict(db, p)
