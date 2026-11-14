"""WhatsApp Cloud API webhook: verification, signatures, idempotency (AT-02), statuses, 24h window, voice."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.ai.stt import MockSTT, set_stt
from app.channels import whatsapp as wa
from app.models import Contact, Conversation, Lead, Message, OutboxEvent
from app.services import messaging
from app.services.common import utcnow
from tests.ai_helpers import WA_PNID, add_wa_integration, post_webhook, wa_payload


@pytest.fixture(autouse=True)
def _outbox():
    wa.clear_mock_outbox()
    yield
    wa.clear_mock_outbox()
    set_stt(None)


def _count(db, model, *where) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def test_verify_challenge(client):
    ok = client.get(
        "/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "dev-verify-token", "hub.challenge": "12345"},
    )
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get(
        "/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"},
    )
    assert bad.status_code == 403


def test_bad_signature_rejected(client, world, db):
    add_wa_integration(db, world.tenant)
    r = post_webhook(client, wa_payload(text="Здравствуйте"), signature="sha256=deadbeef")
    assert r.status_code == 401
    r = post_webhook(client, wa_payload(text="Здравствуйте"), secret="wrong-secret")
    assert r.status_code == 401
    assert _count(db, Message) == 0


def test_per_tenant_app_secret_accepted(client, world, db):
    add_wa_integration(db, world.tenant, secrets={"access_token": "t", "app_secret": "tenant-secret"})
    r = post_webhook(client, wa_payload(text="Здравствуйте"), secret="tenant-secret")
    assert r.status_code == 200
    assert _count(db, Message, Message.direction == "in") == 1


def test_text_message_creates_contact_conversation_lead_and_ai_reply(client, world, db):
    add_wa_integration(db, world.tenant)
    r = post_webhook(client, wa_payload(text="Здравствуйте", wa_id="77015550001", name="Айдар"))
    assert r.status_code == 200 and r.json()["accepted"] == 1
    contact = db.scalar(
        select(Contact).where(Contact.tenant_id == world.tenant.id, Contact.phone_e164 == "+77015550001")
    )
    assert contact is not None and contact.name == "Айдар"
    conv = db.scalar(select(Conversation).where(Conversation.contact_id == contact.id))
    assert conv.mode == "ai" and conv.channel == "whatsapp"
    assert _count(db, Lead, Lead.contact_id == contact.id) == 1
    out = db.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.direction == "out")
    ).all()
    assert len(out) == 1 and out[0].author == "ai" and out[0].provider_status == "sent"
    sent = wa.mock_outbox()
    assert len(sent) == 1 and sent[0]["to"] == "77015550001" and sent[0]["type"] == "text"
    assert sent[0]["id"] == out[0].provider_message_id


def test_duplicate_webhook_is_noop(client, world, db):
    """AT-02: the same webhook delivered twice -> no duplicate message, lead or reply."""
    add_wa_integration(db, world.tenant)
    payload = wa_payload(text="Хочу записаться на стрижку завтра в 16:00", mid="wamid.DUP1")
    assert post_webhook(client, payload).status_code == 200
    assert post_webhook(client, payload).json()["accepted"] == 0
    assert _count(db, Message, Message.provider_message_id == "wamid.DUP1") == 1
    assert _count(db, Message, Message.direction == "out") == 1
    assert _count(db, Lead) == 1
    assert len(wa.mock_outbox()) == 1


def test_unknown_phone_number_id_ignored(client, world, db):
    add_wa_integration(db, world.tenant)
    r = post_webhook(client, wa_payload(pnid="SOMEONE-ELSE", text="Здравствуйте"))
    assert r.status_code == 200 and r.json()["accepted"] == 0
    assert _count(db, Message) == 0


def test_delivery_statuses_move_forward_and_failure_emits_event(client, world, db):
    add_wa_integration(db, world.tenant)
    post_webhook(client, wa_payload(text="Здравствуйте"))
    out = db.scalar(select(Message).where(Message.direction == "out"))
    wamid = out.provider_message_id

    def status(s: str, **extra) -> None:
        r = post_webhook(
            client,
            wa_payload(
                text=None,
                statuses=[
                    {
                        "id": wamid,
                        "status": s,
                        "timestamp": "1763100100",
                        "recipient_id": "77015550001",
                        **extra,
                    }
                ],
            ),
        )
        assert r.status_code == 200

    status("read")
    db.expire_all()
    assert db.get(Message, out.id).provider_status == "read"
    status("delivered")  # late receipt never downgrades
    db.expire_all()
    assert db.get(Message, out.id).provider_status == "read"
    status("failed", errors=[{"code": 131047, "title": "Re-engagement message"}])
    db.expire_all()
    failed = db.get(Message, out.id)
    assert failed.provider_status == "failed" and "131047" in failed.error
    assert _count(db, OutboxEvent, OutboxEvent.event_type == "message.delivery_failed") == 1


def test_parse_media_interactive_and_statuses():
    payload = wa_payload(
        message={"type": "audio", "audio": {"id": "MEDIA1", "mime_type": "audio/ogg", "voice": True}}
    )
    payload["entry"][0]["changes"][0]["value"]["messages"] += [
        {
            "from": "77015550001",
            "id": "wamid.I",
            "timestamp": "1763100000",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Да"}},
        },
        {
            "from": "77015550001",
            "id": "wamid.D",
            "timestamp": "1763100000",
            "type": "document",
            "document": {
                "id": "MEDIA2",
                "mime_type": "application/pdf",
                "filename": "x.pdf",
                "caption": "анализы",
            },
        },
        {
            "from": "77015550001",
            "id": "wamid.R",
            "timestamp": "1763100000",
            "type": "reaction",
            "reaction": {},
        },
    ]
    payload["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "wamid.S", "status": "delivered"}]
    (entry,) = wa.parse_webhook(payload)
    audio, inter, doc, other = entry.messages
    assert audio.message_type == "audio" and audio.media_id == "MEDIA1" and audio.meta["voice"] is True
    assert audio.to_normalized(__import__("uuid").uuid4()).media_url == "wa-media:MEDIA1"
    assert inter.message_type == "interactive" and inter.text == "Да" and inter.meta["reply_id"] == "yes"
    assert doc.message_type == "document" and doc.text == "анализы" and doc.meta["filename"] == "x.pdf"
    assert other.message_type == "unsupported"
    assert entry.statuses[0].status == "delivered"


def test_voice_note_is_transcribed_and_answered(client, world, db):
    add_wa_integration(db, world.tenant)
    set_stt(MockSTT("Сколько стоит мужская стрижка?"))
    post_webhook(
        client, wa_payload(message={"type": "audio", "audio": {"id": "MEDIA1", "mime_type": "audio/ogg"}})
    )
    inbound = db.scalar(select(Message).where(Message.direction == "in"))
    assert inbound.text == "Сколько стоит мужская стрижка?" and inbound.meta["transcribed"] is True
    reply = db.scalar(select(Message).where(Message.direction == "out"))
    assert "5000" in reply.text


def test_service_window_requires_template(client, world, db):
    integ = add_wa_integration(db, world.tenant)
    post_webhook(client, wa_payload(text="Здравствуйте"))
    conv = db.scalar(select(Conversation))
    inbound = db.scalar(select(Message).where(Message.direction == "in"))
    inbound.received_at = utcnow() - timedelta(hours=25)
    db.commit()
    wa.clear_mock_outbox()

    msg = messaging.send_client_message(db, world.tenant.id, conv.id, "Напоминаем о записи", author="system")
    db.commit()  # delivery runs after COMMIT in its own session
    db.refresh(msg)
    assert msg.provider_status == "failed" and msg.error.startswith("OUTSIDE_SERVICE_WINDOW")
    assert wa.mock_outbox() == []

    integ.config = {"template_name": "service_update", "template_language": "ru"}
    db.commit()
    msg = messaging.send_client_message(db, world.tenant.id, conv.id, "Напоминаем о записи", author="system")
    db.commit()
    db.refresh(msg)
    assert msg.provider_status == "sent" and msg.meta["template"] == "service_update"
    (sent,) = wa.mock_outbox()
    assert sent["type"] == "template" and sent["template"]["name"] == "service_update"
    assert sent["template"]["components"][0]["parameters"][0]["text"] == "Напоминаем о записи"


def test_service_window_helper():
    now = utcnow()
    assert wa.within_service_window(now - timedelta(hours=23), now)
    assert not wa.within_service_window(now - timedelta(hours=24, minutes=1), now)
    assert not wa.within_service_window(None, now)


def test_live_send_uses_tenant_token(world, db, monkeypatch):
    """Live mode posts to {api_base}/{phone_number_id}/messages with the tenant's decrypted token."""
    from app.config import get_settings

    add_wa_integration(db, world.tenant, secrets={"access_token": "SECRET-TOKEN"})
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    calls = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.LIVE1"}]}

    def fake_post(url, json, timeout, headers):
        calls.append((url, json, headers))
        return Resp()

    monkeypatch.setattr(wa.httpx, "post", fake_post)
    adapter = wa.WhatsAppAdapter.for_tenant(db, world.tenant.id)
    res = adapter.send_text("77015550001", "Привет")
    assert res.ok and res.provider_message_id == "wamid.LIVE1"
    url, body, headers = calls[0]
    assert url.endswith(f"/{WA_PNID}/messages") and headers["Authorization"] == "Bearer SECRET-TOKEN"
    assert body["text"]["body"] == "Привет"
