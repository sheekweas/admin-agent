"""Outbox dispatcher (ТЗ §18.2) and worker tasks: AT-10, idempotent redelivery, event routing."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.channels import telegram
from app.models import Notification, OutboxEvent, WebhookSubscription
from app.security import encrypt_secret
from app.services import escalation, outbox
from app.services.common import emit_event
from app.workers import tasks
from tests.ops_helpers import T0, SentToClient, make_booking, reset_mocks


@pytest.fixture(autouse=True)
def _mocks():
    reset_mocks()
    yield
    reset_mocks()


@pytest.fixture
def sent(monkeypatch) -> SentToClient:
    rec = SentToClient()
    monkeypatch.setattr("app.services.messaging.send_client_message", rec)
    return rec


def _booking_event(db, world, b, event_type="booking.confirmed"):
    emit_event(db, world.tenant.id, event_type, "booking", b.id, {"booking_id": str(b.id)})
    db.commit()


def test_at10_telegram_down_nothing_lost(db, world):
    escalation.create_admin_task(db, world.tenant.id, question="Цена окрашивания?", now=T0)
    db.commit()
    telegram.set_mock_outage(True)
    assert outbox.dispatch_outbox(db, T0) == 1
    ev = db.query(OutboxEvent).one()
    assert ev.status == "processed"  # the event became a durable notification
    n = db.query(Notification).filter_by(channel="telegram").one()
    assert n.status == "pending" and n.attempts == 1 and "outage" in n.last_error
    telegram.set_mock_outage(False)
    assert tasks.run_send_due_notifications(db, T0 + timedelta(minutes=1)) == 1
    db.refresh(n)
    assert n.status == "sent" and telegram.mock_messages("1001")


def test_redelivery_of_processed_event_is_idempotent(db, world, sent):
    b = make_booking(db, world, start=T0 + timedelta(days=2))
    _booking_event(db, world, b)
    outbox.dispatch_outbox(db, T0)
    before = db.query(Notification).count()
    ev = db.query(OutboxEvent).one()
    ev.status = "pending"  # e.g. worker crashed after sending but before commit
    db.commit()
    outbox.dispatch_outbox(db, T0)
    assert db.query(Notification).count() == before
    assert len(telegram.mock_messages()) == 2  # 1001 + 1002 once each


def test_handler_failure_keeps_event_with_backoff_then_failed(db, world, monkeypatch):
    def broken(db, ev):
        raise RuntimeError("bug")

    monkeypatch.setitem(outbox.HANDLERS, "booking.confirmed", broken)
    b = make_booking(db, world, start=T0 + timedelta(days=2))
    _booking_event(db, world, b)
    outbox.dispatch_outbox(db, T0)
    ev = db.query(OutboxEvent).one()
    assert ev.status == "pending" and ev.attempts == 1 and ev.next_attempt_at == T0 + timedelta(seconds=30)
    assert outbox.dispatch_outbox(db, T0 + timedelta(seconds=5)) == 0  # backoff respected
    now = T0
    for _ in range(outbox.OUTBOX_MAX_ATTEMPTS):
        now += timedelta(hours=2)
        outbox.dispatch_outbox(db, now)
    db.refresh(ev)
    assert ev.status == "failed" and "bug" in ev.last_error
    monkeypatch.undo()
    outbox.retry_event(db, world.tenant.id, ev.id)
    db.commit()
    assert outbox.dispatch_outbox(db, now) == 1


def test_booking_confirmed_fans_out(db, world, sent):
    b = make_booking(db, world, start=T0 + timedelta(days=2), source="admin")
    db.add(
        WebhookSubscription(
            tenant_id=world.tenant.id,
            url="https://hooks.example.com/x",
            events=["booking.confirmed"],
            secret=encrypt_secret("s"),
        )
    )
    db.commit()
    _booking_event(db, world, b)
    outbox.dispatch_outbox(db, T0, deliver=False)
    kinds = sorted((n.channel, n.kind) for n in db.query(Notification).all())
    assert kinds == [
        ("telegram", "new_booking"),
        ("telegram", "new_booking"),
        ("webhook", "booking.confirmed"),
        ("whatsapp", "booking_confirmation"),
        ("whatsapp", "reminder"),
        ("whatsapp", "reminder"),
    ]


def test_ai_booking_gets_no_duplicate_client_confirmation(db, world):
    b = make_booking(db, world, start=T0 + timedelta(days=2), source="ai_agent")
    _booking_event(db, world, b)
    outbox.dispatch_outbox(db, T0, deliver=False)
    assert db.query(Notification).filter_by(kind="booking_confirmation").count() == 0


def test_cancel_event_cancels_reminders_and_alerts(db, world):
    b = make_booking(db, world, start=T0 + timedelta(days=2))
    _booking_event(db, world, b)
    outbox.dispatch_outbox(db, T0, deliver=False)
    b.status, b.cancel_reason = "cancelled", "client asked"
    _booking_event(db, world, b, "booking.cancelled")
    outbox.dispatch_outbox(db, T0)
    assert {n.status for n in db.query(Notification).filter_by(kind="reminder")} == {"cancelled"}
    assert any("ОТМЕНА ЗАПИСИ" in m["text"] and "client asked" in m["text"] for m in telegram.mock_messages())


def test_reschedule_moves_reminders(db, world):
    old = make_booking(db, world, start=T0 + timedelta(days=2))
    _booking_event(db, world, old)
    outbox.dispatch_outbox(db, T0, deliver=False)
    old.status = "rescheduled"
    new = make_booking(db, world, start=T0 + timedelta(days=3))
    new.rescheduled_from_id = old.id
    old.rescheduled_to_id = new.id
    _booking_event(db, world, new, "booking.rescheduled")
    outbox.dispatch_outbox(db, T0)
    reminders = db.query(Notification).filter_by(kind="reminder").all()
    assert sorted(n.status for n in reminders) == ["cancelled", "cancelled", "pending", "pending"]
    assert any("ПЕРЕНОС ЗАПИСИ" in m["text"] for m in telegram.mock_messages())


def test_worker_wrappers_are_plain_callables(db, world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.booking.release_expired_holds", lambda s, now=None: calls.append(now) or 3
    )
    assert tasks.run_release_expired_holds(db, T0) == 3 and calls == [T0]
    assert tasks.run_process_escalations(db, T0) == 0
    assert tasks.run_dispatch_outbox(db, T0) == 0
    assert tasks.run_reconcile_integrations(db, T0) == 0
    seen = []
    monkeypatch.setattr("app.services.messaging.process_ai_turn", lambda s, t, c, m: seen.append((t, c, m)))
    from tests.ops_helpers import make_conversation

    conv = make_conversation(db, world)
    import uuid

    mid = uuid.uuid4()
    tasks.run_process_inbound_message(conv.id, mid, db=db)
    assert seen == [(world.tenant.id, conv.id, mid)]


def test_daily_summary_once_per_local_date(db, world):
    from datetime import UTC, datetime

    at_21 = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # 21:00 Almaty
    assert tasks.run_daily_summary(db, at_21 - timedelta(hours=1)) == 0
    assert tasks.run_daily_summary(db, at_21) == 2
    assert tasks.run_daily_summary(db, at_21 + timedelta(minutes=10)) == 0
    assert any(t["text"].startswith("СВОДКА ЗА 16.11.2026") for t in telegram.mock_messages())


def test_reconcile_alerts_on_failed_events(db, world):
    db.add(
        OutboxEvent(
            tenant_id=world.tenant.id,
            event_type="booking.created",
            aggregate_type="booking",
            status="failed",
            attempts=10,
        )
    )
    db.commit()
    assert tasks.run_reconcile_integrations(db, T0) == 1
    assert tasks.run_reconcile_integrations(db, T0 + timedelta(minutes=5)) == 0  # once per hour
