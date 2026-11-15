"""Speech-to-text for WhatsApp voice notes (v1.1): OpenAI-compatible adapter, confidence, low-confidence re-ask,
human fallback, voice-note message meta and the STT integration endpoints. No real network: httpx.MockTransport."""

from __future__ import annotations

import io
import json
import logging
import math
import wave

import httpx
import pytest
from sqlalchemy import func, select

from app.ai import stt as stt_mod
from app.ai.stt import (
    MAX_AUDIO_BYTES,
    MockSTT,
    NotConfiguredSTT,
    OpenAICompatibleSTT,
    STTError,
    STTNotConfigured,
    Transcription,
    confidence_from_segments,
    get_stt,
    sample_wav,
    set_stt,
)
from app.channels import whatsapp as wa
from app.config import get_settings
from app.models import AdminTask, AuditLog, Conversation, Message, Tenant
from tests.ai_helpers import add_wa_integration, post_webhook, wa_payload

API_KEY = "sk-test-SUPER-SECRET-key-0123456789"
BASE = "https://stt.example/v1"


@pytest.fixture(autouse=True)
def _reset():
    wa.clear_mock_outbox()
    yield
    set_stt(None)
    wa.clear_mock_outbox()


def _adapter(handler, **kw) -> OpenAICompatibleSTT:
    return OpenAICompatibleSTT(BASE, API_KEY, "whisper-1", 5, transport=httpx.MockTransport(handler), **kw)


def _verbose(text="Сколько стоит стрижка?", language="russian", logprobs=(-0.1, -0.3)):
    return {
        "task": "transcribe",
        "language": language,
        "duration": 2.1,
        "text": text,
        "segments": [{"id": i, "text": text, "avg_logprob": lp} for i, lp in enumerate(logprobs)],
    }


# ---------------------------------------------------------------- adapter
def test_adapter_success_sends_multipart_and_parses_verbose_json():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_verbose())

    tr = _adapter(handler).transcribe(b"OggS-audio", "audio/ogg; codecs=opus", "kk")
    assert tr.text == "Сколько стоит стрижка?" and tr.language == "ru"  # provider detection wins over hint
    assert tr.confidence == pytest.approx((math.exp(-0.1) + math.exp(-0.3)) / 2, abs=1e-4)
    (req,) = seen
    assert str(req.url) == f"{BASE}/audio/transcriptions" and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content.decode("latin-1")
    for name, value in (("model", "whisper-1"), ("response_format", "verbose_json"), ("language", "kk")):
        assert f'name="{name}"\r\n\r\n{value}' in body
    assert 'filename="voice.ogg"' in body and "OggS-audio" in body


def test_adapter_without_segments_has_null_confidence_and_hint_language():
    tr = _adapter(lambda r: httpx.Response(200, json={"text": " Сәлем "})).transcribe(b"x", "audio/ogg", "kk")
    assert tr == Transcription(text="Сәлем", language="kk", confidence=None)


def test_adapter_without_key_sends_no_authorization():
    seen = []
    adapter = OpenAICompatibleSTT(
        "http://whisper:8000/v1/",
        None,
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"text": "ok"})),
    )
    adapter.transcribe(b"x", "audio/mpeg")
    assert (
        "authorization" not in seen[0].headers
        and str(seen[0].url) == "http://whisper:8000/v1/audio/transcriptions"
    )
    assert b'name="language"' not in seen[0].content  # no hint -> provider auto-detects


def test_adapter_timeout_raises_stt_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(STTError, match="timeout"):
        _adapter(handler).transcribe(b"x", "audio/ogg", "ru")


def test_adapter_network_error_raises_stt_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(STTError, match="network error"):
        _adapter(handler).transcribe(b"x", "audio/ogg", "ru")


@pytest.mark.parametrize("status", [500, 502, 503, 401, 429])
def test_adapter_http_errors_raise_stt_error(status):
    with pytest.raises(STTError, match=f"HTTP {status}"):
        _adapter(lambda r: httpx.Response(status, text="upstream down")).transcribe(b"x", "audio/ogg")


@pytest.mark.parametrize("body", ["not json", json.dumps({"no_text": 1}), json.dumps([1, 2])])
def test_adapter_bad_payload_raises_stt_error(body):
    with pytest.raises(STTError):
        _adapter(lambda r: httpx.Response(200, text=body)).transcribe(b"x", "audio/ogg")


def test_adapter_rejects_oversize_and_empty_audio_without_calling_provider():
    calls = []
    adapter = _adapter(lambda r: calls.append(r) or httpx.Response(200, json={"text": "x"}))
    assert adapter.max_bytes == MAX_AUDIO_BYTES == 25 * 1024 * 1024
    with pytest.raises(STTError, match="too large"):
        adapter.transcribe(b"\0" * (MAX_AUDIO_BYTES + 1), "audio/ogg")
    with pytest.raises(STTError, match="empty"):
        adapter.transcribe(b"", "audio/ogg")
    assert calls == []


def test_adapter_never_leaks_the_key(caplog):
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(STTError) as exc:
        adapter.transcribe(b"x", "audio/ogg")
    assert API_KEY not in str(exc.value) and API_KEY not in repr(adapter)
    assert API_KEY not in caplog.text


def test_confidence_from_segments():
    assert confidence_from_segments(None) is None
    assert confidence_from_segments([]) is None
    assert confidence_from_segments([{"text": "a"}, {"avg_logprob": None}]) is None
    assert confidence_from_segments([{"avg_logprob": 0.0}]) == 1.0
    assert confidence_from_segments([{"avg_logprob": -1.0}, {"avg_logprob": -2.0}]) == pytest.approx(
        (math.exp(-1) + math.exp(-2)) / 2, abs=1e-4
    )
    assert confidence_from_segments([{"avg_logprob": 0.5}]) == 1.0  # clamped
    assert confidence_from_segments([{"avg_logprob": float("nan")}, {"avg_logprob": -0.5}]) == pytest.approx(
        math.exp(-0.5), abs=1e-4
    )


def test_provider_selection_from_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "stt_provider", "mock")
    assert isinstance(get_stt(), MockSTT)
    monkeypatch.setattr(s, "stt_provider", "none")
    assert isinstance(get_stt(), NotConfiguredSTT)
    with pytest.raises(STTNotConfigured):
        get_stt().transcribe(b"x", "audio/ogg")
    monkeypatch.setattr(s, "stt_provider", "openai_compatible")
    monkeypatch.setattr(s, "stt_base_url", None)
    assert isinstance(get_stt(), NotConfiguredSTT)  # half-configured -> human fallback, never a crash
    monkeypatch.setattr(s, "stt_base_url", BASE)
    monkeypatch.setattr(s, "stt_api_key", API_KEY)
    monkeypatch.setattr(s, "stt_model", "whisper-large-v3")
    monkeypatch.setattr(s, "stt_timeout_sec", 12.0)
    adapter = get_stt()
    assert isinstance(adapter, OpenAICompatibleSTT)
    assert (adapter.base_url, adapter.model, adapter.timeout) == (BASE, "whisper-large-v3", 12.0)
    assert stt_mod.stt_status() == {
        "provider": "openai_compatible",
        "configured": True,
        "model": "whisper-large-v3",
    }
    monkeypatch.setattr(s, "stt_provider", "something-else")
    assert stt_mod.provider_name() == "none"
    monkeypatch.setattr(s, "stt_provider", None)
    assert stt_mod.provider_name() == "mock"  # tests run WhatsApp in mock mode


def test_sample_wav_is_one_second_of_silence():
    with wave.open(io.BytesIO(sample_wav()), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 16000)


# ---------------------------------------------------------------- voice notes through the WhatsApp webhook
class StubSTT:
    name = "openai_compatible"
    model = "whisper-1"

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[bytes, str, str | None]] = []

    def transcribe(self, audio, mime, language_hint=None):
        self.calls.append((audio, mime, language_hint))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def _voice(client, media="MEDIA1"):
    return post_webhook(
        client,
        wa_payload(
            message={"type": "audio", "audio": {"id": media, "mime_type": "audio/ogg", "voice": True}}
        ),
    )


def _inbound(db) -> list[Message]:
    db.expire_all()
    return list(
        db.scalars(select(Message).where(Message.direction == "in").order_by(Message.received_at)).all()
    )


def _outbound(db) -> list[Message]:
    db.expire_all()
    return list(
        db.scalars(select(Message).where(Message.direction == "out").order_by(Message.received_at)).all()
    )


def test_voice_note_meta_on_success(client, world, db):
    add_wa_integration(db, world.tenant)
    set_stt(StubSTT(Transcription("Сколько стоит мужская стрижка?", "ru", 0.91)))
    assert _voice(client).status_code == 200
    (msg,) = _inbound(db)
    assert msg.text == "Сколько стоит мужская стрижка?" and msg.language == "ru"
    assert {
        k: msg.meta[k] for k in ("transcribed", "stt", "stt_confidence", "stt_language", "stt_error")
    } == {
        "transcribed": True,
        "stt": "openai_compatible",
        "stt_confidence": 0.91,
        "stt_language": "ru",
        "stt_error": None,
    }
    assert "stt_low_confidence" not in msg.meta
    assert "5000" in _outbound(db)[-1].text  # the AI answered the transcript

    # exposed as-is in the conversation message API
    conv = db.scalar(select(Conversation))
    items = client.get(f"/v1/conversations/{conv.id}/messages", headers=world.headers(world.admin)).json()[
        "items"
    ]
    voice = next(i for i in items if i["message_type"] == "audio")
    assert voice["meta"]["transcribed"] is True and voice["meta"]["stt_confidence"] == 0.91
    assert voice["meta"]["stt_language"] == "ru" and voice["meta"]["stt_error"] is None


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("ru", "Повторите, пожалуйста, или напишите текстом"),
        ("kk", "Қайталап жіберіңізші немесе мәтінмен жазыңыз"),
    ],
)
def test_low_confidence_reasks_in_client_language(client, world, db, lang, expected):
    add_wa_integration(db, world.tenant)
    set_stt(StubSTT(Transcription("ммм шш", lang, 0.31)))
    _voice(client)
    (msg,) = _inbound(db)
    assert msg.meta["stt_low_confidence"] is True and msg.meta["transcribed"] is True
    assert msg.meta["stt_confidence"] == 0.31 and msg.meta["stt_language"] == lang
    (reply,) = _outbound(db)
    assert expected in reply.text and reply.author == "ai"
    conv = db.scalar(select(Conversation))
    assert conv.mode == "ai" and db.scalar(select(func.count(AdminTask.id))) == 0


def test_threshold_comes_from_tenant_settings(client, world, db):
    add_wa_integration(db, world.tenant)
    t = db.get(Tenant, world.tenant.id)
    t.settings = {**(t.settings or {}), "voice": {"low_confidence_threshold": 0.95}}
    db.commit()
    set_stt(StubSTT(Transcription("Сколько стоит мужская стрижка?", "ru", 0.9)))
    _voice(client)
    assert _inbound(db)[0].meta["stt_low_confidence"] is True
    assert "Повторите" in _outbound(db)[0].text


def test_empty_transcript_is_treated_as_unintelligible(client, world, db):
    add_wa_integration(db, world.tenant)
    set_stt(StubSTT(Transcription("  ", None, None)))
    _voice(client)
    (msg,) = _inbound(db)
    assert msg.meta["transcribed"] is False and msg.meta["stt_low_confidence"] is True
    assert "Повторите" in _outbound(db)[0].text


def test_repeated_low_confidence_goes_to_a_human_and_success_resets(client, world, db):
    add_wa_integration(db, world.tenant)
    set_stt(StubSTT(Transcription("ммм", "ru", 0.2)))
    _voice(client, "M1")
    _voice(client, "M2")
    outs = _outbound(db)
    assert len(outs) == 2 and all("Повторите" in m.text for m in outs)  # asked twice
    _voice(client, "M3")  # third unintelligible note in a row -> human
    conv = db.scalar(select(Conversation))
    db.refresh(conv)
    assert conv.mode == "waiting_admin"
    task = db.scalar(select(AdminTask))
    assert task is not None and task.payload["reason"] == "stt: low confidence"

    # a clear note after a re-ask resets the streak
    db.query(AdminTask).delete()
    conv.mode, conv.state = "ai", {"stt_low_confidence_streak": 2}
    db.commit()
    set_stt(StubSTT(Transcription("Сколько стоит мужская стрижка?", "ru", 0.9)))
    _voice(client, "M4")
    db.refresh(conv)
    assert "stt_low_confidence_streak" not in (conv.state or {}) and conv.mode == "ai"


@pytest.mark.parametrize(
    "error", [STTError("timeout after 30s"), STTNotConfigured("No speech-to-text provider")]
)
def test_stt_failure_falls_back_to_human(client, world, db, error):
    add_wa_integration(db, world.tenant)
    set_stt(StubSTT(error))
    _voice(client)
    (msg,) = _inbound(db)
    assert msg.meta["transcribed"] is False and msg.meta["stt"] == "openai_compatible"
    assert msg.meta["stt_error"] == str(error) and msg.meta["stt_confidence"] is None
    assert msg.meta["stt_language"] is None
    conv = db.scalar(select(Conversation))
    assert conv.mode == "waiting_admin"
    task = db.scalar(select(AdminTask))
    assert task is not None and task.payload["reason"].startswith("stt:")
    (reply,) = _outbound(db)
    assert reply.author == "system"  # the pre-approved fallback text, not an AI guess


def test_real_adapter_failure_end_to_end(client, world, db):
    """Mock WhatsApp media is empty -> the real adapter refuses -> human fallback, provider never called."""
    add_wa_integration(db, world.tenant)
    hits = []
    set_stt(_adapter(lambda r: hits.append(r) or httpx.Response(200, json={"text": "x"})))
    _voice(client)
    (msg,) = _inbound(db)
    assert msg.meta["stt_error"] == "empty audio" and hits == []
    assert db.scalar(select(Conversation)).mode == "waiting_admin"


# ---------------------------------------------------------------- integration endpoints
def test_stt_status_endpoint_and_permissions(client, world, monkeypatch):
    r = client.get("/v1/integrations/stt", headers=world.headers(world.admin))
    assert r.status_code == 200 and r.json() == {"provider": "mock", "configured": True, "model": None}
    for user in (world.staff_user, world.analyst):
        assert client.get("/v1/integrations/stt", headers=world.headers(user)).status_code == 403
    s = get_settings()
    monkeypatch.setattr(s, "stt_provider", "openai_compatible")
    monkeypatch.setattr(s, "stt_base_url", BASE)
    monkeypatch.setattr(s, "stt_api_key", API_KEY)
    r = client.get("/v1/integrations/stt", headers=world.headers())
    assert r.json() == {"provider": "openai_compatible", "configured": True, "model": "whisper-1"}
    assert API_KEY not in r.text
    monkeypatch.setattr(s, "stt_base_url", None)
    r = client.get("/v1/integrations/stt", headers=world.headers())
    assert r.json() == {"provider": "none", "configured": False, "model": None}
    monkeypatch.setattr(s, "stt_provider", "none")
    assert client.get("/v1/integrations/stt", headers=world.headers()).json()["configured"] is False


def test_stt_test_endpoint_mock_ok_and_owner_only(client, world, db):
    for user in (world.admin, world.staff_user, world.analyst):
        assert client.post("/v1/integrations/stt/test", headers=world.headers(user)).status_code == 403
    r = client.post("/v1/integrations/stt/test", headers=world.headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["provider"] == "mock" and body["error"] is None
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == "integration.test", AuditLog.entity_id == "stt")
    )
    assert audit is not None and audit.payload == {"ok": True, "provider": "mock"}


def test_stt_test_endpoint_with_real_adapter(client, world):
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "", "segments": []})

    set_stt(_adapter(ok))
    body = client.post("/v1/integrations/stt/test", headers=world.headers()).json()
    assert body["ok"] is True and body["provider"] == "openai_compatible" and body["error"] is None
    assert b"RIFF" in seen[0].content and b'filename="voice.wav"' in seen[0].content  # bundled 1 s sample

    set_stt(_adapter(lambda r: httpx.Response(503, text="overloaded")))
    r = client.post("/v1/integrations/stt/test", headers=world.headers())
    body = r.json()
    assert body["ok"] is False and "HTTP 503" in body["error"] and API_KEY not in r.text

    set_stt(NotConfiguredSTT())
    body = client.post("/v1/integrations/stt/test", headers=world.headers()).json()
    assert body == {**body, "ok": False, "provider": "none"} and "configured" in body["error"]


def test_stt_routes_do_not_shadow_integration_kinds(client, world):
    assert client.post("/v1/integrations/voice/test", headers=world.headers()).json()["ok"] is True
    assert client.put("/v1/integrations/stt", headers=world.headers(), json={}).status_code in (404, 405)
