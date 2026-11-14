"""AI agent end-to-end with MockLLM through the mock WhatsApp channel (ТЗ §4.1-4.3, §7, §26)."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.ai.llm import LLMRequest, LLMResult, LLMUnavailable, set_llm
from app.channels import whatsapp as wa
from app.models import AdminTask, AIRun, Booking, Contact, Conversation, Lead, Message
from app.services import messaging
from app.services.common import utcnow
from tests.ai_helpers import reply_text, say


@pytest.fixture(autouse=True)
def _reset():
    wa.clear_mock_outbox()
    yield
    set_llm(None)
    wa.clear_mock_outbox()


def _tomorrow_local():
    return (utcnow().astimezone(ZoneInfo("Asia/Almaty")) + timedelta(days=1)).date()


def _bookings(db) -> list[Booking]:
    db.expire_all()
    return list(db.scalars(select(Booking)))


def _conv(db, resp) -> Conversation:
    db.expire_all()
    return db.get(Conversation, resp["conversation_id"])


def test_full_booking_dialog_ru(client, world, db):
    r = say(client, world, "Здравствуйте! Хочу записаться на мужскую стрижку завтра в 16:00")
    text = reply_text(r)
    assert "16:00" in text and "Мужская стрижка" in text and "записаны" not in text
    assert _bookings(db) == []  # nothing is booked before explicit confirmation

    r = say(client, world, "Да")
    (b,) = _bookings(db)
    local = b.start_at.astimezone(ZoneInfo("Asia/Almaty"))
    assert local.date() == _tomorrow_local() and local.strftime("%H:%M") == "16:00"
    assert b.source == "ai_agent" and b.channel == "whatsapp" and b.status == "confirmed"
    contact = db.get(Contact, b.contact_id)
    assert contact.phone_e164 == "+77015550001" and contact.name == "Айдар"
    text = reply_text(r)
    assert f"BK-{b.number:06d}" in text and "Вы записаны" in text and "Абая 1" in text
    lead = db.scalar(select(Lead).where(Lead.contact_id == contact.id).order_by(Lead.created_at.desc()))
    assert lead.stage == "booked"
    run = db.scalars(select(AIRun).order_by(AIRun.created_at.desc())).first()
    assert run.prompt_version and any(c["name"] == "create_booking" and c["ok"] for c in run.tool_calls)


def test_full_booking_dialog_kk_asks_name(client, world, db):
    phone = "+77015550002"
    r = say(
        client, world, "Сәлеметсіз бе! Ертең сағат 16:00-де шаш қиюға бос орын бар ма?", phone=phone, name=""
    )
    assert "Атыңызды" in reply_text(r)
    assert _conv(db, r).language == "kk"
    r = say(client, world, "Нұрлан", phone=phone, name="")
    assert "растайсыз ба" in reply_text(r) and _bookings(db) == []
    r = say(client, world, "Иә", phone=phone, name="")
    (b,) = _bookings(db)
    text = reply_text(r)
    assert "Жазылуыңыз расталды" in text and f"Бронь №BK-{b.number:06d}" in text and "16:00" in text
    assert db.get(Contact, b.contact_id).name == "Нұрлан"


def test_unknown_price_escalates_without_inventing(client, world, db):
    """AT-03: no price is invented; an admin task is created; the client is told a human will answer."""
    r = say(client, world, "Сколько стоит окрашивание волос?")
    text = reply_text(r)
    assert "администратор" in text and "₸" not in text and not any(ch.isdigit() for ch in text)
    task = db.scalar(select(AdminTask))
    assert task is not None and "окрашивание" in task.question
    assert task.conversation_id is not None and str(task.conversation_id) == r["conversation_id"]
    assert _conv(db, r).mode == "waiting_admin"
    assert "спросил цену" in db.scalar(select(Contact).where(Contact.phone_e164 == "+77015550001")).tags


def test_known_price_answered_from_catalogue(client, world, db):
    r = say(client, world, "Сколько стоит мужская стрижка?")
    assert "5000" in reply_text(r)
    assert db.scalar(select(func.count(AdminTask.id))) == 0
    lead = db.scalar(select(Lead))
    assert lead.stage == "info_requested"


def test_discount_question_escalates(client, world, db):
    r = say(client, world, "Можно сделать стрижку со скидкой 30%?")
    assert "администратор" in reply_text(r)
    assert db.scalar(select(func.count(AdminTask.id))) == 1


def test_ambiguous_date_is_clarified(client, world, db):
    """AT-12: an ambiguous date gets a clarifying question; nothing is booked."""
    r = say(client, world, "Запишите меня на стрижку на выходных")
    assert "Уточните" in reply_text(r)
    r = say(client, world, "Да")  # a bare "yes" must not book anything
    assert _bookings(db) == []
    r = say(client, world, "Завтра в 4")  # 04:00 or 16:00?
    assert "16:00" in reply_text(r) and "04:00" in reply_text(r)
    assert _bookings(db) == []
    r = say(client, world, "в 16:00")
    assert "записать вас" in reply_text(r)
    say(client, world, "да")
    assert len(_bookings(db)) == 1


def test_human_mode_silences_ai(client, world, db):
    """AT-15: after takeover the AI does not answer until released."""
    r = say(client, world, "Здравствуйте")
    conv_id = r["conversation_id"]
    assert (
        client.post(f"/v1/conversations/{conv_id}/takeover", headers=world.headers(world.admin)).status_code
        == 200
    )
    r = say(client, world, "Сколько стоит мужская стрижка?")
    assert r["replies"] == []
    msg = db.scalar(select(Message).where(Message.direction == "in").order_by(Message.received_at.desc()))
    assert msg.text == "Сколько стоит мужская стрижка?"  # stored for the human
    assert (
        client.post(f"/v1/conversations/{conv_id}/release", headers=world.headers(world.admin)).status_code
        == 200
    )
    r = say(client, world, "Сколько стоит мужская стрижка?")
    assert "5000" in reply_text(r)


def test_language_switch(client, world, db):
    """AT-16: the same client writes RU, then KK, then RU again."""
    r = say(client, world, "Здравствуйте")
    assert "Здравствуйте" in reply_text(r) and _conv(db, r).language == "ru"
    r = say(client, world, "Ер адамның шаш қиюы қанша тұрады?")
    assert "ұзақтығы" in reply_text(r) and "5000" in reply_text(r)
    conv = _conv(db, r)
    assert conv.language == "kk" and db.get(Contact, conv.contact_id).language == "kk"
    r = say(client, world, "Спасибо, а сколько стоит борода?")
    assert "длительность" in reply_text(r) and "3000" in reply_text(r) and _conv(db, r).language == "ru"


@pytest.mark.parametrize("text", ["Пожалуйста, не пишите мне больше", "Маған жазбаңыз", "STOP"])
def test_opt_out(client, world, db, text):
    """AT-13: opt-out flag is set; marketing messages are blocked; service replies still work."""
    r = say(client, world, text)
    conv = _conv(db, r)
    contact = db.get(Contact, conv.contact_id)
    assert contact.opted_out is True and contact.consent_flags.get("marketing") is False
    assert len(r["replies"]) == 1
    msg = messaging.send_client_message(
        db, world.tenant.id, conv.id, "Акция -20%!", author="system", kind="marketing"
    )
    db.commit()  # delivery runs after COMMIT in its own session
    db.refresh(msg)
    assert msg.provider_status == "failed" and msg.error.startswith("OPTED_OUT")


def test_opt_out_detection_negatives():
    assert not messaging.detect_opt_out("Стоп, а сколько стоит стрижка?")
    assert not messaging.detect_opt_out("Не пишу вам больше двух раз")
    assert messaging.detect_opt_out("отпишите меня от рассылки")


class _Broken:
    provider, model = "anthropic", "claude-opus-5-5"

    def run(self, req: LLMRequest, execute) -> LLMResult:
        execute("list_services", {})
        raise LLMUnavailable("APIConnectionError: connection refused")


def test_llm_failure_fallback_task_and_data_kept(client, world, db):
    """AT-10: LLM down -> inbound stored, approved fallback sent once, admin task, failed AIRun audited."""
    set_llm(_Broken())
    r = say(client, world, "Хочу записаться завтра")
    assert reply_text(r) == "Спасибо за сообщение! Администратор скоро ответит вам."
    assert r["replies"][0]["author"] == "system"
    assert db.scalar(select(Message).where(Message.direction == "in")).text == "Хочу записаться завтра"
    task = db.scalar(select(AdminTask))
    assert task.priority == "high" and task.payload["source"] == "ai_fallback"
    run = db.scalar(select(AIRun))
    assert "connection refused" in run.error and run.tool_calls[0]["rolled_back"] is True
    assert _conv(db, r).mode == "waiting_admin"
    r = say(client, world, "Алло?")  # no second fallback, no second task
    assert r["replies"] == []
    assert db.scalar(select(func.count(AdminTask.id))) == 1


class _Liar:
    provider, model = "test", "liar"

    def run(self, req: LLMRequest, execute) -> LLMResult:
        return LLMResult(text="Отлично, вы записаны на завтра!")


def test_booking_claim_without_tool_is_blocked(client, world, db):
    set_llm(_Liar())
    r = say(client, world, "Запишите на завтра")
    assert "записаны" not in reply_text(r)
    run = db.scalar(select(AIRun))
    assert run.error == "unverified_booking_claim"


def test_ai_turn_idempotent(client, world, db):
    say(client, world, "Хочу записаться на мужскую стрижку завтра в 16:00")
    r = say(client, world, "Да")
    msg_id = r["message_id"]
    messaging.run_ai_turn_now(r["conversation_id"], msg_id)  # worker retry of the same job
    assert len(_bookings(db)) == 1
    db.expire_all()
    assert db.scalar(select(func.count(Message.id)).where(Message.direction == "out")) == 2


def test_cancel_own_booking(client, world, db):
    say(client, world, "Хочу записаться на мужскую стрижку завтра в 16:00")
    say(client, world, "Да")
    r = say(client, world, "Хочу отменить запись")
    assert "Отменить запись BK-" in reply_text(r)
    r = say(client, world, "Да")
    assert "отменена" in reply_text(r)
    (b,) = _bookings(db)
    assert b.status == "cancelled"


def test_ai_disabled_stays_silent(client, world, db):
    world.tenant.settings = {"ai": {"enabled": False}}
    db.commit()
    r = say(client, world, "Здравствуйте")
    assert r["replies"] == []
