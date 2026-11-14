"""Escalation chain with explicit ACK (ТЗ §12.2-12.3): AT-04, AT-05, AT-06, quiet hours, backup, call cap, SLA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.channels import voice
from app.models import AdminTask, Call, Conversation, KnowledgeArticle, Notification, OutboxEvent, Tenant
from app.services import escalation
from app.services.notifications import send_due_notifications
from app.services.outbox import dispatch_outbox
from tests.ops_helpers import T0, SentToClient, make_conversation, reset_mocks, tg_texts


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


def _task(db, world, conv=None, at=T0, **kw) -> AdminTask:
    t = escalation.create_admin_task(
        db,
        world.tenant.id,
        question="Можно скидку 30%?",
        conversation_id=conv.id if conv else None,
        now=at,
        **kw,
    )
    db.commit()
    return t


def _tick(db, minutes: float, base=T0) -> int:
    now = base + timedelta(minutes=minutes)
    n = escalation.process_due_escalations(db, now)
    send_due_notifications(db, now)
    return n


def _calls(db, task) -> list[Call]:
    return list(db.scalars(select(Call).where(Call.admin_task_id == task.id).order_by(Call.created_at)).all())


def test_t0_alert_goes_to_primary_with_buttons(db, world):
    conv = make_conversation(db, world)
    task = _task(db, world, conv)
    assert task.number == 1 and task.status == "open"
    assert task.next_escalation_at == T0 + timedelta(minutes=5)
    assert db.get(Conversation, conv.id).mode == "waiting_admin"
    dispatch_outbox(db, T0)
    from app.channels.telegram import mock_messages

    msgs = mock_messages()
    assert [m["chat_id"] for m in msgs] == ["1001"]  # primary only at T0
    assert "ВОПРОС №1" in msgs[0]["text"] and "Нурлан" in msgs[0]["text"]
    callbacks = [
        b["callback_data"]
        for row in msgs[0]["reply_markup"]["inline_keyboard"]
        for b in row
        if "callback_data" in b
    ]
    assert callbacks == [f"task:take:{task.id}", f"task:answer:{task.id}"]
    dispatch_outbox(db, T0)  # redelivery is a no-op
    assert len(mock_messages()) == 1


def test_at04_repeat_after_5_minutes_to_primary_and_backup(db, world):
    task = _task(db, world, make_conversation(db, world))
    dispatch_outbox(db, T0)
    assert _tick(db, 4.9) == 0
    assert _tick(db, 5) == 1
    for chat in ("1001", "1002"):
        assert len([t for t in tg_texts(chat) if t.startswith("ПОВТОРНО: нет ответа 5 мин")]) == 1
    db.refresh(task)
    assert task.escalation_level == 1 and task.next_escalation_at == T0 + timedelta(minutes=10)
    assert _tick(db, 6) == 0  # nothing more until T+10
    assert not voice.MOCK_CALLS


def test_at05_call_after_10_minutes_then_backup_then_cap(db, world):
    task = _task(db, world, make_conversation(db, world))
    _tick(db, 5)
    _tick(db, 10)
    calls = _calls(db, task)
    assert len(calls) == 1
    assert calls[0].to_number == "+77010000001" and calls[0].status == "ringing"
    assert calls[0].purpose == "admin_escalation" and calls[0].provider_ref.startswith("mock-")
    twiml = voice.MOCK_CALLS[0]["twiml"]
    assert 'numDigits="1"' in twiml and "Нажмите один" in twiml and "скидк" not in twiml  # no PII spoken
    _tick(db, 13)  # call_retry_min = 3 -> backup
    calls = _calls(db, task)
    assert [c.to_number for c in calls] == ["+77010000001", "+77010000002"]
    db.refresh(task)
    assert task.escalation_level == 3 and task.next_escalation_at is None  # max_call_attempts = 2
    _tick(db, 25)
    assert len(_calls(db, task)) == 2


def test_at06_ack_at_7th_minute_cancels_call_and_repeats(db, world):
    task = _task(db, world, make_conversation(db, world))
    dispatch_outbox(db, T0)
    _tick(db, 5)
    before = db.query(Notification).count()
    escalation.ack_task(
        db, world.tenant.id, task.id, world.admin.id, "telegram", now=T0 + timedelta(minutes=7)
    )
    db.commit()
    for m in (10, 13, 20, 60):
        _tick(db, m)
    db.refresh(task)
    assert task.status == "acked" and task.assignee_user_id == world.admin.id
    assert task.ack_at == T0 + timedelta(minutes=7) and task.next_escalation_at is None
    assert _calls(db, task) == [] and not voice.MOCK_CALLS
    assert db.query(Notification).filter(Notification.kind == "admin_task").count() == before


def test_ack_cancels_ringing_call(db, world):
    task = _task(db, world)
    _tick(db, 5)
    _tick(db, 10)
    call = _calls(db, task)[0]
    escalation.ack_task(db, world.tenant.id, task.id, world.backup_admin.id, "crm")
    db.commit()
    db.refresh(call)
    assert call.status == "cancelled"
    assert voice.MOCK_CALLS[-1] == {"action": "cancel", "provider_ref": call.provider_ref}


def test_ack_is_idempotent_first_wins(db, world):
    task = _task(db, world)
    escalation.ack_task(db, world.tenant.id, task.id, world.admin.id, "telegram")
    escalation.ack_task(db, world.tenant.id, task.id, world.backup_admin.id, "crm")
    db.commit()
    db.refresh(task)
    assert task.assignee_user_id == world.admin.id
    assert db.query(OutboxEvent).filter_by(event_type="admin_task.acked").count() == 1


def test_quiet_hours_skip_calls_but_keep_telegram(db, world):
    night = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # 23:00 Almaty
    task = _task(db, world, at=night)
    _tick(db, 5, night)
    _tick(db, 10, night)
    db.refresh(task)
    assert _calls(db, task) == []
    assert task.payload["calls_skipped"] == "quiet_hours" and task.next_escalation_at is None
    assert any(t.startswith("ПОВТОРНО") for t in tg_texts())


def test_calls_disabled_by_tenant_settings(db, world):
    tenant = db.get(Tenant, world.tenant.id)
    tenant.settings = {"escalation": {"calls_enabled": False}}
    db.commit()
    task = _task(db, world)
    _tick(db, 5)
    _tick(db, 10)
    db.refresh(task)
    assert _calls(db, task) == [] and task.payload["calls_skipped"] == "calls_disabled"


def test_custom_timers_and_single_call(db, world):
    tenant = db.get(Tenant, world.tenant.id)
    tenant.settings = {"escalation": {"repeat_after_min": 2, "call_after_min": 4, "max_call_attempts": 1}}
    db.commit()
    task = _task(db, world)
    assert task.next_escalation_at == T0 + timedelta(minutes=2)
    _tick(db, 2)
    _tick(db, 4)
    _tick(db, 10)
    assert len(_calls(db, task)) == 1


def test_no_answer_status_pulls_backup_call_forward(client, db, world):
    task = _task(db, world)
    _tick(db, 5)
    _tick(db, 10)
    call = _calls(db, task)[0]
    r = client.post(f"/v1/webhooks/voice/mock/status?call_id={call.id}", data={"CallStatus": "no-answer"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Call, call.id).status == "no_answer"
    task = db.get(AdminTask, task.id)
    assert task.next_escalation_at <= datetime.now(UTC)
    escalation.process_due_escalations(db, T0 + timedelta(minutes=11))
    assert [c.to_number for c in _calls(db, task)] == ["+77010000001", "+77010000002"]


def test_ivr_digit_1_acks_task(client, db, world):
    task = _task(db, world)
    _tick(db, 5)
    _tick(db, 10)
    call = _calls(db, task)[0]
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "1"})
    assert r.status_code == 200 and "закреплена" in r.text and "<Hangup/>" in r.text
    db.expire_all()
    task = db.get(AdminTask, task.id)
    assert task.status == "acked" and task.assignee_user_id == world.admin.id
    assert db.get(Call, call.id).dtmf == "1" and db.get(Call, call.id).status == "answered"
    _tick(db, 13)
    assert len(_calls(db, task)) == 1  # no backup call after ACK


def test_ivr_digit_2_forwards_to_backup(client, db, world):
    task = _task(db, world)
    _tick(db, 5)
    _tick(db, 10)
    call = _calls(db, task)[0]
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "2"})
    assert "резервному" in r.text
    escalation.process_due_escalations(db, T0 + timedelta(minutes=10, seconds=30))
    assert [c.to_number for c in _calls(db, task)] == ["+77010000001", "+77010000002"]


def test_sla_timeout_tells_client_once(db, world, sent):
    conv = make_conversation(db, world)
    task = _task(db, world, conv)
    assert task.sla_due_at == T0 + timedelta(minutes=30)
    escalation.process_due_escalations(db, T0 + timedelta(minutes=30))
    escalation.process_due_escalations(db, T0 + timedelta(minutes=31))
    assert len(sent.calls) == 1 and "передан специалисту" in sent.calls[0]["text"]
    db.refresh(task)
    assert task.sla_due_at is None and "sla_notified_at" in task.payload


def test_sla_skipped_message_when_human_took_over(db, world, sent):
    conv = make_conversation(db, world)
    _task(db, world, conv)
    db.get(Conversation, conv.id).mode = "human"
    db.commit()
    escalation.process_due_escalations(db, T0 + timedelta(minutes=30))
    assert sent.calls == []


def test_resolve_relays_answer_restores_ai_and_drafts_knowledge(db, world, sent):
    conv = make_conversation(db, world)
    task = _task(db, world, conv)
    escalation.resolve_task(
        db,
        world.tenant.id,
        task.id,
        world.admin.id,
        "Скидка 10% только по вторникам",
        relay_to_client=True,
        add_to_knowledge=True,
    )
    db.commit()
    db.refresh(task)
    assert task.status == "resolved" and task.ack_at is not None and task.answer.startswith("Скидка")
    assert sent.calls[0]["text"] == "Администратор уточнил: Скидка 10% только по вторникам"
    assert sent.calls[0]["author"] == "human" and sent.calls[0]["author_user_id"] == world.admin.id
    assert db.get(Conversation, conv.id).mode == "ai"
    art = db.scalar(select(KnowledgeArticle))
    assert art.status == "draft" and art.content.startswith("Скидка")  # owner must publish (ТЗ §4.3)
    with pytest.raises(Exception) as exc:
        escalation.resolve_task(db, world.tenant.id, task.id, world.admin.id, "again", True, False)
    assert exc.value.status_code == 409
    db.rollback()
    assert len(sent.calls) == 1


def test_admin_tasks_api(client, db, world, sent):
    conv = make_conversation(db, world)
    h = world.headers(world.admin)
    r = client.post(
        "/v1/admin-tasks",
        headers=h,
        json={"question": "Перезвонить клиенту", "kind": "callback", "conversation_id": str(conv.id)},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = client.get("/v1/admin-tasks?status=open", headers=h)
    assert r.json()["total"] == 1 and r.json()["items"][0]["contact"]["name"] == "Нурлан"
    r = client.post(f"/v1/admin-tasks/{tid}/ack", headers=h)
    assert r.json()["status"] == "acked" and r.json()["assignee"]["id"] == str(world.admin.id)
    r = client.post(
        f"/v1/admin-tasks/{tid}/resolve",
        headers=h,
        json={"answer": "Перезвонили", "relay_to_client": False, "add_to_knowledge": False},
    )
    assert r.json()["status"] == "resolved"
    assert client.get("/v1/admin-tasks", headers=world.headers(world.staff_user)).status_code == 403
    assert client.get("/v1/admin-tasks", headers=world.headers(world.analyst)).status_code == 403


def test_tasks_are_tenant_isolated(client, db, world, other_world):
    task = _task(db, world)
    r = client.post(f"/v1/admin-tasks/{task.id}/ack", headers=other_world.headers(other_world.owner))
    assert r.status_code == 404
