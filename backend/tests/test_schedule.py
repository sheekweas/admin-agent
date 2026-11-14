"""Shift requests and schedule exceptions (ТЗ §4.4, §9.2-9.3; AT-07, AT-17)."""

from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy import select

from app.models import AdminTask, AuditLog, Booking, OutboxEvent, ScheduleException
from app.services import booking as booking_svc
from app.services import schedule
from app.services.common import DomainError
from tests.booking_helpers import at, future_day, local_hhmm


def _book(db, world, start, staff=None, service=None):
    b = booking_svc.create_booking(
        db,
        world.tenant.id,
        contact_id=world.contact.id,
        service_id=(service or world.haircut).id,
        start_at=start,
        staff_id=(staff or world.barber1).id,
        idempotency_key=None,
        channel="admin",
        source="admin",
        actor_type="user",
        actor_id=str(world.admin.id),
    )
    db.commit()
    return b


def _times(db, world, day, staff=None):
    slots = booking_svc.get_available_slots(
        db,
        world.tenant.id,
        service_id=world.haircut.id,
        date_from=day,
        date_to=day,
        staff_id=(staff or world.barber1).id,
        limit=500,
    )
    return [local_hhmm(s.start_at) for s in slots]


def _event_types(db):
    return [e.event_type for e in db.scalars(select(OutboxEvent))]


def test_at07_early_leave_closes_slots_and_lists_conflicts(db, world):
    """AT-07: barber leaves at 17:00, a client is booked at 18:00 → slots closed, conflict for manual decision."""
    day = future_day()
    late = _book(db, world, at(day, 18))
    fine = _book(db, world, at(day, 11))

    req = schedule.create_shift_request(
        db,
        world.tenant.id,
        staff_id=world.barber1.id,
        kind="early_leave",
        day=day,
        new_start=None,
        new_end=time(17),
        reason="к врачу",
        actor_type="user",
        actor_id=str(world.staff_user.id),
    )
    db.commit()
    assert req.status == "pending"
    assert (req.old_start, req.old_end) == (time(10), time(21))
    assert req.conflicts == [str(late.id)]
    assert "shift_request.created" in _event_types(db)
    assert "19:00" in _times(db, world, day)  # nothing changes before approval

    req = schedule.decide_shift_request(
        db, world.tenant.id, req.id, approve=True, comment="ок", actor_id=world.admin.id
    )
    db.commit()
    assert req.status == "approved" and req.decided_by == world.admin.id
    exc = db.scalar(select(ScheduleException).where(ScheduleException.staff_id == world.barber1.id))
    assert (exc.kind, exc.end_time, exc.approved_by) == ("early_leave", time(17), world.admin.id)
    times = _times(db, world, day)
    assert times[-1] == "16:00" and "19:00" not in times  # AI immediately stops offering closed slots
    assert "19:00" in _times(db, world, day, staff=world.barber2)

    # existing bookings are NOT cancelled automatically (ТЗ §9.3)
    db.refresh(late)
    db.refresh(fine)
    assert late.status == "confirmed" and fine.status == "confirmed"
    assert req.conflicts == [str(late.id)]
    types = _event_types(db)
    assert "staff.schedule_changed" in types and "shift_request.decided" in types
    task = db.scalar(select(AdminTask).where(AdminTask.kind == "schedule_conflict"))
    assert task is not None and str(late.id) in task.payload["booking_ids"]
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert {"shift_request.create", "shift_request.decide", "schedule.exception_add"} <= actions


def test_reject_and_double_decision(db, world):
    day = future_day()
    req = schedule.create_shift_request(
        db,
        world.tenant.id,
        staff_id=world.barber1.id,
        kind="day_off",
        day=day,
        new_start=None,
        new_end=None,
        reason="",
        actor_type="user",
        actor_id=None,
    )
    schedule.decide_shift_request(
        db, world.tenant.id, req.id, approve=False, comment="нет замены", actor_id=world.admin.id
    )
    db.commit()
    assert req.status == "rejected" and req.decision_comment == "нет замены"
    assert db.query(ScheduleException).count() == 0
    assert _times(db, world, day)  # still working
    with pytest.raises(DomainError) as e:
        schedule.decide_shift_request(db, world.tenant.id, req.id, approve=True, comment="", actor_id=None)
    assert e.value.code == "ALREADY_DECIDED"


@pytest.mark.parametrize(
    ("kind", "start", "end", "code"),
    [
        ("late_start", None, None, "TIME_REQUIRED"),
        ("early_leave", time(9), None, "TIME_REQUIRED"),
        ("overtime", time(22), time(21), "BAD_RANGE"),
        ("teleport", None, None, "BAD_KIND"),
    ],
)
def test_validation(db, world, kind, start, end, code):
    with pytest.raises(DomainError) as e:
        schedule.create_shift_request(
            db,
            world.tenant.id,
            staff_id=world.barber1.id,
            kind=kind,
            day=future_day(),
            new_start=start,
            new_end=end,
            reason="",
            actor_type="user",
            actor_id=None,
        )
    assert e.value.code == code


def test_block_exception_reports_conflicts(db, world):
    day = future_day()
    b = _book(db, world, at(day, 16, 30))
    exc, conflicts = schedule.add_exception(
        db,
        world.tenant.id,
        world.barber1.id,
        day=day,
        kind="block",
        start_time=time(16),
        end_time=time(18),
        reason="обучение",
        actor_id=world.admin.id,
    )
    db.commit()
    assert [c.id for c in conflicts] == [b.id]
    assert exc.start_time == time(16)
    times = _times(db, world, day)
    assert "16:00" not in times and "18:00" in times and "15:00" in times
    schedule.remove_exception(db, world.tenant.id, world.barber1.id, exc.id, actor_id=world.admin.id)
    db.commit()
    assert db.query(ScheduleException).count() == 0


def test_at17_shift_boundary_with_buffers(db, world):
    """AT-17: duration + cleanup buffer must end before the shift closes; buffers also block neighbours."""
    world.haircut.buffer_after_min = 15
    db.commit()
    day = future_day()
    with pytest.raises(DomainError) as e:
        _book(db, world, at(day, 20))  # 20:00-21:00 + 15 min cleanup > 21:00 closing
    assert e.value.code == "SLOT_UNAVAILABLE"
    last = _book(db, world, at(day, 19, 45))
    assert last.end_at == at(day, 20, 45) and last.occupied_end == at(day, 21)

    # a booking right before the lunch break must leave room for its buffer
    with pytest.raises(DomainError):
        _book(db, world, at(day, 13))
    before_break = _book(db, world, at(day, 12, 45))
    # the next client cannot start inside the previous client's cleanup buffer
    with pytest.raises(DomainError):
        _book(db, world, at(day, 11, 45))  # would end 12:45 + 15 min overlapping 12:45 booking
    _book(db, world, at(day, 11, 30))
    assert before_break.status == "confirmed"

    # late start moves the first possible slot
    db.add(
        ScheduleException(
            tenant_id=world.tenant.id,
            staff_id=world.barber2.id,
            date=day,
            kind="late_start",
            start_time=time(12),
        )
    )
    db.commit()
    with pytest.raises(DomainError):
        _book(db, world, at(day, 11, 45), staff=world.barber2)
    assert _book(db, world, at(day, 12), staff=world.barber2).status == "confirmed"
    assert db.query(Booking).filter(Booking.status == "confirmed").count() == 4
