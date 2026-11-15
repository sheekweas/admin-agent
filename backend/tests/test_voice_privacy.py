"""v1.1 privacy fixes: non-consented Twilio recordings are deleted at the provider, and the transcript-retention
job redacts old ai_runs content (ТЗ §20.1)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.channels import voice
from app.channels.voice import MockVoice, TwilioVoice, VoiceError
from app.models import AIRun, AuditLog, Contact, Conversation
from app.services.common import utcnow
from app.services.voice_calls import redact_expired_ai_runs
from app.workers.tasks import run_voice_retention
from tests.voice_helpers import add_voice_integration, call_by_sid, inbound, set_settings

SID = "RE" + "0123456789abcdef" * 2


@pytest.fixture(autouse=True)
def _voice(db, world):
    add_voice_integration(db, world.tenant)
    voice.MOCK_CALLS.clear()
    yield
    voice.MOCK_CALLS.clear()


def _recording(client, call, sid=SID, url="https://api.twilio.com/rec/" + SID):
    r = client.post(
        f"/v1/webhooks/voice/mock/recording?call_id={call.id}",
        data={"RecordingSid": sid, "RecordingStatus": "completed", "RecordingUrl": url},
    )
    assert r.status_code == 200, r.text


def _deleted() -> list[str]:
    return [c["recording_sid"] for c in voice.MOCK_CALLS if c["action"] == "delete_recording"]


# ---------------------------------------------------------------- recording deletion
def test_recording_of_client_who_refused_is_deleted_at_provider(client, db, world):
    set_settings(db, world.tenant, voice={"recording_enabled": True})
    world.contact.consent_flags = {"processing": True, "call_recording": False}
    db.commit()
    _, sid = inbound(client, phone=world.contact.phone_e164)
    call = call_by_sid(db, sid)
    _recording(client, call)
    call = call_by_sid(db, sid)
    assert call.recording_url is None and _deleted() == [SID]
    assert call.meta["recording_deleted"] == SID
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "call.recording_deleted"))
    assert audit is not None and audit.entity_id == str(call.id) and audit.payload["reason"] == "no_consent"


def test_recording_while_recording_disabled_is_deleted(client, db, world):
    _, sid = inbound(client)  # recording_enabled is False by default
    _recording(client, call_by_sid(db, sid))
    assert call_by_sid(db, sid).recording_url is None and _deleted() == [SID]


def test_recording_disabled_mid_call_is_deleted(client, db, world):
    set_settings(db, world.tenant, voice={"recording_enabled": True})
    _, sid = inbound(client)
    assert call_by_sid(db, sid).meta["recording"] is True
    set_settings(db, world.tenant, voice={"recording_enabled": False})
    _recording(client, call_by_sid(db, sid))
    assert call_by_sid(db, sid).recording_url is None and _deleted() == [SID]


def test_consented_recording_is_kept(client, db, world):
    set_settings(db, world.tenant, voice={"recording_enabled": True})
    _, sid = inbound(client)
    _recording(client, call_by_sid(db, sid))
    assert call_by_sid(db, sid).recording_url.endswith(SID) and _deleted() == []


def test_recording_without_sid_is_only_ignored(client, db, world):
    _, sid = inbound(client)
    call = call_by_sid(db, sid)
    r = client.post(
        f"/v1/webhooks/voice/mock/recording?call_id={call.id}",
        data={"RecordingStatus": "completed", "RecordingUrl": "https://x/r"},
    )
    assert r.status_code == 200 and call_by_sid(db, sid).recording_url is None and _deleted() == []


def test_failed_deletion_is_flagged_and_url_never_stored(client, db, world, monkeypatch):
    def boom(self, recording_sid):
        raise VoiceError("Twilio HTTP 500")

    monkeypatch.setattr(MockVoice, "delete_recording", boom)
    _, sid = inbound(client)
    _recording(client, call_by_sid(db, sid))
    call = call_by_sid(db, sid)
    assert call.recording_url is None and call.meta["recording_delete_failed"] == SID


def test_twilio_delete_recording_rest(monkeypatch):
    sent = []

    def fake_delete(url, auth=None, timeout=None):
        sent.append((url, auth))
        return httpx.Response(sent_status.pop(0))

    sent_status = [204, 404, 500]
    monkeypatch.setattr(httpx, "delete", fake_delete)
    tw = TwilioVoice("AC1", "tok", "+77270000001")
    tw.delete_recording(SID)
    assert sent[-1] == (
        f"https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/{SID}.json",
        ("AC1", "tok"),
    )
    tw.delete_recording(SID)  # already gone -> fine
    with pytest.raises(VoiceError, match="HTTP 500"):
        tw.delete_recording(SID)
    with pytest.raises(VoiceError, match="invalid recording sid"):
        tw.delete_recording("../Calls/CA1")
    assert len(sent) == 3


# ---------------------------------------------------------------- ai_runs redaction
def _run(db, world, conv, *, age_days: int, tool_calls=None, reply="Записал вас на 15:00, Айдар") -> AIRun:
    run = AIRun(
        tenant_id=world.tenant.id,
        conversation_id=conv.id,
        provider="mock",
        model="mock-1",
        prompt_version="v1",
        knowledge_article_ids=[],
        tool_calls=tool_calls
        if tool_calls is not None
        else [
            {
                "name": "create_booking",
                "input": {"client_name": "Айдар", "phone": "+77015550001"},
                "ok": True,
                "error": None,
                "ms": 12,
                "result": {"booking_number": 7, "time": "15:00"},
            },
            {"name": "add_tag", "input": {"tag": "x"}, "ok": False, "error": "BAD_TAG", "ms": 1},
        ],
        reply_text=reply,
        input_tokens=321,
        output_tokens=45,
        latency_ms=870,
        created_at=utcnow() - timedelta(days=age_days),
    )
    db.add(run)
    db.commit()
    return run


def _conv(db, world) -> Conversation:
    conv = Conversation(
        tenant_id=world.tenant.id,
        contact_id=world.contact.id,
        channel="whatsapp",
        mode="ai",
        status="open",
        language="ru",
        state={},
    )
    db.add(conv)
    db.commit()
    return conv


def test_retention_redacts_old_ai_runs(db, world):
    set_settings(db, world.tenant, voice={"transcript_retention_days": 30})
    conv = _conv(db, world)
    old, fresh = _run(db, world, conv, age_days=31), _run(db, world, conv, age_days=5)
    run_voice_retention()
    db.expire_all()
    old, fresh = db.get(AIRun, old.id), db.get(AIRun, fresh.id)
    assert old.reply_text == ""
    assert old.tool_calls == [
        {"name": "create_booking", "ok": True, "error": None, "ms": 12, "redacted": True},
        {"name": "add_tag", "ok": False, "error": "BAD_TAG", "ms": 1, "redacted": True},
    ]
    assert (old.input_tokens, old.output_tokens, old.latency_ms, old.model) == (321, 45, 870, "mock-1")
    assert old.conversation_id == conv.id and old.created_at < utcnow() - timedelta(days=30)
    assert fresh.reply_text and fresh.tool_calls[0]["input"]["client_name"] == "Айдар"  # within retention
    # idempotent: nothing left to redact
    assert redact_expired_ai_runs(db) == 0


def test_ai_run_redaction_respects_each_tenant_and_zero_days(db, world, other_world):
    set_settings(db, world.tenant, voice={"transcript_retention_days": 0})  # retention disabled
    conv = _conv(db, world)
    kept = _run(db, world, conv, age_days=4000)
    c = Contact(tenant_id=other_world.tenant.id, name="Other", phone_e164="+77015559999")
    db.add(c)
    db.flush()
    other_conv = Conversation(
        tenant_id=other_world.tenant.id,
        contact_id=c.id,
        channel="whatsapp",
        mode="ai",
        status="open",
        language="ru",
        state={},
    )
    db.add(other_conv)
    db.commit()
    other_run = AIRun(
        tenant_id=other_world.tenant.id,
        conversation_id=other_conv.id,
        provider="mock",
        model="m",
        prompt_version="v1",
        knowledge_article_ids=[],
        tool_calls=[],
        reply_text="old reply",
        created_at=utcnow() - timedelta(days=91),  # default retention: 90 days
    )
    db.add(other_run)
    db.commit()
    assert redact_expired_ai_runs(db) == 1
    db.expire_all()
    assert db.get(AIRun, kept.id).reply_text and db.get(AIRun, kept.id).tool_calls[0].get("input")
    assert db.get(AIRun, other_run.id).reply_text == ""


def test_redaction_picks_up_runs_with_only_tool_content(db, world):
    set_settings(db, world.tenant, voice={"transcript_retention_days": 10})
    conv = _conv(db, world)
    run = _run(db, world, conv, age_days=11, reply="")
    assert redact_expired_ai_runs(db) == 1
    db.expire_all()
    assert all("input" not in c and "result" not in c for c in db.get(AIRun, run.id).tool_calls)
