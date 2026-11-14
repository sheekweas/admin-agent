"""Telegram webhook: secret token, buttons (take/answer/approve/reject), reply = answer, permission checks."""

from __future__ import annotations

import pytest

from app.channels import telegram
from app.models import AdminTask, AuditLog, Notification
from app.services import escalation
from app.services.integrations import telegram_webhook_secret
from app.services.outbox import dispatch_outbox
from tests.ops_helpers import T0, SentToClient, make_conversation, reset_mocks


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


def _post(client, world, update, secret=None):
    return client.post(
        f"/v1/webhooks/telegram/{world.tenant.id}",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": secret or telegram_webhook_secret(world.tenant.id)},
    )


def _cb(chat_id, data, cb_id="cb1", message_id=None):
    msg = {"message_id": message_id, "chat": {"id": chat_id}, "text": "orig"} if message_id else {}
    return {
        "update_id": 1,
        "callback_query": {"id": cb_id, "from": {"id": chat_id}, "data": data, "message": msg},
    }


def _task(db, world, conv=None) -> AdminTask:
    t = escalation.create_admin_task(
        db, world.tenant.id, question="Есть парковка?", conversation_id=conv.id if conv else None, now=T0
    )
    db.commit()
    return t


def test_rejects_bad_secret(client, world, other_world):
    assert _post(client, world, {"update_id": 1}, secret="wrong").status_code == 401
    # another tenant's secret is not valid for this tenant
    assert (
        _post(
            client, world, {"update_id": 1}, secret=telegram_webhook_secret(other_world.tenant.id)
        ).status_code
        == 401
    )


def test_take_button_acks_task(client, db, world):
    task = _task(db, world)
    r = _post(client, world, _cb(1001, f"task:take:{task.id}"))
    assert r.status_code == 200
    db.expire_all()
    task = db.get(AdminTask, task.id)
    assert task.status == "acked" and task.assignee_user_id == world.admin.id
    answers = telegram.mock_messages(method="answerCallbackQuery")
    assert answers and "№1" in answers[0]["text"]
    # second press by the backup admin: idempotent, tells who owns it
    _post(client, world, _cb(1002, f"task:take:{task.id}", cb_id="cb2"))
    db.expire_all()
    assert db.get(AdminTask, task.id).assignee_user_id == world.admin.id
    assert "Уже взята" in telegram.mock_messages(method="answerCallbackQuery")[-1]["text"]


def test_unknown_chat_is_denied_and_audited(client, db, world):
    task = _task(db, world)
    _post(client, world, _cb(4242, f"task:take:{task.id}"))
    db.expire_all()
    assert db.get(AdminTask, task.id).status == "open"
    assert db.query(AuditLog).filter_by(action="access.denied", actor_id="4242").count() == 1


def test_staff_chat_cannot_take(client, db, world):
    world.staff_user.telegram_chat_id = "3003"
    db.commit()
    task = _task(db, world)
    _post(client, world, _cb(3003, f"task:take:{task.id}"))
    db.expire_all()
    assert db.get(AdminTask, task.id).status == "open"


def test_reply_to_alert_resolves_and_relays(client, db, world, sent):
    conv = make_conversation(db, world)
    task = _task(db, world, conv)
    dispatch_outbox(db, T0)
    alert = db.query(Notification).filter_by(recipient="1001", kind="admin_task").one()
    update = {
        "update_id": 5,
        "message": {
            "message_id": 77,
            "chat": {"id": 1001},
            "text": "Да, бесплатная",
            "reply_to_message": {"message_id": int(alert.provider_ref)},
        },
    }
    assert _post(client, world, update).status_code == 200
    db.expire_all()
    task = db.get(AdminTask, task.id)
    assert task.status == "resolved" and task.answer == "Да, бесплатная"
    assert sent.calls[0]["text"] == "Администратор уточнил: Да, бесплатная"
    assert "Ответ отправлен клиенту" in telegram.mock_messages("1001")[-1]["text"]


def test_answer_button_sends_force_reply_prompt(client, db, world, sent):
    conv = make_conversation(db, world)
    task = _task(db, world, conv)
    _post(client, world, _cb(1002, f"task:answer:{task.id}"))
    prompt = telegram.mock_messages("1002")[-1]
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    n = db.query(Notification).filter_by(kind="answer_prompt").one()
    _post(
        client,
        world,
        {
            "update_id": 9,
            "message": {
                "message_id": 1,
                "chat": {"id": 1002},
                "text": "Ответ",
                "reply_to_message": {"message_id": int(n.provider_ref)},
            },
        },
    )
    db.expire_all()
    assert db.get(AdminTask, task.id).status == "resolved"


def test_shift_buttons_call_schedule_service(client, db, world, monkeypatch):
    import uuid

    decided = []

    def fake_decide(db, tenant_id, request_id, *, approve, comment, actor_id):
        decided.append((tenant_id, request_id, approve, actor_id))

    monkeypatch.setattr("app.services.schedule.decide_shift_request", fake_decide)
    rid = uuid.uuid4()
    _post(client, world, _cb(1001, f"shift:approve:{rid}", message_id=10))
    _post(client, world, _cb(4242, f"shift:reject:{rid}", message_id=10))  # not an admin
    assert decided == [(world.tenant.id, rid, True, world.admin.id)]
    edits = telegram.mock_messages(method="editMessageText")
    assert edits and "Подтверждено" in edits[0]["text"]


def test_start_command_returns_chat_id(client, world):
    _post(
        client, world, {"update_id": 3, "message": {"message_id": 1, "chat": {"id": 5555}, "text": "/start"}}
    )
    assert "5555" in telegram.mock_messages("5555")[0]["text"]
