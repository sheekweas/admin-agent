"""Admin notifications (recipients, dedupe, retry/backoff/dead) and client reminders."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.channels import telegram
from app.models import Booking, Contact, Membership, Notification
from app.services import notifications as nt
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


def test_recipients_are_admins_with_chat_in_branch_scope(db, world, other_world):
    world.owner.telegram_chat_id = "999"
    m = db.query(Membership).filter_by(user_id=world.backup_admin.id).one()
    m.branch_scope = [str(other_world.branch.id)]  # admin of another branch only
    world.staff_user.telegram_chat_id = "555"  # staff never gets admin alerts
    db.commit()
    out = nt.notify_admins(db, world.tenant.id, "test", "hello", branch_id=world.branch.id)
    assert sorted(n.recipient for n in out) == ["1001", "999"]
    out = nt.notify_admins(db, world.tenant.id, "test", "hello")  # no branch -> everyone
    assert sorted(n.recipient for n in out) == ["1001", "1002", "999"]


def test_dedupe_key_makes_notify_idempotent(db, world):
    assert len(nt.notify_admins(db, world.tenant.id, "k", "x", dedupe_key="ev:1")) == 2
    assert nt.notify_admins(db, world.tenant.id, "k", "x", dedupe_key="ev:1") == []
    db.commit()
    assert db.query(Notification).count() == 2


def test_delivery_retries_with_backoff_then_dead(db, world):
    nt.notify_admins(db, world.tenant.id, "k", "hello", recipients=["1001"], now=T0)
    db.commit()
    telegram.set_mock_outage(True)
    assert nt.send_due_notifications(db, T0) == 0
    n = db.query(Notification).one()
    assert n.status == "pending" and n.attempts == 1 and n.next_attempt_at == T0 + timedelta(seconds=30)
    assert nt.send_due_notifications(db, T0 + timedelta(seconds=10)) == 0  # not due yet
    db.refresh(n)
    assert n.attempts == 1
    now = T0
    for _ in range(nt.NOTIFY_MAX_ATTEMPTS):
        now += timedelta(hours=2)
        nt.send_due_notifications(db, now)
    db.refresh(n)
    assert n.status == "dead" and n.attempts == nt.NOTIFY_MAX_ATTEMPTS and "outage" in n.last_error
    nt.retry_notification(db, world.tenant.id, n.id, now)  # manual retry from the DLQ
    db.commit()
    telegram.set_mock_outage(False)
    assert nt.send_due_notifications(db, now) == 1
    db.refresh(n)
    assert n.status == "sent" and n.provider_ref


def test_permanent_telegram_error_goes_dead_immediately(db, world, monkeypatch):
    def boom(self, method, payload):
        raise telegram.TelegramError("HTTP 403: bot was blocked by the user", retryable=False)

    monkeypatch.setattr(telegram.TelegramClient, "_call", boom)
    nt.notify_admins(db, world.tenant.id, "k", "x", recipients=["1001"], now=T0)
    db.commit()
    nt.send_due_notifications(db, T0)
    assert db.query(Notification).one().status == "dead"


def test_new_booking_template_matches_spec(db, world):
    b = make_booking(db, world, start=T0 + timedelta(days=1))
    ctx = nt.booking_context(db, b, "ru")
    text = nt.render("new_booking", "ru", **ctx)
    assert text.startswith(f"НОВАЯ ЗАПИСЬ  #BK-{b.number:06d}")
    assert "Клиент: Нурлан  Телефон: +7 707 123 45 67" in text
    assert "Услуга: Мужская стрижка  Мастер: Арман" in text
    assert "Дата: 17.11.2026  Время: 11:00" in text and "Филиал: Центральный" in text
    kk = nt.render("new_booking", "kk", **nt.booking_context(db, b, "kk"))
    assert "ЖАҢА ЖАЗЫЛУ" in kk and "Ер адамның шаш қиюы" in kk


def test_reminders_scheduled_cancelled_and_rechecked(db, world, sent):
    b = make_booking(db, world, start=T0 + timedelta(days=3))
    out = nt.schedule_booking_reminders(db, world.tenant.id, b, now=T0)
    db.commit()
    assert sorted(n.scheduled_for for n in out) == [
        b.start_at - timedelta(hours=24),
        b.start_at - timedelta(hours=2),
    ]
    assert nt.schedule_booking_reminders(db, world.tenant.id, b, now=T0) == []  # idempotent
    # not due yet
    assert nt.send_due_notifications(db, T0 + timedelta(days=1)) == 0
    # 24h reminder due: sent via messaging (transactional, even for opted-out contacts)
    db.get(Contact, world.contact.id).opted_out = True
    db.commit()
    assert nt.send_due_notifications(db, b.start_at - timedelta(hours=24)) == 1
    assert "Напоминаем о записи: Мужская стрижка" in sent.calls[0]["text"]
    # booking cancelled -> pending 2h reminder is cancelled
    db.get(Booking, b.id).status = "cancelled"
    assert nt.cancel_booking_reminders(db, world.tenant.id, b.id) == 1
    db.commit()
    assert nt.send_due_notifications(db, b.start_at - timedelta(hours=1)) == 0
    assert len(sent.calls) == 1


def test_reminder_skipped_when_booking_no_longer_active(db, world, sent):
    b = make_booking(db, world, start=T0 + timedelta(days=3))
    nt.schedule_booking_reminders(db, world.tenant.id, b, now=T0)
    db.get(Booking, b.id).status = "cancelled"  # changed without going through the outbox
    db.commit()
    nt.send_due_notifications(db, b.start_at - timedelta(hours=2))
    assert sent.calls == []
    assert {n.status for n in db.query(Notification).all()} == {"cancelled"}
