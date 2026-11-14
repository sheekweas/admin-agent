"""Availability engine (ТЗ §8.1, §9.1; AT-17)."""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta
from decimal import Decimal

import pytest

from app.models import Booking, BookingItem, Resource, ScheduleException, Service, Shift, StaffService
from app.services import availability
from app.services.booking import get_available_slots
from app.services.common import DomainError
from tests.booking_helpers import ALMATY, at, future_day, local_hhmm


def _slots(db, world, day, service=None, staff=None, **kw):
    return get_available_slots(
        db,
        world.tenant.id,
        service_id=(service or world.haircut).id,
        date_from=day,
        date_to=day,
        staff_id=(staff or world.barber1).id,
        limit=1000,
        **kw,
    )


def _times(slots):
    return [local_hhmm(s.start_at) for s in slots]


def _book(db, world, staff, start, minutes=60, status="confirmed", **kw):
    b = Booking(
        tenant_id=world.tenant.id,
        number=kw.pop("number", 1),
        branch_id=world.branch.id,
        contact_id=world.contact.id,
        staff_id=staff.id,
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        occupied_start=start,
        occupied_end=start + timedelta(minutes=minutes),
        status=status,
        total_price=Decimal(5000),
        **kw,
    )
    b.items.append(
        BookingItem(service_id=world.haircut.id, price_snapshot=Decimal(5000), duration_snapshot=60)
    )
    db.add(b)
    db.commit()
    return b


# ---------------------------------------------------------------- pure schedule resolution
def _shift(**kw):
    base = {
        "weekday": 0,
        "start_time": time(10),
        "end_time": time(21),
        "breaks": [{"start": "14:00", "end": "15:00"}],
    }
    return Shift(**{**base, **kw})


def _exc(kind, start=None, end=None):
    return ScheduleException(kind=kind, start_time=start, end_time=end)


MONDAY = date(2026, 11, 16)


def _hours(intervals):
    return [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in intervals]


def test_resolve_day_shift_minus_breaks():
    assert _hours(availability.resolve_day(MONDAY, ALMATY, [_shift()], [])) == [
        ("10:00", "14:00"),
        ("15:00", "21:00"),
    ]
    assert availability.resolve_day(MONDAY + timedelta(days=1), ALMATY, [_shift()], []) == []


def test_resolve_day_respects_validity_window():
    shift = _shift(valid_from=MONDAY + timedelta(days=1))
    assert availability.resolve_day(MONDAY, ALMATY, [shift], []) == []


def test_resolve_day_exceptions():
    shifts = [_shift()]
    for kind in ("day_off", "vacation", "sick"):
        assert availability.resolve_day(MONDAY, ALMATY, shifts, [_exc(kind)]) == []
    assert _hours(availability.resolve_day(MONDAY, ALMATY, shifts, [_exc("late_start", time(12))])) == [
        ("12:00", "14:00"),
        ("15:00", "21:00"),
    ]
    assert _hours(
        availability.resolve_day(MONDAY, ALMATY, shifts, [_exc("early_leave", None, time(17))])
    ) == [
        ("10:00", "14:00"),
        ("15:00", "17:00"),
    ]
    assert _hours(
        availability.resolve_day(MONDAY, ALMATY, shifts, [_exc("overtime", time(21), time(23))])
    ) == [
        ("10:00", "14:00"),
        ("15:00", "23:00"),
    ]
    assert _hours(availability.resolve_day(MONDAY, ALMATY, shifts, [_exc("block", time(16), time(18))])) == [
        ("10:00", "14:00"),
        ("15:00", "16:00"),
        ("18:00", "21:00"),
    ]
    # overtime on a day without a shift
    tuesday = MONDAY + timedelta(days=1)
    assert _hours(
        availability.resolve_day(tuesday, ALMATY, shifts, [_exc("overtime", time(9), time(12))])
    ) == [("09:00", "12:00")]


# ---------------------------------------------------------------- slot generation
def test_basic_slots_grid_breaks_and_utc(db, world):
    day = future_day()
    slots = _slots(db, world, day)
    times = _times(slots)
    assert times[0] == "10:00" and times[-1] == "20:00"
    assert "13:00" in times and "13:15" not in times and "14:30" not in times and "15:00" in times
    assert len(times) == 13 + 21
    first = slots[0]
    assert first.start_at.tzinfo is not None and first.start_at.utcoffset() == timedelta(0)
    assert first.start_at == at(day, 10)
    assert first.end_at - first.start_at == timedelta(minutes=60)
    staff_id, start, service_id = availability.parse_slot_id(first.slot_id)
    assert (staff_id, start, service_id) == (world.barber1.id, first.start_at, world.haircut.id)


def test_buffers_must_fit_inside_working_hours(db, world):
    """AT-17: duration + buffers respect break and shift closing."""
    world.haircut.buffer_before_min = 15
    world.haircut.buffer_after_min = 15
    db.commit()
    times = _times(_slots(db, world, future_day()))
    assert times[0] == "10:15"
    assert "12:45" in times and "13:00" not in times
    assert "15:15" in times and "15:00" not in times
    assert times[-1] == "19:45"


def test_existing_booking_blocks_occupied_interval(db, world):
    day = future_day()
    b = _book(db, world, world.barber1, at(day, 11))
    b.occupied_end = at(day, 12, 15)  # booking with a 15 min cleanup buffer
    db.commit()
    times = _times(_slots(db, world, day))
    assert "10:00" in times and "10:15" not in times  # 10:15-11:15 overlaps 11:00
    assert "12:00" not in times and "12:15" in times
    # other staff unaffected
    assert "11:00" in _times(_slots(db, world, day, staff=world.barber2))


def test_cancelled_booking_does_not_block(db, world):
    day = future_day()
    _book(db, world, world.barber1, at(day, 11), status="cancelled")
    assert "11:00" in _times(_slots(db, world, day))


def test_holds_block_until_expiry(db, world):
    day = future_day()
    now = at(day, 0) - timedelta(days=1)
    _book(db, world, world.barber1, at(day, 11), status="held", hold_expires_at=now + timedelta(minutes=3))
    assert "11:00" not in _times(_slots(db, world, day, now=now))
    assert "11:00" in _times(_slots(db, world, day, now=now + timedelta(minutes=4)))


def test_per_staff_duration_override(db, world):
    link = db.get(StaffService, (world.barber1.id, world.haircut.id))
    link.duration_min = 90
    db.commit()
    slots = _slots(db, world, future_day())
    assert _times(slots)[-1] == "19:30"
    assert slots[0].end_at - slots[0].start_at == timedelta(minutes=90)


def test_exceptions_applied_to_slots(db, world):
    day = future_day()
    db.add(
        ScheduleException(
            tenant_id=world.tenant.id,
            staff_id=world.barber1.id,
            date=day,
            kind="early_leave",
            end_time=time(17),
        )
    )
    db.commit()
    times = _times(_slots(db, world, day))
    assert times[-1] == "16:00"
    db.add(ScheduleException(tenant_id=world.tenant.id, staff_id=world.barber1.id, date=day, kind="day_off"))
    db.commit()
    assert _slots(db, world, day) == []


def test_min_lead_time_and_step_setting(db, world):
    day = future_day()
    now = at(day, 11, 5)
    times = _times(_slots(db, world, day, now=now))
    assert times[0] == "11:45"  # 11:05 + 30 min lead → next 15-min grid point
    world.tenant.settings = {"slot_step_min": 30}
    db.commit()
    times = _times(_slots(db, world, day))
    assert times[:3] == ["10:00", "10:30", "11:00"]


def test_booking_horizon(db, world):
    world.tenant.settings = {"booking_horizon_days": 2}
    db.commit()
    assert _slots(db, world, future_day(3)) == []
    assert _slots(db, world, future_day(2)) != []


def test_inactive_staff_or_unlinked_service_has_no_slots(db, world):
    db.delete(db.get(StaffService, (world.barber1.id, world.beard.id)))
    db.commit()
    assert _slots(db, world, future_day(), service=world.beard) == []
    world.barber2.active = False
    db.commit()
    assert _slots(db, world, future_day(), staff=world.barber2) == []


def test_limited_resource(db, world):
    """A service that needs a chair is limited by free chairs of the branch, not only by staff."""
    day = future_day()
    color = Service(
        tenant_id=world.tenant.id,
        title_ru="Окрашивание",
        duration_min=60,
        price=Decimal(9000),
        resource_type="wash_chair",
    )
    db.add(color)
    db.flush()
    for st in (world.barber1, world.barber2):
        db.add(StaffService(staff_id=st.id, service_id=color.id))
    db.commit()
    # no resource of that type in the branch → nothing bookable
    assert _slots(db, world, day, service=color) == []

    chair = Resource(tenant_id=world.tenant.id, branch_id=world.branch.id, type="wash_chair", name="Мойка 1")
    db.add(chair)
    db.commit()
    slots = _slots(db, world, day, service=color, staff=world.barber2)
    assert slots[0].resource_id == chair.id
    # barber1 uses the only chair at 10:00 → barber2 cannot start at 10:00
    _book(db, world, world.barber1, at(day, 10), resource_id=chair.id)
    times = _times(_slots(db, world, day, service=color, staff=world.barber2))
    assert "10:00" not in times and "11:00" in times


def test_slot_id_roundtrip_and_validation():
    staff_id, service_id = uuid.uuid4(), uuid.uuid4()
    sid = availability.make_slot_id(staff_id, at(MONDAY, 10), service_id)
    assert availability.parse_slot_id(sid) == (staff_id, at(MONDAY, 10), service_id)
    with pytest.raises(DomainError):
        availability.parse_slot_id("garbage")
