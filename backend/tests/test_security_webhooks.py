"""Webhook security (ТЗ §20.1, §22.1): forged / missing signatures, replay, cross-tenant forging, Twilio."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.channels import telegram
from app.channels.voice import twilio_signature
from app.config import get_settings
from app.models import AdminTask, AuditLog, Call, Message, ShiftRequest
from app.security import sign_payload, verify_crm_webhook
from app.services import escalation, webhooks_out
from app.services.integrations import telegram_webhook_secret
from tests.ai_helpers import post_webhook, wa_payload


@pytest.fixture(autouse=True)
def _tg_mock():
    telegram.reset_mock()
    yield
    telegram.reset_mock()


def _messages(db, tenant_id) -> int:
    return db.scalar(select(func.count(Message.id)).where(Message.tenant_id == tenant_id))


# ---------------------------------------------------------------- WhatsApp
def test_whatsapp_missing_or_forged_signature(client, db, tenant_a):
    tid = tenant_a.world.tenant.id
    before = _messages(db, tid)
    payload = wa_payload(pnid="PNID-alpha", text="forged")
    body = json.dumps(payload).encode()
    cases = [
        {},  # missing
        {"X-Hub-Signature-256": "sha256=" + "0" * 64},
        {"X-Hub-Signature-256": "sha256=" + sign_payload("attacker-secret", body)},
        {
            "X-Hub-Signature-256": sign_payload(get_settings().whatsapp_app_secret, body)
        },  # no "sha256=" prefix
        {"X-Hub-Signature-256": "sha256=" + sign_payload(get_settings().whatsapp_app_secret, body + b" ")},
    ]
    for headers in cases:
        r = client.post(
            "/v1/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json", **headers}
        )
        assert r.status_code == 401, (headers, r.text)
    assert _messages(db, tid) == before


def test_whatsapp_empty_platform_secret_never_verifies(client, db, tenant_a, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", "")
    r = post_webhook(client, wa_payload(pnid="PNID-alpha", text="x"), secret="")
    assert r.status_code == 401


def test_whatsapp_replayed_payload_creates_no_duplicates(client, db, tenant_a):
    tid = tenant_a.world.tenant.id
    payload = wa_payload(pnid="PNID-alpha", wa_id="77015550777", text="Здравствуйте", mid="wamid.replay-1")
    first = post_webhook(client, payload)
    assert first.status_code == 200 and first.json()["accepted"] == 1
    count = _messages(db, tid)
    for _ in range(3):
        again = post_webhook(client, payload)
        assert again.status_code == 200 and again.json()["accepted"] == 0
    assert _messages(db, tid) == count
    assert (
        db.scalar(select(func.count(Message.id)).where(Message.provider_message_id == "wamid.replay-1")) == 1
    )


def test_whatsapp_tenant_secret_cannot_sign_for_another_tenant(client, db, tenant_a, tenant_b, monkeypatch):
    """A tenant knows its own per-tenant app_secret; it must not be able to inject messages into another
    tenant's number by bundling that number into a payload it signs."""
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", "platform-secret-unknown-to-tenants")
    hb = tenant_b.world.headers()
    assert (
        client.put(
            "/v1/integrations/whatsapp", headers=hb, json={"secrets": {"app_secret": "bravo-app-secret"}}
        ).status_code
        == 200
    )
    a_tid = tenant_a.world.tenant.id
    before = _messages(db, a_tid)
    own = wa_payload(pnid="PNID-bravo", text="legit", mid=f"wamid.{uuid.uuid4().hex}")
    victim = wa_payload(
        pnid="PNID-alpha", text="Ваша запись отменена, оплатите по ссылке", mid=f"wamid.{uuid.uuid4().hex}"
    )
    bundled = {**own, "entry": own["entry"] + victim["entry"]}
    for payload in (victim, bundled):
        r = post_webhook(client, payload, secret="bravo-app-secret")
        assert r.status_code == 401, r.text
    assert _messages(db, a_tid) == before
    # the tenant's own number still works with its own secret
    assert post_webhook(client, own, secret="bravo-app-secret").status_code == 200


def test_whatsapp_verify_handshake_requires_token(client):
    params = {"hub.mode": "subscribe", "hub.challenge": "42"}
    assert (
        client.get("/v1/webhooks/whatsapp", params={**params, "hub.verify_token": "wrong"}).status_code == 403
    )
    assert client.get("/v1/webhooks/whatsapp", params=params).status_code == 403
    ok = client.get(
        "/v1/webhooks/whatsapp", params={**params, "hub.verify_token": get_settings().whatsapp_verify_token}
    )
    assert ok.status_code == 200 and ok.text == "42"


# ---------------------------------------------------------------- Telegram
def _tg(client, tenant_id, update, secret):
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return client.post(f"/v1/webhooks/telegram/{tenant_id}", json=update, headers=headers)


def _callback(chat_id, data):
    return {
        "update_id": 7,
        "callback_query": {"id": "cb", "from": {"id": chat_id}, "data": data, "message": {}},
    }


def test_telegram_wrong_or_missing_secret_rejected(client, db, tenant_a, tenant_b):
    a = tenant_a.world
    task_id = tenant_a.one("task")
    upd = _callback(a.admin.telegram_chat_id, f"task:take:{task_id}")
    for secret in (
        None,
        "",
        "wrong",
        telegram_webhook_secret(tenant_b.world.tenant.id),
        get_settings().telegram_webhook_secret,
    ):
        assert _tg(client, a.tenant.id, upd, secret).status_code == 401, secret
    assert db.get(AdminTask, uuid.UUID(task_id)).status == "open"


def test_telegram_callback_from_foreign_chat_is_rejected(client, db, tenant_a, tenant_b):
    a, b = tenant_a.world, tenant_b.world
    task_id = uuid.UUID(tenant_a.one("task"))
    secret = telegram_webhook_secret(a.tenant.id)
    for chat in (b.admin.telegram_chat_id, "999999", a.staff_user.telegram_chat_id or "424242"):
        r = _tg(client, a.tenant.id, _callback(chat, f"task:take:{task_id}"), secret)
        assert r.status_code == 200  # Telegram must not retry-storm; the action itself is refused
    db.expire_all()
    assert db.get(AdminTask, task_id).status == "open"
    denied = db.scalars(
        select(AuditLog).where(AuditLog.tenant_id == a.tenant.id, AuditLog.action == "access.denied")
    ).all()
    assert {d.actor_id for d in denied} >= {b.admin.telegram_chat_id, "999999"}
    assert any(
        m.get("text") == "Нет доступа" for m in telegram.MOCK_SENT if m["method"] == "answerCallbackQuery"
    )


def test_telegram_admin_cannot_act_on_other_tenants_objects(client, db, tenant_a, tenant_b):
    a = tenant_a.world
    secret = telegram_webhook_secret(a.tenant.id)
    b_task = uuid.UUID(tenant_b.one("task"))
    b_req = uuid.UUID(tenant_b.one("shift_request"))
    _tg(client, a.tenant.id, _callback(a.admin.telegram_chat_id, f"task:take:{b_task}"), secret)
    _tg(client, a.tenant.id, _callback(a.admin.telegram_chat_id, f"shift:approve:{b_req}"), secret)
    db.expire_all()
    assert db.get(AdminTask, b_task).status == "open"
    assert db.get(ShiftRequest, b_req).status == "pending"


# ---------------------------------------------------------------- Twilio voice callbacks
@pytest.fixture
def twilio_call(db, tenant_a, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_provider", "twilio")
    task = escalation.create_admin_task(db, tenant_a.world.tenant.id, question="call me")
    call = Call(
        tenant_id=tenant_a.world.tenant.id,
        direction="out",
        purpose="admin_escalation",
        to_number="+77010000001",
        admin_task_id=task.id,
        status="ringing",
        provider_ref="CA123",
    )
    db.add(call)
    db.commit()
    return call


def _twilio_post(client, call, form, signature):
    path = f"/v1/webhooks/voice/twilio/status?call_id={call.id}"
    headers = {"X-Twilio-Signature": signature} if signature else {}
    return client.post(path, data=form, headers=headers), get_settings().public_base_url.rstrip("/") + path


def test_twilio_signature_required_when_token_configured(client, db, twilio_call, monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "twilio-auth-token-123")
    form = {"CallSid": "CA123", "CallStatus": "completed"}
    r, url = _twilio_post(client, twilio_call, form, None)
    assert r.status_code == 403
    r, _ = _twilio_post(client, twilio_call, form, twilio_signature("wrong-token", url, form))
    assert r.status_code == 403
    r, _ = _twilio_post(
        client,
        twilio_call,
        form,
        twilio_signature("twilio-auth-token-123", url, {**form, "CallStatus": "busy"}),
    )
    assert r.status_code == 403  # signature over different parameters
    db.expire_all()
    assert db.get(Call, twilio_call.id).status == "ringing"
    r, _ = _twilio_post(client, twilio_call, form, twilio_signature("twilio-auth-token-123", url, form))
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Call, twilio_call.id).status == "completed"
    # a valid signature for a different call sid is refused too
    other = {"CallSid": "CA999", "CallStatus": "completed"}
    r, _ = _twilio_post(client, twilio_call, other, twilio_signature("twilio-auth-token-123", url, other))
    assert r.status_code == 403


def test_twilio_callbacks_fail_closed_without_token(client, db, twilio_call, monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_auth_token", None)
    r, _ = _twilio_post(client, twilio_call, {"CallSid": "CA123", "CallStatus": "completed"}, None)
    assert r.status_code == 403


def test_mock_voice_provider_disabled_outside_mock_mode(client, db, twilio_call):
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={twilio_call.id}", data={"Digits": "1"})
    assert r.status_code == 404
    assert client.post(f"/v1/webhooks/voice/twilio/status?call_id={uuid.uuid4()}", data={}).status_code == 404


# ---------------------------------------------------------------- outbound webhook signatures (receiver helper)
def test_outbound_signature_and_timestamp_verification():
    secret, body, ts = "whsec-123", b'{"type":"booking.created"}', int(time.time())
    sig = webhooks_out.sign(secret, ts, body)
    assert verify_crm_webhook(secret, body, sig, str(ts))
    assert not verify_crm_webhook(secret, body + b" ", sig, str(ts))  # tampered body
    assert not verify_crm_webhook("other", body, sig, str(ts))  # wrong secret
    assert not verify_crm_webhook(secret, body, sig, str(ts + 1))  # signature binds the timestamp
    old = ts - 3600
    assert not verify_crm_webhook(
        secret, body, webhooks_out.sign(secret, old, body), str(old)
    )  # replay window
    assert not verify_crm_webhook(secret, body, None, str(ts))
    assert not verify_crm_webhook(secret, body, sig, "not-a-number")


def test_post_signed_headers_verify(monkeypatch):
    captured = {}

    def fake_post(url, content, headers, **kw):
        captured.update(content=content, headers=headers)

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(webhooks_out.httpx, "post", fake_post)
    webhooks_out.post_signed("https://example.com/h", "s3cret", {"a": 1}, event_type="ping", delivery_id="d1")
    h = captured["headers"]
    assert verify_crm_webhook("s3cret", captured["content"], h["X-CRM-Signature"], h["X-CRM-Timestamp"])
    assert h["X-CRM-Delivery"] == "d1" and h["X-CRM-Event"] == "ping"
