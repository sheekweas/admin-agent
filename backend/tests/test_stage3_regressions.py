"""Regressions for defects found by the stage-3 E2E / load work (see docs/ACCEPTANCE.md, docs/LOAD_TEST.md)."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from sqlalchemy import func, select

from app.channels import telegram
from app.channels import whatsapp as wa
from app.models import Conversation, Message, Notification
from app.services import booking as booking_svc
from app.services import messaging, schedule
from app.services import notifications as nt
from app.services.outbox import dispatch_outbox
from tests.ai_helpers import say
from tests.booking_helpers import at, future_day


@pytest.fixture(autouse=True)
def _mocks():
    wa.clear_mock_outbox()
    telegram.reset_mock()
    yield
    wa.clear_mock_outbox()
    telegram.reset_mock()


def test_shift_request_telegram_card_is_rendered(db, world):
    """render() took ``kind`` as its own parameter, so the shift_request template ({kind}) raised TypeError and
    the approve/reject card never reached the admins (the outbox event kept failing)."""
    assert "Тип: ранний уход" in nt.render("shift_request", "ru", kind="ранний уход")
    schedule.create_shift_request(
        db,
        world.tenant.id,
        staff_id=world.barber1.id,
        kind="early_leave",
        day=future_day(),
        new_start=None,
        new_end=time(17),
        reason="к врачу",
        actor_type="user",
        actor_id=str(world.staff_user.id),
    )
    db.commit()
    dispatch_outbox(db)
    nt.send_due_notifications(db)
    cards = [m for m in telegram.mock_messages("1001") if m["text"].startswith("ЗАПРОС НА ИЗМЕНЕНИЕ СМЕНЫ")]
    assert len(cards) == 1 and "ранний уход" in cards[0]["text"]
    assert db.scalar(select(Notification).where(Notification.kind == "shift_request")).status == "sent"


def test_message_queued_inside_savepoint_is_delivered_after_outer_commit(client, world, db):
    """SQLAlchemy fires after_commit for a released SAVEPOINT too: the after-commit hook used to try delivery
    at that moment (row not committed yet -> invisible), dropped the id and left the message 'queued' until the
    60 s sweep. Every worker path (reminders, confirmations, SLA notices) sends inside a savepoint."""
    say(client, world, "Здравствуйте")
    conv = db.scalar(select(Conversation).where(Conversation.tenant_id == world.tenant.id))
    wa.clear_mock_outbox()
    sp = db.begin_nested()
    msg = messaging.send_client_message(db, world.tenant.id, conv.id, "Напоминание", author="system")
    sp.commit()
    assert wa.mock_outbox() == []  # still nothing before the real COMMIT
    db.commit()
    db.expire_all()
    assert db.get(Message, msg.id).provider_status == "sent"
    assert [m["text"]["body"] for m in wa.mock_outbox()] == ["Напоминание"]


def _ai_booking(db, world):
    b = booking_svc.create_booking(
        db,
        world.tenant.id,
        contact_id=world.contact.id,
        service_id=world.haircut.id,
        start_at=at(future_day(), 12),
        staff_id=world.barber1.id,
        idempotency_key=None,
        channel="whatsapp",
        source="ai_agent",
        actor_type="ai",
        actor_id=None,
    )
    db.commit()
    return b


@pytest.mark.parametrize(("actor_type", "notified"), [("user", True), ("ai", False)])
def test_reschedule_of_ai_booking_notifies_client_unless_done_by_ai(db, world, actor_type, notified):
    """A reschedule keeps source='ai_agent'; the client confirmation was skipped even when an ADMIN moved the
    booking (e.g. resolving an early-leave conflict), so the client was never told about the new time."""
    b = _ai_booking(db, world)
    dispatch_outbox(db, deliver=False)
    new = booking_svc.reschedule_booking(
        db,
        world.tenant.id,
        b.id,
        new_start_at=at(future_day(), 12) + timedelta(hours=5),
        new_staff_id=world.barber2.id,
        actor_type=actor_type,
        actor_id=str(world.admin.id) if actor_type == "user" else None,
        contact_id=world.contact.id if actor_type == "ai" else None,
    )
    db.commit()
    dispatch_outbox(db, deliver=False)
    confirmations = db.scalars(
        select(Notification).where(
            Notification.kind == "booking_confirmation", Notification.dedupe_key == f"confirm:{new.id}"
        )
    ).all()
    assert len(confirmations) == (1 if notified else 0)


def test_concurrent_same_idempotency_key_returns_one_booking(db, world):
    """Load test: 20 concurrent POST /v1/bookings with the SAME Idempotency-Key -> 6 x 201 + 14 x 409. Requests
    that waited for the staff lock found the slot taken and raised SLOT_UNAVAILABLE without re-checking the key."""
    import threading

    from app.db import SessionLocal
    from app.models import Booking
    from app.services.common import DomainError

    n, start = 8, at(future_day(), 12)
    barrier = threading.Barrier(n)
    results: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        s = SessionLocal()
        try:
            barrier.wait(timeout=10)
            b = booking_svc.create_booking(
                s,
                world.tenant.id,
                contact_id=world.contact.id,
                service_id=world.haircut.id,
                start_at=start,
                staff_id=world.barber1.id,
                idempotency_key="same-key",
                channel="admin",
                source="admin",
                actor_type="user",
                actor_id=None,
            )
            s.commit()
            out: object = b.id
        except DomainError as exc:
            s.rollback()
            out = exc
        finally:
            s.close()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(results) == n
    assert not [r for r in results if isinstance(r, DomainError)], results
    assert len(set(results)) == 1
    assert db.scalar(select(func.count(Booking.id))) == 1


def test_post_commit_delivery_does_not_hold_a_second_pooled_connection(client, world, db, monkeypatch):
    """Webhook burst: delivery ran inside after_commit while the committing session still held its pooled
    connection -> 2 connections per thread, QueuePool exhausted (30 s timeouts, 500s). It now runs after the
    root transaction has released its connection."""
    from app.db import engine

    say(client, world, "Здравствуйте")
    conv = db.scalar(select(Conversation).where(Conversation.tenant_id == world.tenant.id))
    db.commit()
    seen: list[int] = []
    real = messaging.deliver_queued

    def spy(s, mid):
        out = real(s, mid)  # the delivery session's own connection is checked out now
        seen.append(engine.pool.checkedout())
        return out

    monkeypatch.setattr(messaging, "deliver_queued", spy)
    messaging.send_client_message(db, world.tenant.id, conv.id, "Привет", author="ai")
    db.commit()
    assert seen == [1]


def test_ai_turn_and_inbound_of_same_client_do_not_deadlock(client, world, db):
    """Webhook burst: handle_inbound's flush updates contact then conversation; process_ai_turn locked the
    conversation first and then updated the contact -> DeadlockDetected (lost AI replies / 500s).
    The AI turn now takes the contact lock first."""
    import threading
    import time as _time

    from sqlalchemy import text

    from app.db import SessionLocal
    from app.models import Contact

    say(client, world, "Здравствуйте")
    conv = db.scalar(select(Conversation).where(Conversation.tenant_id == world.tenant.id))
    msg = Message(
        tenant_id=world.tenant.id,
        conversation_id=conv.id,
        direction="in",
        author="client",
        text="Ер адамның шаш қиюы қанша тұрады?",
        provider_status="received",
        received_at=messaging.utcnow(),
        meta={},
    )
    db.add(msg)
    db.commit()
    contact_id = conv.contact_id

    inbound = SessionLocal()  # plays handle_inbound: holds the contact row, then needs the conversation
    inbound.execute(text("SET lock_timeout = '10s'"))
    inbound.get(Contact, contact_id).last_contact_at = messaging.utcnow()
    inbound.flush()
    errors: list[Exception] = []

    def ai_turn() -> None:
        s = SessionLocal()
        try:
            s.execute(text("SET lock_timeout = '10s'"))
            messaging.process_ai_turn(s, world.tenant.id, conv.id, msg.id)
            s.commit()
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            errors.append(exc)
        finally:
            s.close()

    t = threading.Thread(target=ai_turn)
    t.start()
    _time.sleep(0.5)  # let the AI turn reach its first lock
    try:
        inbound.get(Conversation, conv.id).unread_count = 5
        inbound.flush()
        inbound.commit()
    finally:
        inbound.close()
    t.join(timeout=30)
    assert not errors, errors
    db.expire_all()
    assert db.get(Message, msg.id).processed_at is not None
    # process_ai_turn swallows AI-side failures (fallback reply), so check the turn's own effect survived
    assert db.get(Contact, contact_id).language == "kk"
