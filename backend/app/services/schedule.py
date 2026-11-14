"""Staff schedule changes: exceptions and shift requests (ТЗ §4.4, §9.2, §9.3).

Approved changes are stored as ``schedule_exceptions`` so the availability engine stops offering the closed
slots immediately (AT-07). Existing bookings are never cancelled automatically: conflicts are listed and an
administrator task of kind ``schedule_conflict`` is opened for manual handling.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ACTIVE_BOOKING_STATUSES, Booking, ScheduleException, ShiftRequest, Staff
from app.services import availability
from app.services.audit import audit
from app.services.availability import EXCEPTION_KINDS, WHOLE_DAY_EXCEPTION_KINDS
from app.services.common import DomainError, emit_event, get_or_404, utcnow

log = logging.getLogger(__name__)


def validate_times(kind: str, start: time | None, end: time | None) -> tuple[time | None, time | None]:
    """Normalise the (start, end) pair an exception of ``kind`` needs; whole-day kinds carry no times."""
    if kind not in EXCEPTION_KINDS:
        raise DomainError(422, "BAD_KIND", f"Unknown schedule change kind: {kind}")
    if kind in WHOLE_DAY_EXCEPTION_KINDS:
        return None, None
    if kind == "late_start":
        if start is None:
            raise DomainError(422, "TIME_REQUIRED", "late_start requires a new start time")
        return start, None
    if kind == "early_leave":
        if end is None:
            raise DomainError(422, "TIME_REQUIRED", "early_leave requires a new end time")
        return None, end
    if start is None or end is None:
        raise DomainError(422, "TIME_REQUIRED", f"{kind} requires start and end time")
    if end != time(0) and end <= start:
        raise DomainError(422, "BAD_RANGE", "End time must be after start time")
    return start, end


def find_conflicts(
    db: Session,
    tenant_id: uuid.UUID,
    staff: Staff,
    day: date,
    extra: ScheduleException | None = None,
) -> list[Booking]:
    """Active bookings on local ``day`` that do not fit the (possibly hypothetical) resolved schedule."""
    zone = availability.branch_zone(db, staff.branch_id)
    exceptions = list(
        availability.load_exceptions(db, tenant_id, [staff.id], day, day).get((staff.id, day), [])
    )
    if extra is not None:
        exceptions.append(extra)
    shifts = availability.load_shifts(db, tenant_id, [staff.id])[staff.id]
    work = availability.resolve_day(day, zone, shifts, exceptions)
    day_window = availability.local_day_window(day, zone)
    bookings = db.scalars(
        select(Booking)
        .where(
            Booking.tenant_id == tenant_id,
            Booking.staff_id == staff.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_at >= day_window[0],
            Booking.start_at < day_window[1],
        )
        .order_by(Booking.start_at)
    ).all()
    return [
        b for b in bookings if not any(ws <= b.occupied_start and b.occupied_end <= we for ws, we in work)
    ]


def _open_conflict_task(
    db: Session, tenant_id: uuid.UUID, staff: Staff, day: date, conflicts: list[Booking]
) -> None:
    """ТЗ §9.3: hand conflicting bookings to a responsible admin (manual reschedule/cancel)."""
    if not conflicts:
        return
    try:
        from app.services.escalation import create_admin_task
    except ImportError:  # TODO: remove guard once app.services.escalation ships (ops agent)
        log.warning("escalation module unavailable; schedule conflict task not created")
        return
    numbers = ", ".join(f"BK-{b.number:06d}" for b in conflicts)
    create_admin_task(
        db,
        tenant_id,
        question=f"Изменение графика: {staff.name}, {day.isoformat()}. Требуют решения записи: {numbers}",
        kind="schedule_conflict",
        priority="high",
        branch_id=staff.branch_id,
        payload={
            "staff_id": str(staff.id),
            "date": day.isoformat(),
            "booking_ids": [str(b.id) for b in conflicts],
        },
    )


def add_exception(
    db: Session,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    *,
    day: date,
    kind: str,
    start_time: time | None,
    end_time: time | None,
    reason: str,
    actor_id: uuid.UUID | None,
) -> tuple[ScheduleException, list[Booking]]:
    """Apply a schedule exception and report the bookings it conflicts with (they stay untouched)."""
    staff = get_or_404(db, Staff, staff_id, tenant_id)
    start_time, end_time = validate_times(kind, start_time, end_time)
    exc = ScheduleException(
        tenant_id=tenant_id,
        staff_id=staff.id,
        date=day,
        kind=kind,
        start_time=start_time,
        end_time=end_time,
        reason=reason,
        approved_by=actor_id,
    )
    db.add(exc)
    db.flush()
    conflicts = find_conflicts(db, tenant_id, staff, day)
    payload = {
        "staff_id": str(staff.id),
        "date": day.isoformat(),
        "kind": kind,
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "conflict_booking_ids": [str(b.id) for b in conflicts],
    }
    emit_event(db, tenant_id, "staff.schedule_changed", "staff", staff.id, payload)
    audit(
        db,
        tenant_id,
        "user" if actor_id else "system",
        str(actor_id) if actor_id else None,
        "schedule.exception_add",
        "staff",
        str(staff.id),
        {**payload, "exception_id": str(exc.id)},
    )
    _open_conflict_task(db, tenant_id, staff, day, conflicts)
    return exc, conflicts


def remove_exception(
    db: Session, tenant_id: uuid.UUID, staff_id: uuid.UUID, exception_id: uuid.UUID, *, actor_id: uuid.UUID
) -> None:
    exc = get_or_404(db, ScheduleException, exception_id, tenant_id)
    if exc.staff_id != staff_id:
        raise DomainError(404, "NOT_FOUND", "ScheduleException not found")
    payload = {"staff_id": str(staff_id), "date": exc.date.isoformat(), "kind": exc.kind, "removed": True}
    db.delete(exc)
    emit_event(db, tenant_id, "staff.schedule_changed", "staff", staff_id, payload)
    audit(
        db,
        tenant_id,
        "user",
        str(actor_id),
        "schedule.exception_remove",
        "staff",
        str(staff_id),
        {**payload, "exception_id": str(exception_id)},
    )


def create_shift_request(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    staff_id: uuid.UUID,
    kind: str,
    day: date,
    new_start: time | None,
    new_end: time | None,
    reason: str,
    actor_type: str,
    actor_id: str | None,
) -> ShiftRequest:
    """ТЗ §9.2: store the request with current hours and the bookings the change would conflict with."""
    staff = get_or_404(db, Staff, staff_id, tenant_id)
    new_start, new_end = validate_times(kind, new_start, new_end)
    current = availability.working_intervals(db, tenant_id, staff, day, day)[day]
    hypothetical = ScheduleException(
        tenant_id=tenant_id, staff_id=staff.id, date=day, kind=kind, start_time=new_start, end_time=new_end
    )
    conflicts = find_conflicts(db, tenant_id, staff, day, hypothetical)
    req = ShiftRequest(
        tenant_id=tenant_id,
        staff_id=staff.id,
        kind=kind,
        date=day,
        old_start=current[0][0].time() if current else None,
        old_end=current[-1][1].time() if current else None,
        new_start=new_start,
        new_end=new_end,
        reason=reason,
        status="pending",
        conflicts=[str(b.id) for b in conflicts],
    )
    db.add(req)
    db.flush()
    payload = {
        "shift_request_id": str(req.id),
        "staff_id": str(staff.id),
        "staff_name": staff.name,
        "kind": kind,
        "date": day.isoformat(),
        "new_start": new_start.isoformat() if new_start else None,
        "new_end": new_end.isoformat() if new_end else None,
        "reason": reason,
        "conflict_booking_ids": req.conflicts,
    }
    emit_event(db, tenant_id, "shift_request.created", "shift_request", req.id, payload)
    audit(db, tenant_id, actor_type, actor_id, "shift_request.create", "shift_request", str(req.id), payload)
    return req


def decide_shift_request(
    db: Session,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    *,
    approve: bool,
    comment: str,
    actor_id: uuid.UUID,
) -> ShiftRequest:
    """Approve (→ schedule exception, conflicts recomputed, admin task) or reject a pending request."""
    db.flush()
    req = db.scalar(
        select(ShiftRequest)
        .where(ShiftRequest.tenant_id == tenant_id, ShiftRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if req is None:
        raise DomainError(404, "NOT_FOUND", "ShiftRequest not found")
    if req.status != "pending":
        raise DomainError(409, "ALREADY_DECIDED", f"Request is already {req.status}")
    req.status = "approved" if approve else "rejected"
    req.decided_by = actor_id
    req.decided_at = utcnow()
    req.decision_comment = comment[:500]
    if approve:
        _, conflicts = add_exception(
            db,
            tenant_id,
            req.staff_id,
            day=req.date,
            kind=req.kind,
            start_time=req.new_start,
            end_time=req.new_end,
            reason=req.reason or comment,
            actor_id=actor_id,
        )
        req.conflicts = [str(b.id) for b in conflicts]
    db.flush()
    payload = {
        "shift_request_id": str(req.id),
        "staff_id": str(req.staff_id),
        "status": req.status,
        "date": req.date.isoformat(),
        "kind": req.kind,
        "comment": req.decision_comment,
        "conflict_booking_ids": req.conflicts,
    }
    emit_event(db, tenant_id, "shift_request.decided", "shift_request", req.id, payload)
    audit(db, tenant_id, "user", str(actor_id), "shift_request.decide", "shift_request", str(req.id), payload)
    return req
