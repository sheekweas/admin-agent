"""Bookings (ТЗ §8, §18: POST/PATCH /v1/bookings). Staff-role users only see and touch their own bookings."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, PlainSerializer, model_validator
from sqlalchemy import case, false, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, require
from app.models import AuditLog, Booking, BookingItem, Branch, Contact, Payment, Service, Staff
from app.services import booking as booking_svc
from app.services import crm
from app.services.availability import parse_slot_id
from app.services.common import DomainError, mask_phone, tz

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


# Money is always serialized as a 2-decimal string (docs/API.md: "Money is a decimal string").
Money = Annotated[Decimal, PlainSerializer(_money, return_type=str)]


# ---------------------------------------------------------------- schemas
class ContactRef(BaseModel):
    id: uuid.UUID
    name: str
    phone_e164: str | None


class StaffRef(BaseModel):
    id: uuid.UUID
    name: str
    color: str


class BookingServiceOut(BaseModel):
    service_id: uuid.UUID
    title_ru: str
    title_kk: str
    price: Money
    duration: int


class BookingOut(BaseModel):
    id: uuid.UUID
    number: int
    branch_id: uuid.UUID
    contact: ContactRef
    staff: StaffRef
    services: list[BookingServiceOut]
    start_at: datetime
    end_at: datetime
    status: str
    channel: str
    source: str
    total_price: Money
    paid_amount: Money
    comment: str
    cancel_reason: str
    hold_expires_at: datetime | None
    rescheduled_from_id: uuid.UUID | None
    rescheduled_to_id: uuid.UUID | None
    created_at: datetime


class BookingList(BaseModel):
    items: list[BookingOut]
    total: int


class InlineContact(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str = Field(min_length=5, max_length=30)


class BookingCreate(BaseModel):
    contact_id: uuid.UUID | None = None
    contact: InlineContact | None = None
    service_id: uuid.UUID
    staff_id: uuid.UUID | None = None
    start_at: datetime | None = None
    slot_id: str | None = None
    branch_id: uuid.UUID | None = None
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> BookingCreate:
        if (self.contact_id is None) == (self.contact is None):
            raise ValueError("exactly one of contact_id or contact is required")
        if self.start_at is None and not self.slot_id:
            raise ValueError("start_at or slot_id is required")
        return self


class BookingPatch(BaseModel):
    action: Literal["reschedule", "cancel", "status"] | None = None
    start_at: datetime | None = None
    staff_id: uuid.UUID | None = None
    slot_id: str | None = None
    reason: str = Field(default="", max_length=500)
    status: Literal["checked_in", "completed", "no_show", "confirmed"] | None = None
    comment: str | None = Field(default=None, max_length=2000)


class HistoryEntry(BaseModel):
    id: uuid.UUID
    action: str
    actor_type: str
    actor_id: str | None
    payload: dict
    created_at: datetime


class HistoryList(BaseModel):
    items: list[HistoryEntry]
    total: int


# ---------------------------------------------------------------- shared helpers (also used by other routers)
def tenant_zone(db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID | None = None) -> ZoneInfo:
    """Time zone used to interpret plain dates: the given branch, else the tenant's first branch."""
    stmt = select(Branch.timezone).where(Branch.tenant_id == tenant_id)
    stmt = stmt.where(Branch.id == branch_id) if branch_id else stmt.order_by(Branch.created_at)
    return tz(db.scalar(stmt.limit(1)))


def parse_bound(value: str | None, zone: ZoneInfo, *, end: bool) -> datetime | None:
    """``YYYY-MM-DD`` → local midnight (``end`` → following midnight, i.e. inclusive day); ISO datetimes need
    an offset."""
    if not value:
        return None
    try:
        if len(value) == 10:
            day = date.fromisoformat(value) + timedelta(days=1 if end else 0)
            return datetime.combine(day, time(0), tzinfo=zone)
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(422, "BAD_DATE", f"Bad date: {value}") from exc
    if moment.tzinfo is None:
        raise DomainError(422, "BAD_DATE", "Datetime must include an offset")
    return moment


def ensure_staff_scope(ctx: AuthContext, staff_id: uuid.UUID | None) -> None:
    """A ``staff`` user may only act on their own calendar (ТЗ §2)."""
    if ctx.role == "staff" and (ctx.staff_id is None or staff_id != ctx.staff_id):
        raise DomainError(403, "FORBIDDEN", "Staff members can only access their own schedule")


def _visible(ctx: AuthContext, b: Booking) -> bool:
    if ctx.role == "staff" and b.staff_id != ctx.staff_id:
        return False
    return ctx.branch_allowed(b.branch_id)


def _paid_amounts(db: Session, booking_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    if not booking_ids:
        return {}
    signed = case(
        (Payment.kind == "refund", -Payment.amount),
        else_=Payment.amount,
    )
    rows = db.execute(
        select(Payment.booking_id, func.coalesce(func.sum(signed), 0))
        .where(
            Payment.booking_id.in_(booking_ids),
            or_(
                (Payment.kind.in_(("payment", "prepayment")) & Payment.status.in_(("confirmed", "refunded"))),
                ((Payment.kind == "refund") & (Payment.status == "confirmed")),
            ),
        )
        .group_by(Payment.booking_id)
    )
    return {bid: Decimal(total) for bid, total in rows}


def serialize_bookings(db: Session, ctx: AuthContext, bookings: list[Booking]) -> list[BookingOut]:
    """Batch-load related rows (contacts, staff, services, payments) for a list of bookings."""
    if not bookings:
        return []
    ids = [b.id for b in bookings]
    contacts = {
        c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_({b.contact_id for b in bookings})))
    }
    staff = {s.id: s for s in db.scalars(select(Staff).where(Staff.id.in_({b.staff_id for b in bookings})))}
    items: dict[uuid.UUID, list[BookingServiceOut]] = defaultdict(list)
    for item, service in db.execute(
        select(BookingItem, Service)
        .join(Service, Service.id == BookingItem.service_id)
        .where(BookingItem.booking_id.in_(ids))
    ):
        items[item.booking_id].append(
            BookingServiceOut(
                service_id=service.id,
                title_ru=service.title_ru,
                title_kk=service.title_kk,
                price=item.price_snapshot,
                duration=item.duration_snapshot,
            )
        )
    paid = _paid_amounts(db, ids)
    mask = ctx.role == "analyst"
    out = []
    for b in bookings:
        c, s = contacts[b.contact_id], staff[b.staff_id]
        out.append(
            BookingOut(
                id=b.id,
                number=b.number,
                branch_id=b.branch_id,
                contact=ContactRef(
                    id=c.id, name=c.name, phone_e164=mask_phone(c.phone_e164) if mask else c.phone_e164
                ),
                staff=StaffRef(id=s.id, name=s.name, color=s.color),
                services=items[b.id],
                start_at=b.start_at,
                end_at=b.end_at,
                status=b.status,
                channel=b.channel,
                source=b.source,
                total_price=b.total_price,
                paid_amount=paid.get(b.id, Decimal(0)),
                comment=b.comment,
                cancel_reason=b.cancel_reason,
                hold_expires_at=b.hold_expires_at,
                rescheduled_from_id=b.rescheduled_from_id,
                rescheduled_to_id=b.rescheduled_to_id,
                created_at=b.created_at,
            )
        )
    return out


def _get_visible(db: Session, ctx: AuthContext, booking_id: uuid.UUID) -> Booking:
    b = db.scalar(select(Booking).where(Booking.tenant_id == ctx.tenant_id, Booking.id == booking_id))
    if b is None or not _visible(ctx, b):
        raise DomainError(404, "NOT_FOUND", "Booking not found")
    return b


def _one(db: Session, ctx: AuthContext, b: Booking) -> BookingOut:
    db.refresh(b)
    return serialize_bookings(db, ctx, [b])[0]


# ---------------------------------------------------------------- routes
@router.get("", response_model=BookingList)
def list_bookings(
    ctx: Annotated[AuthContext, Depends(require("bookings.read"))],
    db: DbSession,
    date_from: str | None = None,
    date_to: str | None = None,
    staff_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(description="Comma-separated statuses")] = None,
    contact_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    zone = tenant_zone(db, ctx.tenant_id, branch_id)
    stmt = select(Booking).where(Booking.tenant_id == ctx.tenant_id)
    if ctx.role == "staff":
        stmt = stmt.where(Booking.staff_id == ctx.staff_id) if ctx.staff_id else stmt.where(false())
    elif staff_id is not None:
        stmt = stmt.where(Booking.staff_id == staff_id)
    if ctx.branch_scope:
        stmt = stmt.where(Booking.branch_id.in_([uuid.UUID(b) for b in ctx.branch_scope]))
    if branch_id is not None:
        stmt = stmt.where(Booking.branch_id == branch_id)
    if contact_id is not None:
        stmt = stmt.where(Booking.contact_id == contact_id)
    if status:
        stmt = stmt.where(Booking.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if (start := parse_bound(date_from, zone, end=False)) is not None:
        stmt = stmt.where(Booking.start_at >= start)
    if (end := parse_bound(date_to, zone, end=True)) is not None:
        stmt = stmt.where(Booking.start_at < end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Booking.start_at, Booking.number).limit(limit).offset(offset)))
    return BookingList(items=serialize_bookings(db, ctx, rows), total=total)


def scoped_alternatives(ctx: AuthContext, exc: DomainError) -> DomainError:
    """Only offer alternatives the caller is allowed to book (own staff for "staff", allowed branches)."""
    if getattr(exc, "code", None) != "SLOT_UNAVAILABLE" or not isinstance(exc.detail, dict):
        return exc
    exc.detail["alternatives"] = [
        a
        for a in exc.detail.get("alternatives", [])
        if ctx.branch_allowed(a.get("branch_id"))
        and (ctx.role != "staff" or str(a.get("staff_id")) == str(ctx.staff_id))
    ]
    return exc


@router.post("", response_model=BookingOut, status_code=201)
def create_booking(
    ctx: Annotated[AuthContext, Depends(require("bookings.write_own"))],
    db: DbSession,
    body: BookingCreate,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None,
):
    if ctx.role == "staff":
        if body.slot_id:
            ensure_staff_scope(ctx, parse_slot_id(body.slot_id)[0])
        else:
            body.staff_id = body.staff_id or ctx.staff_id
            ensure_staff_scope(ctx, body.staff_id)
    if body.branch_id is not None and not ctx.branch_allowed(body.branch_id):
        raise DomainError(403, "FORBIDDEN", "No access to this branch")
    actor = str(ctx.user.id)
    if body.contact_id is not None:
        contact_id = crm.resolve_contact(db, ctx.tenant_id, body.contact_id).id
    else:
        existing = crm.find_contact_by_phone(db, ctx.tenant_id, body.contact.phone)
        contact_id = (
            existing
            or crm.create_contact(
                db,
                ctx.tenant_id,
                name=body.contact.name,
                phone=body.contact.phone,
                source="manual",
                branch_id=body.branch_id,
                actor_id=actor,
            )
        ).id
    try:
        b = booking_svc.create_booking(
            db,
            ctx.tenant_id,
            contact_id=contact_id,
            service_id=body.service_id,
            start_at=body.start_at,
            staff_id=body.staff_id,
            slot_id=body.slot_id,
            branch_id=body.branch_id,
            idempotency_key=idempotency_key,
            channel="admin",
            source="admin",
            actor_type="user",
            actor_id=actor,
            comment=body.comment,
        )
    except DomainError as exc:
        raise scoped_alternatives(ctx, exc) from None
    if not ctx.branch_allowed(b.branch_id):
        raise DomainError(403, "FORBIDDEN", "No access to this branch")
    db.commit()
    return _one(db, ctx, b)


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("bookings.read"))], db: DbSession
):
    return _one(db, ctx, _get_visible(db, ctx, booking_id))


@router.patch("/{booking_id}", response_model=BookingOut)
def patch_booking(
    booking_id: uuid.UUID,
    body: BookingPatch,
    ctx: Annotated[AuthContext, Depends(require("bookings.write_own"))],
    db: DbSession,
):
    """Reschedule (returns the NEW booking), cancel, change status, or edit the comment."""
    b = _get_visible(db, ctx, booking_id)
    actor: dict[str, Any] = {"actor_type": "user", "actor_id": str(ctx.user.id)}
    if body.action in ("reschedule", "cancel"):
        ctx.require("bookings.write")  # staff members may only mark their own visits
    if body.action == "reschedule":
        if body.start_at is None and not body.slot_id:
            raise DomainError(422, "START_REQUIRED", "start_at or slot_id is required")
        try:
            b = booking_svc.reschedule_booking(
                db,
                ctx.tenant_id,
                b.id,
                new_start_at=body.start_at,
                new_staff_id=body.staff_id,
                new_slot_id=body.slot_id,
                **actor,
            )
        except DomainError as exc:
            raise scoped_alternatives(ctx, exc) from None
    elif body.action == "cancel":
        b = booking_svc.cancel_booking(db, ctx.tenant_id, b.id, reason=body.reason, **actor)
    elif body.action == "status":
        if body.status is None:
            raise DomainError(422, "STATUS_REQUIRED", "status is required")
        b = booking_svc.set_booking_status(db, ctx.tenant_id, b.id, body.status, **actor)
    if body.comment is not None:
        b = booking_svc.update_comment(db, ctx.tenant_id, b.id, body.comment, **actor)
    elif body.action is None:
        raise DomainError(422, "NOTHING_TO_DO", "Specify an action or a comment")
    db.commit()
    return _one(db, ctx, b)


@router.get("/{booking_id}/history", response_model=HistoryList)
def booking_history(
    booking_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("bookings.read"))], db: DbSession
):
    b = _get_visible(db, ctx, booking_id)
    rows = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == ctx.tenant_id, AuditLog.entity == "booking", AuditLog.entity_id == str(b.id)
        )
        .order_by(AuditLog.created_at)
    ).all()
    items = [
        HistoryEntry(
            id=r.id,
            action=r.action,
            actor_type=r.actor_type,
            actor_id=r.actor_id,
            payload=r.payload or {},
            created_at=r.created_at,
        )
        for r in rows
    ]
    return HistoryList(items=items, total=len(items))
