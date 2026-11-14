"""Staff, weekly shifts, schedule exceptions and shift requests (ТЗ §9).

A ``staff``-role user reads only their own schedule and files shift requests only for themselves.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.bookings import BookingOut, ensure_staff_scope, serialize_bookings
from app.db import get_db
from app.deps import AuthContext, require
from app.models import (
    Booking,
    Branch,
    Membership,
    ScheduleException,
    Service,
    Shift,
    ShiftRequest,
    Staff,
    StaffService,
)
from app.services import availability, schedule
from app.services.audit import audit
from app.services.common import DomainError, emit_event, get_or_404

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["10:00"])]
ExceptionKind = Literal["day_off", "vacation", "sick", "early_leave", "late_start", "overtime", "block"]


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t is not None else None


def _time(value: str | None) -> time | None:
    return availability.parse_hhmm(value) if value else None


# ---------------------------------------------------------------- schemas
class StaffOut(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    user_id: uuid.UUID | None
    name: str
    specialization: str
    color: str
    active: bool
    service_ids: list[uuid.UUID]


class StaffIn(BaseModel):
    branch_id: uuid.UUID
    user_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    specialization: str = Field(default="", max_length=200)
    color: str = Field(default="#4f46e5", pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    active: bool = True
    service_ids: list[uuid.UUID] = []


class StaffPatch(BaseModel):
    branch_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    specialization: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
    active: bool | None = None
    service_ids: list[uuid.UUID] | None = None


class StaffList(BaseModel):
    items: list[StaffOut]
    total: int


class BreakIO(BaseModel):
    start: HHMM
    end: HHMM


class ShiftIO(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start_time: HHMM
    end_time: HHMM
    breaks: list[BreakIO] = []

    @model_validator(mode="after")
    def _order(self) -> ShiftIO:
        if self.end_time != "00:00" and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        for br in self.breaks:
            if (
                br.end <= br.start
                or br.start < self.start_time
                or (self.end_time != "00:00" and br.end > self.end_time)
            ):
                raise ValueError("breaks must lie inside the shift")
        return self


class ExceptionIn(BaseModel):
    date: date
    kind: ExceptionKind
    start_time: HHMM | None = None
    end_time: HHMM | None = None
    reason: str = Field(default="", max_length=500)


class ExceptionOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    date: date
    kind: str
    start_time: str | None
    end_time: str | None
    reason: str
    approved_by: uuid.UUID | None


class ExceptionCreated(ExceptionOut):
    conflicts: list[BookingOut]


class ExceptionList(BaseModel):
    items: list[ExceptionOut]
    total: int


class ShiftRequestIn(BaseModel):
    staff_id: uuid.UUID | None = None
    kind: ExceptionKind
    date: date
    new_start: HHMM | None = None
    new_end: HHMM | None = None
    reason: str = Field(default="", max_length=500)


class DecisionIn(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=500)


class ShiftRequestOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    staff_name: str
    kind: str
    date: date
    old_start: str | None
    old_end: str | None
    new_start: str | None
    new_end: str | None
    reason: str
    status: str
    conflicts: list[BookingOut]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_comment: str
    created_at: datetime


class ShiftRequestList(BaseModel):
    items: list[ShiftRequestOut]
    total: int


class IntervalOut(BaseModel):
    start: datetime
    end: datetime


class DayScheduleOut(BaseModel):
    date: date
    intervals: list[IntervalOut]
    exceptions: list[ExceptionOut]


class ScheduleOut(BaseModel):
    staff_id: uuid.UUID
    timezone: str
    days: list[DayScheduleOut]


# ---------------------------------------------------------------- helpers
def _service_map(db: Session, staff_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if staff_ids:
        for staff_id, service_id in db.execute(
            select(StaffService.staff_id, StaffService.service_id).where(StaffService.staff_id.in_(staff_ids))
        ):
            out[staff_id].append(service_id)
    return out


def _staff_out(db: Session, s: Staff) -> StaffOut:
    return StaffOut(
        id=s.id,
        branch_id=s.branch_id,
        user_id=s.user_id,
        name=s.name,
        specialization=s.specialization,
        color=s.color,
        active=s.active,
        service_ids=_service_map(db, [s.id])[s.id],
    )


def _visible_staff(db: Session, ctx: AuthContext, staff_id: uuid.UUID, *, own_only: bool) -> Staff:
    s = get_or_404(db, Staff, staff_id, ctx.tenant_id)
    if not ctx.branch_allowed(s.branch_id):
        raise DomainError(404, "NOT_FOUND", "Staff not found")
    if own_only:
        ensure_staff_scope(ctx, s.id)
    return s


def _check_refs(
    db: Session, ctx: AuthContext, branch_id: uuid.UUID | None, user_id: uuid.UUID | None
) -> None:
    if branch_id is not None:
        get_or_404(db, Branch, branch_id, ctx.tenant_id)
        if not ctx.branch_allowed(branch_id):
            raise DomainError(403, "FORBIDDEN", "No access to this branch")
    if user_id is not None and not db.scalar(
        select(Membership.id).where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
    ):
        raise DomainError(422, "UNKNOWN_USER", "User is not a member of this organization")


def _set_services(db: Session, tenant_id: uuid.UUID, staff: Staff, service_ids: list[uuid.UUID]) -> None:
    wanted = set(service_ids)
    if (
        wanted
        and set(db.scalars(select(Service.id).where(Service.tenant_id == tenant_id, Service.id.in_(wanted))))
        != wanted
    ):
        raise DomainError(422, "UNKNOWN_SERVICE", "Some service ids do not exist")
    current = set(db.scalars(select(StaffService.service_id).where(StaffService.staff_id == staff.id)))
    if current - wanted:
        db.execute(
            delete(StaffService).where(
                StaffService.staff_id == staff.id, StaffService.service_id.in_(current - wanted)
            )
        )
    for service_id in wanted - current:
        db.add(StaffService(staff_id=staff.id, service_id=service_id))


def _exception_out(e: ScheduleException) -> ExceptionOut:
    return ExceptionOut(
        id=e.id,
        staff_id=e.staff_id,
        date=e.date,
        kind=e.kind,
        start_time=_hhmm(e.start_time),
        end_time=_hhmm(e.end_time),
        reason=e.reason,
        approved_by=e.approved_by,
    )


def _shift_requests_out(db: Session, ctx: AuthContext, rows: list[ShiftRequest]) -> list[ShiftRequestOut]:
    booking_ids = {uuid.UUID(b) for r in rows for b in (r.conflicts or [])}
    bookings = (
        {
            b.id: b
            for b in db.scalars(
                select(Booking).where(Booking.tenant_id == ctx.tenant_id, Booking.id.in_(booking_ids))
            )
        }
        if booking_ids
        else {}
    )
    serialized = {b.id: b for b in serialize_bookings(db, ctx, list(bookings.values()))}
    names = dict(
        db.execute(select(Staff.id, Staff.name).where(Staff.id.in_({r.staff_id for r in rows}))).all()
    )
    return [
        ShiftRequestOut(
            id=r.id,
            staff_id=r.staff_id,
            staff_name=names.get(r.staff_id, ""),
            kind=r.kind,
            date=r.date,
            old_start=_hhmm(r.old_start),
            old_end=_hhmm(r.old_end),
            new_start=_hhmm(r.new_start),
            new_end=_hhmm(r.new_end),
            reason=r.reason,
            status=r.status,
            conflicts=[serialized[uuid.UUID(b)] for b in (r.conflicts or []) if uuid.UUID(b) in serialized],
            decided_by=r.decided_by,
            decided_at=r.decided_at,
            decision_comment=r.decision_comment,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------- shift requests (before /{staff_id})
@router.get("/shift-requests", response_model=ShiftRequestList)
def list_shift_requests(
    ctx: Annotated[AuthContext, Depends(require("staff.read"))],
    db: DbSession,
    status: Literal["pending", "approved", "rejected"] | None = None,
    staff_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = select(ShiftRequest).where(ShiftRequest.tenant_id == ctx.tenant_id)
    if ctx.role == "staff":
        staff_id = ctx.staff_id
        ensure_staff_scope(ctx, staff_id)
    if staff_id is not None:
        stmt = stmt.where(ShiftRequest.staff_id == staff_id)
    if status is not None:
        stmt = stmt.where(ShiftRequest.status == status)
    if ctx.branch_scope:
        stmt = stmt.where(
            ShiftRequest.staff_id.in_(
                select(Staff.id).where(Staff.branch_id.in_([uuid.UUID(b) for b in ctx.branch_scope]))
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(ShiftRequest.created_at.desc()).limit(limit).offset(offset)))
    return ShiftRequestList(items=_shift_requests_out(db, ctx, rows), total=total)


@router.post("/shift-requests", response_model=ShiftRequestOut, status_code=201)
def create_shift_request(
    body: ShiftRequestIn,
    ctx: Annotated[AuthContext, Depends(require("shift_requests.create"))],
    db: DbSession,
):
    staff_id = body.staff_id or ctx.staff_id
    if staff_id is None:
        raise DomainError(422, "STAFF_REQUIRED", "staff_id is required")
    staff = _visible_staff(db, ctx, staff_id, own_only=True)
    req = schedule.create_shift_request(
        db,
        ctx.tenant_id,
        staff_id=staff.id,
        kind=body.kind,
        day=body.date,
        new_start=_time(body.new_start),
        new_end=_time(body.new_end),
        reason=body.reason,
        actor_type="user",
        actor_id=str(ctx.user.id),
    )
    db.commit()
    db.refresh(req)
    return _shift_requests_out(db, ctx, [req])[0]


@router.post("/shift-requests/{request_id}/decision", response_model=ShiftRequestOut)
def decide_shift_request(
    request_id: uuid.UUID,
    body: DecisionIn,
    ctx: Annotated[AuthContext, Depends(require("shift_requests.decide"))],
    db: DbSession,
):
    req = get_or_404(db, ShiftRequest, request_id, ctx.tenant_id)
    _visible_staff(db, ctx, req.staff_id, own_only=False)
    req = schedule.decide_shift_request(
        db, ctx.tenant_id, req.id, approve=body.approve, comment=body.comment, actor_id=ctx.user.id
    )
    db.commit()
    db.refresh(req)
    return _shift_requests_out(db, ctx, [req])[0]


# ---------------------------------------------------------------- staff CRUD
@router.get("", response_model=StaffList)
def list_staff(
    ctx: Annotated[AuthContext, Depends(require("staff.read"))],
    db: DbSession,
    branch_id: uuid.UUID | None = None,
    active: bool | None = None,
    service_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = select(Staff).where(Staff.tenant_id == ctx.tenant_id)
    if branch_id is not None:
        stmt = stmt.where(Staff.branch_id == branch_id)
    if ctx.branch_scope:
        stmt = stmt.where(Staff.branch_id.in_([uuid.UUID(b) for b in ctx.branch_scope]))
    if active is not None:
        stmt = stmt.where(Staff.active.is_(active))
    if service_id is not None:
        stmt = stmt.where(
            Staff.id.in_(select(StaffService.staff_id).where(StaffService.service_id == service_id))
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Staff.name).limit(limit).offset(offset)))
    services = _service_map(db, [s.id for s in rows])
    items = [
        StaffOut(
            id=s.id,
            branch_id=s.branch_id,
            user_id=s.user_id,
            name=s.name,
            specialization=s.specialization,
            color=s.color,
            active=s.active,
            service_ids=services[s.id],
        )
        for s in rows
    ]
    return StaffList(items=items, total=total)


@router.post("", response_model=StaffOut, status_code=201)
def create_staff(body: StaffIn, ctx: Annotated[AuthContext, Depends(require("staff.write"))], db: DbSession):
    _check_refs(db, ctx, body.branch_id, body.user_id)
    s = Staff(tenant_id=ctx.tenant_id, **body.model_dump(exclude={"service_ids"}))
    db.add(s)
    db.flush()
    _set_services(db, ctx.tenant_id, s, body.service_ids)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "staff.create",
        "staff",
        str(s.id),
        body.model_dump(mode="json"),
        ip=ctx.ip,
    )
    db.commit()
    return _staff_out(db, s)


@router.get("/{staff_id}", response_model=StaffOut)
def get_staff(
    staff_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("staff.read"))], db: DbSession
):
    return _staff_out(db, _visible_staff(db, ctx, staff_id, own_only=False))


@router.patch("/{staff_id}", response_model=StaffOut)
def patch_staff(
    staff_id: uuid.UUID,
    body: StaffPatch,
    ctx: Annotated[AuthContext, Depends(require("staff.write"))],
    db: DbSession,
):
    s = _visible_staff(db, ctx, staff_id, own_only=False)
    changes = body.model_dump(exclude_unset=True)
    service_ids = changes.pop("service_ids", None)
    _check_refs(db, ctx, changes.get("branch_id"), changes.get("user_id"))
    for k, v in changes.items():
        if v is not None or k == "user_id":
            setattr(s, k, v)
    if service_ids is not None:
        _set_services(db, ctx.tenant_id, s, service_ids)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "staff.update",
        "staff",
        str(s.id),
        body.model_dump(mode="json", exclude_unset=True),
        ip=ctx.ip,
    )
    db.commit()
    return _staff_out(db, s)


# ---------------------------------------------------------------- weekly shifts
@router.get("/{staff_id}/shifts", response_model=list[ShiftIO])
def get_shifts(
    staff_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("staff.read"))], db: DbSession
):
    s = _visible_staff(db, ctx, staff_id, own_only=True)
    rows = db.scalars(
        select(Shift)
        .where(Shift.tenant_id == ctx.tenant_id, Shift.staff_id == s.id)
        .order_by(Shift.weekday, Shift.start_time)
    )
    return [
        ShiftIO(
            weekday=r.weekday,
            start_time=_hhmm(r.start_time),
            end_time=_hhmm(r.end_time),
            breaks=[BreakIO(**b) for b in (r.breaks or [])],
        )
        for r in rows
    ]


@router.put("/{staff_id}/shifts", response_model=list[ShiftIO])
def put_shifts(
    staff_id: uuid.UUID,
    body: list[ShiftIO],
    ctx: Annotated[AuthContext, Depends(require("staff.write"))],
    db: DbSession,
):
    """Replace the weekly template. Existing bookings are kept (ТЗ §8.4)."""
    s = _visible_staff(db, ctx, staff_id, own_only=False)
    db.execute(delete(Shift).where(Shift.tenant_id == ctx.tenant_id, Shift.staff_id == s.id))
    for item in body:
        db.add(
            Shift(
                tenant_id=ctx.tenant_id,
                staff_id=s.id,
                weekday=item.weekday,
                start_time=_time(item.start_time),
                end_time=_time(item.end_time),
                breaks=[b.model_dump() for b in item.breaks],
            )
        )
    payload = {"staff_id": str(s.id), "weekly_shifts": [i.model_dump() for i in body]}
    emit_event(db, ctx.tenant_id, "staff.schedule_changed", "staff", s.id, payload)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "schedule.shifts_replace",
        "staff",
        str(s.id),
        payload,
        ip=ctx.ip,
    )
    db.commit()
    return get_shifts(staff_id, ctx, db)


# ---------------------------------------------------------------- exceptions
@router.get("/{staff_id}/exceptions", response_model=ExceptionList)
def list_exceptions(
    ctx: Annotated[AuthContext, Depends(require("staff.read"))],
    db: DbSession,
    staff_id: uuid.UUID,
    date_from: date | None = None,
    date_to: date | None = None,
):
    s = _visible_staff(db, ctx, staff_id, own_only=True)
    stmt = select(ScheduleException).where(
        ScheduleException.tenant_id == ctx.tenant_id, ScheduleException.staff_id == s.id
    )
    if date_from:
        stmt = stmt.where(ScheduleException.date >= date_from)
    if date_to:
        stmt = stmt.where(ScheduleException.date <= date_to)
    rows = db.scalars(stmt.order_by(ScheduleException.date)).all()
    return ExceptionList(items=[_exception_out(e) for e in rows], total=len(rows))


@router.post("/{staff_id}/exceptions", response_model=ExceptionCreated, status_code=201)
def create_exception(
    staff_id: uuid.UUID,
    body: ExceptionIn,
    ctx: Annotated[AuthContext, Depends(require("staff.write"))],
    db: DbSession,
):
    s = _visible_staff(db, ctx, staff_id, own_only=False)
    exc, conflicts = schedule.add_exception(
        db,
        ctx.tenant_id,
        s.id,
        day=body.date,
        kind=body.kind,
        start_time=_time(body.start_time),
        end_time=_time(body.end_time),
        reason=body.reason,
        actor_id=ctx.user.id,
    )
    db.commit()
    return ExceptionCreated(
        **_exception_out(exc).model_dump(), conflicts=serialize_bookings(db, ctx, conflicts)
    )


@router.delete("/{staff_id}/exceptions/{exception_id}", status_code=204)
def delete_exception(
    staff_id: uuid.UUID,
    exception_id: uuid.UUID,
    ctx: Annotated[AuthContext, Depends(require("staff.write"))],
    db: DbSession,
):
    s = _visible_staff(db, ctx, staff_id, own_only=False)
    schedule.remove_exception(db, ctx.tenant_id, s.id, exception_id, actor_id=ctx.user.id)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- resolved schedule
@router.get("/{staff_id}/schedule", response_model=ScheduleOut)
def get_schedule(
    ctx: Annotated[AuthContext, Depends(require("staff.read"))],
    db: DbSession,
    staff_id: uuid.UUID,
    date_from: date,
    date_to: date | None = None,
):
    """Resolved working intervals per local day (shifts − breaks ± exceptions)."""
    s = _visible_staff(db, ctx, staff_id, own_only=True)
    date_to = date_to or date_from
    if date_to < date_from or date_to - date_from > timedelta(days=62):
        raise DomainError(422, "BAD_RANGE", "date_to must be within 62 days after date_from")
    zone = availability.branch_zone(db, s.branch_id)
    days = availability.working_intervals(db, ctx.tenant_id, s, date_from, date_to)
    exceptions = availability.load_exceptions(db, ctx.tenant_id, [s.id], date_from, date_to)
    return ScheduleOut(
        staff_id=s.id,
        timezone=zone.key,
        days=[
            DayScheduleOut(
                date=d,
                intervals=[IntervalOut(start=a, end=b) for a, b in ivs],
                exceptions=[_exception_out(e) for e in exceptions.get((s.id, d), [])],
            )
            for d, ivs in days.items()
        ],
    )
