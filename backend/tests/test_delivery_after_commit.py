"""Client messages are delivered only after the producing transaction commits (ТЗ §7.3: never tell the
client "you are booked" unless the booking is committed)."""

from datetime import timedelta

from sqlalchemy import select

from app.channels import whatsapp as wa
from app.models import Conversation, Message
from app.services import messaging
from app.services.common import utcnow
from tests.ai_helpers import say


def _conv(db, world):
    return db.scalar(select(Conversation).where(Conversation.tenant_id == world.tenant.id))


def test_rollback_sends_nothing(client, world, db):
    say(client, world, "Здравствуйте")
    conv = _conv(db, world)
    wa.clear_mock_outbox()
    messaging.send_client_message(db, world.tenant.id, conv.id, "Вы записаны!", author="ai")
    assert wa.mock_outbox() == []  # not sent before commit
    db.rollback()
    assert wa.mock_outbox() == []
    assert db.scalar(select(Message).where(Message.text == "Вы записаны!")) is None


def test_commit_delivers_once(client, world, db):
    say(client, world, "Здравствуйте")
    conv = _conv(db, world)
    wa.clear_mock_outbox()
    msg = messaging.send_client_message(db, world.tenant.id, conv.id, "Подтверждено", author="ai")
    db.commit()
    db.refresh(msg)
    assert msg.provider_status == "sent"
    assert [m["text"]["body"] for m in wa.mock_outbox()] == ["Подтверждено"]
    assert messaging.deliver_queued(db, msg.id) is None  # already sent -> no duplicate


def test_sweep_delivers_messages_left_queued(client, world, db):
    say(client, world, "Здравствуйте")
    conv = _conv(db, world)
    wa.clear_mock_outbox()
    stuck = Message(
        tenant_id=world.tenant.id,
        conversation_id=conv.id,
        direction="out",
        author="ai",
        text="Зависшее сообщение",
        provider_status="queued",
        received_at=utcnow() - timedelta(minutes=5),
        meta={"kind": "service"},
    )
    db.add(stuck)
    db.commit()
    assert messaging.deliver_stale_queued(db) == 1
    db.refresh(stuck)
    assert stuck.provider_status == "sent"
    assert messaging.deliver_stale_queued(db) == 0
