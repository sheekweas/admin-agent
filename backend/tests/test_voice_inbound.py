"""Inbound AI phone calls (ТЗ §4.5, §13.2): booking RU/KK through the real orchestrator + booking engine,
WhatsApp written confirmation, AT-12 re-asks, transfer to a human / callback task."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.channels import whatsapp
from app.models import AdminTask, AIRun, Booking, Call, Conversation, Message, Notification
from app.services.notifications import send_due_notifications
from tests.ai_helpers import say
from tests.voice_helpers import (
    action_path,
    add_voice_integration,
    call_by_sid,
    inbound,
    no_quiet_hours,
    said,
    set_settings,
    turn,
)


@pytest.fixture(autouse=True)
def _voice(db, world):
    add_voice_integration(db, world.tenant)
    whatsapp.MOCK_OUTBOX.clear()
    yield
    whatsapp.MOCK_OUTBOX.clear()


def _bookings(db) -> list[Booking]:
    db.expire_all()
    return list(db.scalars(select(Booking)).all())


def _voice_messages(db, call: Call) -> list[Message]:
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == call.conversation_id)
            .order_by(Message.received_at)
        ).all()
    )


def test_inbound_ru_booking_with_whatsapp_confirmation(client, db, world):
    phone = "+77015550001"
    say(client, world, "Здравствуйте", phone=phone, name="Айдар")  # the client already uses WhatsApp
    whatsapp.MOCK_OUTBOX.clear()

    r, sid = inbound(client, phone=phone)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    xml = r.text
    assert "виртуальный помощник" in said(xml) and "искусственный интеллект" in said(xml)
    assert '<Gather input="speech" language="ru-RU"' in xml and 'bargeIn="true"' in xml
    assert 'speechTimeout="auto"' in xml and "turn?call_id=" in xml
    assert "записывается" not in said(xml)  # recording is off by default
    assert phone not in xml and "77015550001" not in xml  # no client PII in TwiML
    call = call_by_sid(db, sid)
    assert (call.direction, call.purpose, call.status) == ("in", "client_ai", "in_progress")
    assert call.contact_id is not None and call.started_at is not None

    xml = turn(client, xml, "Здравствуйте, хочу записаться на мужскую стрижку завтра в 16:00")
    assert "записать вас" in said(xml) and "16:00" in said(xml) and _bookings(db) == []
    assert "Айдар" in said(xml)  # the name the client gave (no other PII)
    xml = turn(client, xml, "Да")
    (b,) = _bookings(db)
    assert b.channel == "voice" and b.source == "ai_agent" and b.status == "confirmed"
    assert "Вы записаны" in said(xml) and "WhatsApp" in said(xml)
    assert "BK-" not in said(xml) and str(b.number) in said(xml)  # booking id read naturally
    assert ".20" not in said(xml)  # dd.mm.yyyy turned into words

    call = call_by_sid(db, sid)
    assert call.booking_id == b.id
    assert call.metrics["booking_created"] is True and call.metrics["turns"] == 2
    assert call.metrics["first_response_ms"] is not None and call.metrics["transferred_to_human"] is False
    speakers = [i["speaker"] for i in call.transcript_items]
    assert speakers == ["ai", "client", "ai", "client", "ai"]
    assert call.transcript.startswith("ai: ") and "\nclient: Да\n" in call.transcript
    assert call.transcript_items[1]["confidence"] == 0.93
    msgs = _voice_messages(db, call)
    assert {m.message_type for m in msgs} == {"voice_turn"} and len(msgs) == 5
    conv = db.get(Conversation, call.conversation_id)
    assert conv.channel == "voice"
    assert db.scalar(select(func.count(AIRun.id)).where(AIRun.conversation_id == conv.id)) == 2

    # written confirmation via WhatsApp (ТЗ §13.2): queued notification -> delivered by the worker
    n = db.scalar(select(Notification).where(Notification.kind == "booking_confirmation"))
    assert n is not None and n.channel == "whatsapp" and n.dedupe_key == f"confirm:{b.id}"
    send_due_notifications(db)
    sent = [m for m in whatsapp.MOCK_OUTBOX if m.get("to") == phone.lstrip("+")]
    assert len(sent) == 1 and f"BK-{b.number:06d}" in str(sent[0])

    # provider status callback configured on the number (no call_id): looked up by CallSid
    r = client.post(
        "/v1/webhooks/voice/mock/status",
        data={"CallSid": sid, "CallStatus": "completed", "CallDuration": "95"},
    )
    assert r.status_code == 200
    call = call_by_sid(db, sid)
    assert call.status == "completed" and call.duration_sec == 95 and call.ended_at is not None
    assert db.get(Conversation, call.conversation_id).status == "closed"


def test_inbound_kk_booking_with_whatsapp_confirmation(client, db, world):
    world.tenant.default_language = "kk"
    db.commit()
    phone = "+77015550002"
    say(client, world, "Сәлеметсіз бе! Қалайсыз?", phone=phone, name="")
    whatsapp.MOCK_OUTBOX.clear()

    r, sid = inbound(client, phone=phone)
    xml = r.text
    assert "Сәлеметсіз бе" in said(xml) and "жасанды интеллект" in said(xml)
    assert 'language="kk-KZ"' in xml  # STT in Kazakh
    assert 'voice="Polly.Tatyana" language="ru-RU"' in xml  # documented KK TTS fallback
    xml = turn(client, xml, "Ертең сағат 16:00-де шаш қиюға жазылғым келеді")
    assert "Атыңызды" in said(xml)
    xml = turn(client, xml, "Нұрлан")
    assert "растайсыз ба" in said(xml) and _bookings(db) == []
    xml = turn(client, xml, "Иә")
    (b,) = _bookings(db)
    assert "Жазылуыңыз расталды" in said(xml) and "WhatsApp" in said(xml)
    assert "қараша" in said(xml) or any(m in said(xml) for m in ("желтоқсан", "қаңтар"))  # month in words
    call = call_by_sid(db, sid)
    assert call.language == "kk" and call.booking_id == b.id and call.metrics["booking_created"]
    send_due_notifications(db)
    sent = [m for m in whatsapp.MOCK_OUTBOX if m.get("to") == phone.lstrip("+")]
    assert len(sent) == 1 and "расталды" in str(sent[0])


def test_no_whatsapp_confirmation_without_whatsapp_channel(client, db, world):
    r, _ = inbound(client, phone="+77015550009")
    xml = turn(client, r.text, "Хочу записаться на мужскую стрижку завтра в 16:00")
    xml = turn(client, xml, "Меня зовут Ерлан")
    xml = turn(client, xml, "Да")
    assert len(_bookings(db)) == 1 and "WhatsApp" not in said(xml)
    assert db.scalar(select(Notification).where(Notification.kind == "booking_confirmation")) is None


def test_at12_voice_ambiguous_date_and_low_confidence_are_reasked(client, db, world):
    r, sid = inbound(client)
    xml = turn(client, r.text, "Запишите меня на стрижку на выходных")
    assert "Уточните" in said(xml) and _bookings(db) == []
    runs = db.scalar(select(func.count(AIRun.id)))
    xml = turn(client, xml, "завтра в шестнадцать", confidence=0.31)  # below the 0.5 threshold
    assert "не расслышала" in said(xml) and "<Gather" in xml
    assert db.scalar(select(func.count(AIRun.id))) == runs  # the AI never saw the unreliable text
    xml = turn(client, xml, "Завтра в 16:00", confidence=0.88)
    assert "16:00" in said(xml) and _bookings(db) == []
    call = call_by_sid(db, sid)
    assert call.metrics["recognition_retries"] == 1 and call.metrics["turns"] == 2
    low = [i for i in call.transcript_items if i.get("confidence") == 0.31]
    assert low and low[0]["speaker"] == "client"


def test_two_failed_recognitions_transfer_to_on_duty_admin_then_callback(client, db, world):
    no_quiet_hours(db, world.tenant)
    r, sid = inbound(client)
    xml = turn(client, r.text, "", confidence=None)  # silence
    assert "не расслышала" in said(xml)
    xml = turn(client, xml, "ммм", confidence=0.2)
    assert "<Dial" in xml and "<Number>+77010000001</Number>" in xml and "Соединяю" in said(xml)
    call = call_by_sid(db, sid)
    assert call.metrics["transferred_to_human"] is True and call.metrics["recognition_retries"] == 2
    assert db.get(Conversation, call.conversation_id).mode == "waiting_admin"
    # the admin does not pick up -> callback task, "we will call you back", hang up
    r = client.post(action_path(xml, "Dial"), data={"DialCallStatus": "no-answer"})
    assert "перезвоним" in said(r.text) and r.text.endswith("<Hangup/></Response>")
    call = call_by_sid(db, sid)
    task = db.get(AdminTask, call.admin_task_id)
    assert task.kind == "callback" and task.contact_id == call.contact_id and task.priority == "high"
    assert "распознать" in task.question
    assert call.status == "completed" and call.ended_at is not None


def test_failed_recognitions_outside_hours_create_callback_task_without_dial(client, db, world):
    set_settings(db, world.tenant, escalation={"quiet_hours": [0, 24]})  # always quiet
    r, sid = inbound(client)
    xml = turn(client, r.text, "", confidence=None)
    xml = turn(client, xml, "", confidence=None)
    assert "<Dial" not in xml and "перезвоним" in said(xml) and "<Hangup/>" in xml
    call = call_by_sid(db, sid)
    assert db.get(AdminTask, call.admin_task_id).kind == "callback"
    assert call.status == "completed"


@pytest.mark.parametrize("phrase", ["Соедините меня с оператором", "человек", "Адаммен сөйлескім келеді"])
def test_human_request_dials_admin(client, db, world, phrase):
    no_quiet_hours(db, world.tenant)
    r, sid = inbound(client)
    xml = turn(client, r.text, phrase)
    assert "<Dial" in xml and "+77010000001" in xml
    r = client.post(action_path(xml, "Dial"), data={"DialCallStatus": "completed", "DialCallDuration": "42"})
    assert r.text.endswith("<Hangup/></Response>")
    call = call_by_sid(db, sid)
    assert call.metrics["transferred_to_human"] and call.admin_task_id is None
    assert any(i["speaker"] == "admin" for i in call.transcript_items)
    assert db.scalar(select(func.count(AIRun.id))) == 0  # the AI was not asked


def test_kk_men_haircut_is_not_a_human_request(client, db, world):
    r, _ = inbound(client)
    xml = turn(client, r.text, "Ер адамның шаш қиюы қанша тұрады?")
    assert "<Dial" not in xml and "5000" in said(xml)


def test_retried_turn_webhook_is_idempotent(client, db, world):
    r, sid = inbound(client)
    path = action_path(r.text)
    first = client.post(path, data={"SpeechResult": "Сколько стоит мужская стрижка?", "Confidence": "0.9"})
    again = client.post(path, data={"SpeechResult": "Сколько стоит мужская стрижка?", "Confidence": "0.9"})
    assert first.text == again.text
    assert db.scalar(select(func.count(AIRun.id))) == 1
    r2, _ = inbound(client, sid=sid)  # retried inbound webhook: same call
    assert db.scalar(select(func.count(Call.id))) == 1 and r2.status_code == 200


def test_ai_escalation_during_call_transfers(client, db, world):
    set_settings(db, world.tenant, escalation={"quiet_hours": [0, 24]})
    r, sid = inbound(client)
    xml = turn(client, r.text, "Можно скидку 30 процентов?")
    assert "администратор" in said(xml) and "<Hangup/>" in xml
    call = call_by_sid(db, sid)
    tasks = db.scalars(select(AdminTask)).all()
    assert len(tasks) == 1 and call.admin_task_id == tasks[0].id  # no duplicate callback task
    assert call.metrics["transferred_to_human"]


def test_recording_only_with_notice_and_without_refusal(client, db, world):
    set_settings(db, world.tenant, voice={"recording_enabled": True})
    r, sid = inbound(client)
    assert "записывается" in said(r.text)
    turn(client, r.text, "Сколько стоит борода?")
    call = call_by_sid(db, sid)
    assert call.meta["recording_started"] is True
    rec = client.post(
        f"/v1/webhooks/voice/mock/recording?call_id={call.id}",
        data={"RecordingStatus": "completed", "RecordingUrl": "https://api.twilio.com/rec/RE1"},
    )
    assert rec.status_code == 200
    assert call_by_sid(db, sid).recording_url == "https://api.twilio.com/rec/RE1"

    # a client who refused recording is not recorded and the URL is never stored
    world.contact.consent_flags = {"processing": True, "call_recording": False}
    db.commit()
    r, sid2 = inbound(client, phone=world.contact.phone_e164)
    assert "записывается" not in said(r.text)
    call2 = call_by_sid(db, sid2)
    client.post(
        f"/v1/webhooks/voice/mock/recording?call_id={call2.id}",
        data={"RecordingStatus": "completed", "RecordingUrl": "https://api.twilio.com/rec/RE2"},
    )
    assert call_by_sid(db, sid2).recording_url is None


def test_unknown_called_number_is_rejected(client, db, world):
    r, _ = inbound(client, to="+77279999999")
    assert r.status_code == 404
    assert db.scalar(select(func.count(Call.id))) == 0
