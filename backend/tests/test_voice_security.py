"""Voice channel security & infrastructure: X-Twilio-Signature on the new webhooks (fail closed), provider
abstraction (Twilio REST payloads, WhatsApp Calling stub), KK admin IVR (§26.4, AT-18), transcript retention."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.channels import voice
from app.channels.voice import (
    MockVoice,
    TwilioVoice,
    VoiceNotConfigured,
    WhatsAppCallingProvider,
    get_voice_provider,
    twilio_signature,
)
from app.config import get_settings
from app.models import AdminTask, Call, Conversation, Message
from app.services import escalation
from app.services.voice_calls import is_goodbye, speakable, wants_human
from app.workers.tasks import run_voice_retention
from tests.ops_helpers import T0
from tests.voice_helpers import VOICE_NUMBER, add_voice_integration, action_path, call_by_sid, set_settings

TOKEN = "twilio-tenant-token-123"


@pytest.fixture(autouse=True)
def _reset():
    voice.MOCK_CALLS.clear()
    yield
    voice.MOCK_CALLS.clear()


@pytest.fixture
def twilio(db, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_provider", "twilio")
    monkeypatch.setattr(get_settings(), "twilio_auth_token", None)
    add_voice_integration(
        db, world.tenant, secrets={"account_sid": "AC1", "auth_token": TOKEN, "from_number": VOICE_NUMBER}
    )
    return world


def _signed_post(client, path, form, token=TOKEN, signature=None):
    url = get_settings().public_base_url.rstrip("/") + path
    sig = signature if signature is not None else twilio_signature(token, url, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": sig} if sig else {})


# ---------------------------------------------------------------- signatures
def test_inbound_and_turn_require_valid_twilio_signature(client, db, twilio):
    form = {"CallSid": "CA100", "From": "+77015550001", "To": VOICE_NUMBER}
    path = "/v1/webhooks/voice/twilio/inbound"
    assert _signed_post(client, path, form, signature="").status_code == 403
    assert _signed_post(client, path, form, token="wrong").status_code == 403
    url = get_settings().public_base_url.rstrip("/") + path
    sig_for_other = twilio_signature(TOKEN, url, {**form, "From": "+77019999999"})
    assert _signed_post(client, path, form, signature=sig_for_other).status_code == 403  # signed other params
    assert db.scalar(select(Call)) is None  # nothing stored for rejected requests

    r = _signed_post(client, path, form)
    assert r.status_code == 200 and "<Gather" in r.text
    turn_path = action_path(r.text)
    assert "/twilio/turn?" in turn_path
    speech = {"CallSid": "CA100", "SpeechResult": "Сколько стоит борода?", "Confidence": "0.9"}
    assert _signed_post(client, turn_path, speech, signature="").status_code == 403
    assert _signed_post(client, turn_path, {**speech, "CallSid": "CA999"}).status_code == 403  # other call
    r = _signed_post(client, turn_path, speech)
    assert r.status_code == 200 and "3000" in r.text
    call = call_by_sid(db, "CA100")
    for kind, data in (
        ("dial", {"CallSid": "CA100", "DialCallStatus": "completed"}),
        ("recording", {"CallSid": "CA100", "RecordingUrl": "https://x/r", "RecordingStatus": "completed"}),
        ("answer", {"CallSid": "CA100"}),
        ("status", {"CallSid": "CA100", "CallStatus": "completed"}),
    ):
        assert (
            _signed_post(
                client, f"/v1/webhooks/voice/twilio/{kind}?call_id={call.id}", data, signature=""
            ).status_code
            == 403
        )


def test_twilio_voice_webhooks_fail_closed_without_token(client, db, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_provider", "twilio")
    monkeypatch.setattr(get_settings(), "twilio_auth_token", None)
    add_voice_integration(db, world.tenant)  # number configured, but no auth token anywhere
    form = {"CallSid": "CA1", "From": "+77015550001", "To": VOICE_NUMBER}
    assert (
        _signed_post(client, "/v1/webhooks/voice/twilio/inbound", form, token="anything").status_code == 403
    )
    # the mock provider is disabled outside mock mode
    assert client.post("/v1/webhooks/voice/mock/inbound", data=form).status_code == 404
    assert client.post("/v1/webhooks/voice/sip/inbound", data=form).status_code == 404


def test_voice_webhooks_are_rate_limited(client, db, world, monkeypatch):
    from app.middleware import reset_rate_limits

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_backend", "memory")
    monkeypatch.setattr(get_settings(), "rate_limit_webhook_per_minute", 3)
    reset_rate_limits()
    try:
        codes = [
            client.post("/v1/webhooks/voice/mock/inbound", data={"To": "+70000000000"}).status_code
            for _ in range(5)
        ]
    finally:
        reset_rate_limits()
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]


# ---------------------------------------------------------------- providers
def test_twilio_rest_payloads(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def fake_post(url, data=None, auth=None, timeout=None):
        sent.append((url, data))
        return httpx.Response(201, json={"sid": "CA42"})

    monkeypatch.setattr(httpx, "post", fake_post)
    tw = TwilioVoice("AC1", "tok", "+77270000001")
    placed = tw.place_call("+77015550001", call_id="c1", answer_url="https://crm.example/answer?call_id=c1")
    url, data = sent[-1]
    assert url.endswith("/Accounts/AC1/Calls.json") and placed.provider_ref == "CA42"
    assert data["Url"] == "https://crm.example/answer?call_id=c1" and "Twiml" not in data
    assert data["MachineDetection"] == "Enable" and "/status?call_id=c1" in data["StatusCallback"]
    tw.start_recording("CA42", call_id="c1")
    assert sent[-1][0].endswith("/Calls/CA42/Recordings.json")
    assert "/recording?call_id=c1" in sent[-1][1]["RecordingStatusCallback"]
    tw.hangup("CA42")
    assert sent[-1][1] == {"Status": "completed"}
    tw.place_call("+77010000001", call_id="c2")  # admin IVR keeps inline TwiML
    assert "numDigits" in sent[-1][1]["Twiml"]


def test_whatsapp_calling_is_a_documented_stub(db, world, monkeypatch):
    with pytest.raises(VoiceNotConfigured, match="WHATSAPP_CALLING_ENABLED"):
        get_voice_provider(db, world.tenant.id, channel="whatsapp")
    monkeypatch.setattr(get_settings(), "whatsapp_calling_enabled", True)
    provider = get_voice_provider(db, world.tenant.id, channel="whatsapp")
    assert isinstance(provider, WhatsAppCallingProvider)
    for fn in (
        lambda: provider.place_call("+7701", call_id="x"),
        provider.response,
        lambda: provider.hangup("x"),
    ):
        with pytest.raises(VoiceNotConfigured, match="Meta approval"):
            fn()
    assert isinstance(get_voice_provider(db, world.tenant.id), MockVoice)


def test_voice_not_configured_for_twilio_without_credentials(client, db, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_provider", "twilio")
    for k in ("twilio_account_sid", "twilio_auth_token", "twilio_from_number"):
        monkeypatch.setattr(get_settings(), k, None)
    monkeypatch.setattr("app.services.voice_calls.utcnow", lambda: T0)
    world.contact.consent_flags = {"processing": True, "outbound_calls": True}
    db.commit()
    r = client.post(
        "/v1/calls/outbound",
        json={"contact_id": str(world.contact.id), "purpose": "callback"},
        headers=world.headers(),
    )
    assert r.status_code == 503 and r.json()["detail"]["code"] == "VOICE_NOT_CONFIGURED"


def test_speech_helpers():
    assert speakable(
        "Готово! Вы записаны: «Стрижка», 17.11.2026 в 16:00. Номер брони: BK-000123. 5000 ₸", "ru"
    ) == ("Готово! Вы записаны: Стрижка, 17 ноября в 16:00. Номер брони: 123. 5000 тенге")
    assert speakable("Бронь №BK-000007, 01.01.2027", "kk") == "Бронь нөмірі 7, 1 қаңтар"
    assert speakable("см. https://example.com/x **важно**", "ru") == "см. важно"
    assert wants_human("Позовите оператора") and wants_human("адам") and wants_human("живой человек")
    assert not wants_human("Ер адамның шаш қиюы") and not wants_human(
        "Запишите на двух человек завтра в 16:00"
    )
    assert is_goodbye("Спасибо, до свидания") and not is_goodbye(
        "До свидания не прощаемся, запишите меня завтра на стрижку в 16"
    )


# ---------------------------------------------------------------- admin escalation IVR in Kazakh (§26.4, AT-18)
def test_admin_escalation_ivr_in_kazakh_for_kk_admin(client, db, world):
    world.admin.ui_language = "kk"
    db.commit()
    task = escalation.create_admin_task(db, world.tenant.id, question="Сұрақ", now=T0)
    db.commit()
    escalation.process_due_escalations(db, T0 + timedelta(minutes=5))
    escalation.process_due_escalations(db, T0 + timedelta(minutes=10))
    call = db.scalar(select(Call).where(Call.admin_task_id == task.id))
    assert call.language == "kk" and call.to_number == "+77010000001"
    twiml = voice.MOCK_CALLS[0]["twiml"]
    assert "бірді басыңыз" in twiml and "екіні басыңыз" in twiml and 'numDigits="1"' in twiml
    assert "Сұрақ" not in twiml and "Нажмите" not in twiml  # no client data, no Russian
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "9"})
    assert "бірді басыңыз" in r.text  # invalid digit -> asked again in Kazakh
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "1"})
    assert "бекітілді" in r.text and "<Hangup/>" in r.text
    db.expire_all()
    assert db.get(AdminTask, task.id).status == "acked"
    # the backup admin (ui_language=ru) still gets Russian
    task2 = escalation.create_admin_task(db, world.tenant.id, question="Q2", now=T0, priority="high")
    db.commit()
    voice.MOCK_CALLS.clear()
    escalation.place_escalation_call(db, task2, world.backup_admin, T0)
    assert "Нажмите один" in voice.MOCK_CALLS[0]["twiml"]


def test_kk_ivr_forward_phrase(client, db, world):
    world.admin.ui_language = "kk"
    db.commit()
    task = escalation.create_admin_task(db, world.tenant.id, question="Q", now=T0)
    call = escalation.place_escalation_call(db, task, world.admin, T0)
    db.commit()
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "2"})
    assert "резервтегі әкімшіге" in r.text


# ---------------------------------------------------------------- retention (§20.1)
def test_transcript_retention_cleanup(client, db, world):
    add_voice_integration(db, world.tenant)
    set_settings(db, world.tenant, voice={"transcript_retention_days": 30, "recording_enabled": True})
    d = client.post(
        "/v1/calls/simulate",
        headers=world.headers(),
        json={"phone": "+77015550011", "utterances": ["Сколько стоит борода?"]},
    ).json()
    old = db.get(Call, d["id"])
    old.recording_url = "https://api.twilio.com/rec/RE1"
    db.commit()
    fresh = client.post(
        "/v1/calls/simulate",
        headers=world.headers(),
        json={"phone": "+77015550012", "utterances": ["Здравствуйте"]},
    ).json()
    stale = Call(
        tenant_id=world.tenant.id,
        direction="in",
        purpose="client_ai",
        to_number=VOICE_NUMBER,
        status="in_progress",
        started_at=T0,
        created_at=T0,
    )
    db.add(stale)
    db.commit()

    now = old.created_at + timedelta(days=31)
    fresh_row = db.get(Call, fresh["id"])
    fresh_row.created_at = now - timedelta(days=1)
    db.commit()
    assert run_voice_retention(now=now) >= 1
    db.expire_all()
    old = db.get(Call, d["id"])
    assert old.transcript == "" and old.transcript_items == [] and old.recording_url is None
    assert old.metrics["turns"] == 1  # metrics survive for reports
    texts = db.scalars(select(Message.text).where(Message.conversation_id == old.conversation_id)).all()
    assert texts and all(t == "" for t in texts)
    assert db.get(Call, fresh["id"]).transcript_items  # within retention: kept
    assert db.get(Call, stale.id).status == "completed" and db.get(Call, stale.id).ended_at is not None
    r = client.get(f"/v1/calls/{d['id']}", headers=world.headers()).json()
    assert r["transcript"] == [] and r["recording_available"] is False
    assert db.get(Conversation, old.conversation_id) is not None


def test_retention_disabled_with_zero_days(client, db, world):
    add_voice_integration(db, world.tenant)
    set_settings(db, world.tenant, voice={"transcript_retention_days": 0})
    d = client.post(
        "/v1/calls/simulate",
        headers=world.headers(),
        json={"phone": "+77015550011", "utterances": ["Здравствуйте"]},
    ).json()
    run_voice_retention(now=db.get(Call, d["id"]).created_at + timedelta(days=3650))
    db.expire_all()
    assert db.get(Call, d["id"]).transcript_items
