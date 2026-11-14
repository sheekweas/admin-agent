"""Booking engine (ТЗ §8): atomic create, AT-01 race, idempotency, holds, reschedule, cancel, statuses."""

from __future__ import annotations

import threading
from datetime import time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog, Booking, Contact, Lead, OutboxEvent, ScheduleException, Visit
from app.services import availability
from app.services import booking as svc
from app.services.common import DomainError
from tests.booking_helpers import at, future_day, local_hhmm


def _create(db, world, start, *, staff=None, actor="user", **kw):
    params = {
        "contact_id": world.contact.id,
        "service_id": world.haircut.id,
        "start_at": start,
        "staff_id": (staff or world.barber1).id,
        "idempotency_key": None,
        "channel": "admin" if actor == "user" else "whatsapp",
        "source": "admin" if actor == "user" else "ai_agent",
        "actor_type": actor,
        "actor_id": str(world.admin.id) if actor == "user" else None,
    }
    params.update(kw)
    b = svc.create_booking(db, world.tenant.id, **params)
    db.commit()
    return b


def _events(db, *types):
    return [
        e.event_type
        for e in db.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at))
        if not types or e.event_type in types
    ]


def test_create_booking_happy_path(db, world):
    day = future_day()
    b = _create(db, world, at(day, 12))
    assert b.status == "confirmed" and b.number == 1
    assert b.end_at - b.start_at == timedelta(minutes=60)
    assert b.total_price == Decimal(5000) and b.items[0].price_snapshot == Decimal(5000)
    assert b.created_by == world.admin.id
    assert _events(db, "booking.created", "booking.confirmed") == ["booking.created", "booking.confirmed"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "booking.create")).entity_id == str(b.id)
    lead = db.scalar(select(Lead).where(Lead.booking_id == b.id))
    assert lead.stage == "booked"
    slots = svc.get_available_slots(
        db,
        world.tenant.id,
        service_id=world.haircut.id,
        date_from=day,
        date_to=day,
        staff_id=world.barber1.id,
        limit=500,
    )
    assert "12:00" not in [local_hhmm(s.start_at) for s in slots]
    assert _create(db, world, at(day, 13)).number == 2


def test_create_with_slot_id_and_per_staff_price(db, world):
    from app.models import StaffService

    link = db.get(StaffService, (world.barber2.id, world.haircut.id))
    link.price = Decimal(7000)
    db.commit()
    day = future_day()
    slot = svc.get_available_slots(
        db,
        world.tenant.id,
        service_id=world.haircut.id,
        date_from=day,
        date_to=day,
        staff_id=world.barber2.id,
    )[0]
    b = _create(db, world, None, slot_id=slot.slot_id)
    assert (b.staff_id, b.start_at) == (world.barber2.id, slot.start_at)
    assert b.total_price == Decimal(7000)
    with pytest.raises(DomainError) as e:
        _create(db, world, None, service_id=world.beard.id, slot_id=slot.slot_id)
    assert e.value.code == "BAD_SLOT_ID"


def test_idempotency_key_returns_same_booking(db, world):
    start = at(future_day(), 12)
    a = _create(db, world, start, idempotency_key="wa-msg-1")
    b = _create(db, world, start, idempotency_key="wa-msg-1")
    assert a.id == b.id
    assert db.query(Booking).count() == 1
    assert _events(db, "booking.created") == ["booking.created"]


def test_conflict_returns_alternatives_same_staff_first(db, world):
    day = future_day()
    _create(db, world, at(day, 12))
    with pytest.raises(DomainError) as e:
        _create(db, world, at(day, 12), idempotency_key="other")
    assert e.value.status_code == 409 and e.value.code == "SLOT_UNAVAILABLE"
    alts = e.value.detail["alternatives"]
    assert alts, "alternatives must be offered (ТЗ §4.2)"
    assert alts[0]["staff_id"] == str(world.barber1.id)
    assert {"slot_id", "start_at", "end_at", "staff_name", "branch_id"} <= set(alts[0])
    assert any(
        a["staff_id"] == str(world.barber2.id) and a["start_at"] == at(day, 12).isoformat() for a in alts
    )


def test_any_staff_is_chosen_when_not_specified(db, world):
    start = at(future_day(), 12)
    first = _create(db, world, start, staff_id=None)
    second = _create(db, world, start, staff_id=None)
    assert {first.staff_id, second.staff_id} == {world.barber1.id, world.barber2.id}
    with pytest.raises(DomainError):
        _create(db, world, start, staff_id=None)


def test_outside_working_hours_rejected(db, world):
    day = future_day()
    for hh, mm in ((9, 0), (13, 30), (20, 15), (12, 5)):  # before shift, into break, past closing, off-grid
        with pytest.raises(DomainError) as e:
            _create(db, world, at(day, hh, mm))
        assert e.value.code == "SLOT_UNAVAILABLE"


def test_ai_respects_min_lead_time_admin_does_not(db, world):
    world.tenant.settings = {"min_lead_time_min": 60 * 24 * 5}
    db.commit()
    start = at(future_day(3), 12)
    with pytest.raises(DomainError) as e:
        _create(db, world, start, actor="ai")
    assert e.value.code == "SLOT_UNAVAILABLE"
    assert _create(db, world, start, actor="user").status == "confirmed"  # walk-in desk booking


def test_db_exclusion_constraint_is_final_guard(db, world, monkeypatch):
    """Even if validation were bypassed, PostgreSQL rejects the overlap → 409 (ТЗ §8.3)."""
    day = future_day()
    existing = _create(db, world, at(day, 12))

    def stale_validation(*args, **kwargs):  # pretend the slot is free (as a racing reader would)
        return availability.Slot(
            staff_id=world.barber1.id,
            staff_name="x",
            branch_id=world.branch.id,
            start_at=at(day, 12, 30),
            end_at=at(day, 13, 30),
            slot_id="x",
        )

    monkeypatch.setattr(availability, "find_slot", stale_validation)
    with pytest.raises(DomainError) as e:
        svc.create_booking(
            db,
            world.tenant.id,
            contact_id=world.contact.id,
            service_id=world.haircut.id,
            start_at=at(day, 12, 30),
            staff_id=world.barber1.id,
            idempotency_key="k2",
            channel="admin",
            source="admin",
            actor_type="user",
            actor_id=str(world.admin.id),
        )
    assert e.value.code == "SLOT_UNAVAILABLE"
    db.rollback()
    assert [b.id for b in db.query(Booking).all()] == [existing.id]


def _race(world, start, n):
    """n threads, each with its own DB session/transaction, try to book the same slot simultaneously."""
    barrier = threading.Barrier(n)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        s = SessionLocal()
        try:
            barrier.wait(timeout=10)
            b = svc.create_booking(
                s,
                world.tenant.id,
                contact_id=world.contact.id,
                service_id=world.haircut.id,
                start_at=start,
                staff_id=world.barber1.id,
                idempotency_key=f"race-{i}",
                channel="whatsapp",
                source="ai_agent",
                actor_type="ai",
                actor_id=None,
            )
            s.commit()
            out = ("ok", b.id)
        except DomainError as exc:
            s.rollback()
            out = ("err", exc)
        finally:
            s.close()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


@pytest.mark.parametrize("clients", [2, 6])
def test_at01_concurrent_last_slot(db, world, clients):
    """AT-01: clients race for barber1's LAST free slot → exactly one booking, the rest get 409 + alternatives."""
    day = future_day()
    db.add(
        ScheduleException(
            tenant_id=world.tenant.id,
            staff_id=world.barber1.id,
            date=day,
            kind="late_start",
            start_time=time(20),
        )
    )
    db.commit()
    free = svc.get_available_slots(
        db,
        world.tenant.id,
        service_id=world.haircut.id,
        date_from=day,
        date_to=day,
        staff_id=world.barber1.id,
    )
    assert [local_hhmm(s.start_at) for s in free] == ["20:00"]  # truly the last slot

    results = _race(world, at(day, 20), clients)
    assert len(results) == clients
    ok = [r for r in results if r[0] == "ok"]
    errors = [r[1] for r in results if r[0] == "err"]
    assert len(ok) == 1
    assert len(errors) == clients - 1
    for exc in errors:
        assert exc.status_code == 409 and exc.code == "SLOT_UNAVAILABLE"
        assert exc.detail["alternatives"], "the loser must be offered an alternative"
        assert all(
            a["staff_id"] != str(world.barber1.id) or a["start_at"] != at(day, 20).isoformat()
            for a in exc.detail["alternatives"]
        )
    db.expire_all()
    active = db.scalars(
        select(Booking).where(Booking.staff_id == world.barber1.id, Booking.status == "confirmed")
    ).all()
    assert len(active) == 1 and active[0].id == ok[0][1]


def test_hold_expiry_released_by_worker_and_lazily(db, world):
    day = future_day()
    held = _create(db, world, at(day, 12), status="held", actor="ai")
    assert held.status == "held" and held.hold_expires_at is not None
    assert db.scalar(select(Lead).where(Lead.booking_id == held.id)).stage == "booking_pending"
    # not expired yet → slot is taken
    with pytest.raises(DomainError):
        _create(db, world, at(day, 12))
    # worker releases it once TTL passed
    assert svc.release_expired_holds(db, held.hold_expires_at + timedelta(seconds=1)) == 1
    db.commit()
    db.refresh(held)
    assert held.status == "cancelled" and held.cancel_reason == "hold_expired"
    assert svc.release_expired_holds(db, held.hold_expires_at + timedelta(minutes=5)) == 0

    # lazily: an expired hold for the same slot is released inside create_booking
    held2 = _create(db, world, at(day, 15), status="held", actor="ai")
    held2.hold_expires_at = held2.hold_expires_at - timedelta(hours=1)
    db.commit()
    taken = _create(db, world, at(day, 15))
    db.refresh(held2)
    assert taken.status == "confirmed" and held2.status == "cancelled"


def test_confirm_held_booking(db, world):
    held = _create(db, world, at(future_day(), 12), status="held", actor="ai")
    b = svc.set_booking_status(db, world.tenant.id, held.id, "confirmed", actor_type="user", actor_id=None)
    db.commit()
    assert b.status == "confirmed" and b.hold_expires_at is None
    assert "booking.confirmed" in _events(db)
    assert db.scalar(select(Lead).where(Lead.booking_id == b.id)).stage == "booked"


def test_reschedule_atomic_with_links(db, world):
    day = future_day()
    old = _create(db, world, at(day, 12))
    lead = db.scalar(select(Lead).where(Lead.booking_id == old.id))
    # shift by 30 minutes overlaps the old interval of the same staff: must still work
    new = svc.reschedule_booking(
        db,
        world.tenant.id,
        old.id,
        new_start_at=at(day, 12, 30),
        actor_type="user",
        actor_id=str(world.admin.id),
    )
    db.commit()
    db.refresh(old)
    assert old.status == "rescheduled" and old.rescheduled_to_id == new.id
    assert new.status == "confirmed" and new.rescheduled_from_id == old.id and new.start_at == at(day, 12, 30)
    assert new.number != old.number
    db.refresh(lead)
    assert lead.booking_id == new.id
    assert "booking.rescheduled" in _events(db)
    # slot at 12:00 is free again for others; 12:30 is taken
    other = _create(db, world, at(day, 11), staff=world.barber1)
    assert other.status == "confirmed"


def test_reschedule_to_busy_slot_keeps_original(db, world):
    day = future_day()
    blocker = _create(db, world, at(day, 16), staff=world.barber2)
    old = _create(db, world, at(day, 12))
    with pytest.raises(DomainError) as e:
        svc.reschedule_booking(
            db,
            world.tenant.id,
            old.id,
            new_start_at=at(day, 16),
            new_staff_id=world.barber2.id,
            actor_type="user",
            actor_id=None,
        )
    assert e.value.code == "SLOT_UNAVAILABLE"
    db.rollback()
    db.refresh(old)
    assert old.status == "confirmed" and old.rescheduled_to_id is None
    assert blocker.status == "confirmed"


def test_reschedule_to_other_staff_via_slot_id(db, world):
    day = future_day()
    old = _create(db, world, at(day, 12))
    slot = svc.get_available_slots(
        db,
        world.tenant.id,
        service_id=world.haircut.id,
        date_from=day,
        date_to=day,
        staff_id=world.barber2.id,
    )[3]
    new = svc.reschedule_booking(
        db,
        world.tenant.id,
        old.id,
        new_slot_id=slot.slot_id,
        actor_type="ai",
        actor_id=None,
        contact_id=world.contact.id,
    )
    db.commit()
    assert (new.staff_id, new.start_at) == (world.barber2.id, slot.start_at)


def test_cancel_frees_slot_and_respects_deadline(db, world):
    day = future_day()
    b = _create(db, world, at(day, 12))
    world.tenant.settings = {"cancel_deadline_hours": 24 * 30}
    db.commit()
    with pytest.raises(DomainError) as e:
        svc.cancel_booking(
            db,
            world.tenant.id,
            b.id,
            reason="передумал",
            actor_type="ai",
            actor_id=None,
            contact_id=world.contact.id,
        )
    assert e.value.code == "DEADLINE_PASSED"
    # an administrator may override the deadline
    svc.cancel_booking(
        db, world.tenant.id, b.id, reason="передумал", actor_type="user", actor_id=str(world.admin.id)
    )
    db.commit()
    assert b.status == "cancelled" and b.cancel_reason == "передумал"
    assert "booking.cancelled" in _events(db)
    assert db.scalar(select(Lead).where(Lead.booking_id == b.id)).stage == "cancelled"
    assert _create(db, world, at(day, 12)).status == "confirmed"  # slot is free again (AT-09)
    with pytest.raises(DomainError) as e:
        svc.cancel_booking(db, world.tenant.id, b.id, reason="", actor_type="user", actor_id=None)
    assert e.value.code == "INVALID_TRANSITION"


def test_ownership_check_for_client_actions(db, world):
    b = _create(db, world, at(future_day(), 12))
    stranger = Contact(tenant_id=world.tenant.id, phone_e164="+77070000099", name="Чужой")
    db.add(stranger)
    db.commit()
    for call in (
        lambda: svc.cancel_booking(
            db, world.tenant.id, b.id, reason="", actor_type="ai", actor_id=None, contact_id=stranger.id
        ),
        lambda: svc.reschedule_booking(
            db,
            world.tenant.id,
            b.id,
            new_start_at=at(future_day(), 15),
            actor_type="ai",
            actor_id=None,
            contact_id=stranger.id,
        ),
    ):
        with pytest.raises(DomainError) as e:
            call()
        assert e.value.status_code == 404
    assert svc.get_booking_for_contact(db, world.tenant.id, str(b.id), stranger.id) is None
    assert svc.get_booking_for_contact(db, world.tenant.id, "BK-000001", world.contact.id).id == b.id
    assert svc.get_booking_for_contact(db, world.tenant.id, "#1", world.contact.id).id == b.id
    assert svc.get_booking_for_contact(db, world.tenant.id, "nonsense", world.contact.id) is None
    assert [x.id for x in svc.list_contact_bookings(db, world.tenant.id, world.contact.id)] == [b.id]
    assert svc.list_contact_bookings(db, world.tenant.id, stranger.id) == []


def test_status_machine_visit_and_repeat_lead(db, world):
    b = _create(db, world, at(future_day(), 12))
    with pytest.raises(DomainError) as e:
        svc.set_booking_status(db, world.tenant.id, b.id, "held", actor_type="user", actor_id=None)
    assert e.value.code == "INVALID_TRANSITION"
    svc.set_booking_status(db, world.tenant.id, b.id, "checked_in", actor_type="user", actor_id=None)
    svc.set_booking_status(db, world.tenant.id, b.id, "completed", actor_type="user", actor_id=None)
    db.commit()
    visits = db.scalars(select(Visit).where(Visit.booking_id == b.id)).all()
    assert len(visits) == 1 and visits[0].amount_due == Decimal(5000)
    assert db.scalar(select(Lead).where(Lead.booking_id == b.id)).stage == "visited"
    with pytest.raises(DomainError):
        svc.set_booking_status(db, world.tenant.id, b.id, "confirmed", actor_type="user", actor_id=None)

    # a returning client gets a new lead which entered the funnel as "repeat"
    again = _create(db, world, at(future_day(4), 12))
    lead = db.scalar(select(Lead).where(Lead.booking_id == again.id))
    assert lead.stage == "booked"
    assert any(
        e.payload.get("from") == "repeat" and e.payload.get("lead_id") == str(lead.id)
        for e in db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "lead.status_changed"))
    )
    db.refresh(world.contact)
    assert "повторный клиент" in world.contact.tags


def test_no_show_tags_contact(db, world):
    b = _create(db, world, at(future_day(), 12))
    svc.set_booking_status(db, world.tenant.id, b.id, "no_show", actor_type="user", actor_id=None)
    db.commit()
    db.refresh(world.contact)
    assert "не пришёл" in world.contact.tags
    assert db.query(Visit).count() == 0


def test_booking_to_dict_is_local_time(db, world):
    b = _create(db, world, at(future_day(), 12))
    d = svc.booking_to_dict(b)
    assert d["number"] == "BK-000001" and d["local_time"] == "12:00" and d["timezone"] == "Asia/Almaty"
    assert d["staff_name"] == "Арман" and d["services"][0]["title_ru"] == "Мужская стрижка"
