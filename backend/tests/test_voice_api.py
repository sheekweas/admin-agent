"""Calls API for the admin panel (docs/API.md "Voice"): list/detail, outbound rules (ТЗ §13.3), simulator,
/v1/reports/voice (§13.4), permissions and tenant isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.channels import voice
from app.config import get_settings
from app.models import AuditLog, Booking, Call, Conversation
from tests.ops_helpers import T0, make_booking
from tests.voice_helpers import add_voice_integration, said, set_settings, turn

BOOKING_UTTERANCES = ["Хочу записаться на мужскую стрижку завтра в 16:00", "Меня зовут Айдар", "Да"]


@pytest.fixture(autouse=True)
def _voice(db, world):
    add_voice_integration(db, world.tenant)
    voice.MOCK_CALLS.clear()
    yield
    voice.MOCK_CALLS.clear()


@pytest.fixture
def at_t0(monkeypatch):
    """Monday 11:00 Asia/Almaty for the outbound-hours / daily-cap rules."""
    monkeypatch.setattr("app.services.voice_calls.utcnow", lambda: T0)
    return T0


def _consent(db, world, **flags):
    world.contact.consent_flags = {"processing": True, "outbound_calls": True, **flags}
    db.commit()


def _outbound(client, world, purpose="booking_confirmation", user=None, **extra):
    body = {"contact_id": str(world.contact.id), "purpose": purpose, **extra}
    return client.post("/v1/calls/outbound", json=body, headers=world.headers(user or world.admin))


# ---------------------------------------------------------------- simulator + list/detail
def test_simulate_booking_call_then_list_and_detail(client, db, world):
    r = client.post(
        "/v1/calls/simulate",
        headers=world.headers(world.admin),
        json={"phone": "+77015550011", "name": "", "utterances": BOOKING_UTTERANCES},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["direction"] == "in" and d["purpose"] == "client_ai" and d["status"] == "completed"
    assert d["contact"]["phone_e164"] == "+77015550011" and d["contact"]["name"] == "Айдар"
    assert d["booking_id"] is not None and d["duration_sec"] >= 1
    assert d["transferred_to_human"] is False and d["recording_available"] is False
    assert d["metrics"]["booking_created"] is True and d["metrics"]["turns"] == 3
    assert isinstance(d["metrics"]["first_response_ms"], int)
    assert [e["speaker"] for e in d["transcript"]][:2] == ["ai", "client"]
    assert d["transcript"][-1] == {
        "speaker": "system",
        "text": "caller hung up",
        "at": d["transcript"][-1]["at"],
        "confidence": None,
    }
    assert any("Вы записаны" in e["text"] for e in d["transcript"] if e["speaker"] == "ai")
    b = db.get(Booking, d["booking_id"])
    assert b.channel == "voice"

    r = client.get("/v1/calls", headers=world.headers())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["id"] == d["id"]
    assert set(body["items"][0]) == {
        "id",
        "direction",
        "purpose",
        "status",
        "from_number",
        "to_number",
        "contact",
        "admin_task_id",
        "booking_id",
        "started_at",
        "ended_at",
        "duration_sec",
        "transferred_to_human",
        "recording_available",
        "created_at",
    }
    assert client.get("/v1/calls?direction=out", headers=world.headers()).json()["total"] == 0
    assert (
        client.get("/v1/calls?purpose=client_ai,reminder&status=completed", headers=world.headers()).json()[
            "total"
        ]
        == 1
    )
    today = datetime.now(UTC).date()
    q = f"/v1/calls?date_from={today - timedelta(days=1)}&date_to={today + timedelta(days=1)}"
    assert client.get(q, headers=world.headers()).json()["total"] == 1
    q = f"/v1/calls?date_from={today + timedelta(days=2)}"
    assert client.get(q, headers=world.headers()).json()["total"] == 0
    r = client.get(f"/v1/calls/{d['id']}", headers=world.headers())
    assert r.status_code == 200 and r.json()["transcript"] == d["transcript"]


def test_simulate_kk_and_silence(client, db, world):
    r = client.post(
        "/v1/calls/simulate",
        headers=world.headers(world.admin),
        json={"phone": "+77015550012", "language": "kk", "utterances": ["", "Бағаңыз қанша?"]},
    )
    d = r.json()
    assert d["transcript"][0]["text"].startswith("Сәлеметсіз бе")
    assert d["metrics"]["recognition_retries"] == 1 and d["metrics"]["turns"] == 1


def test_simulate_only_in_mock_mode_and_needs_write(client, db, world, monkeypatch):
    body = {"phone": "+77015550011", "utterances": ["Здравствуйте"]}
    assert (
        client.post("/v1/calls/simulate", json=body, headers=world.headers(world.staff_user)).status_code
        == 403
    )
    monkeypatch.setattr(get_settings(), "voice_provider", "twilio")
    assert client.post("/v1/calls/simulate", json=body, headers=world.headers()).status_code == 404


def test_calls_permissions_and_tenant_isolation(client, db, world, other_world):
    d = client.post(
        "/v1/calls/simulate",
        headers=world.headers(),
        json={"phone": "+77015550011", "utterances": ["Сколько стоит борода?"]},
    ).json()
    for user in (world.staff_user, world.analyst):
        assert client.get("/v1/calls", headers=world.headers(user)).status_code == 403
    assert client.get(f"/v1/calls/{d['id']}", headers=world.headers(world.staff_user)).status_code == 403
    # another organisation: the id looks exactly like a missing one, and the list is empty
    r = client.get(f"/v1/calls/{d['id']}", headers=other_world.headers())
    assert r.status_code == 404 and r.json()["detail"]["code"] == "NOT_FOUND"
    assert client.get("/v1/calls", headers=other_world.headers()).json() == {"items": [], "total": 0}
    assert client.get("/v1/calls/not-a-uuid", headers=world.headers()).status_code == 422


def test_analyst_view_masks_numbers():
    from app.services.voice_calls import call_items

    class _DB:
        def scalars(self, *_):
            return []

    call = Call(
        id=None,
        direction="in",
        purpose="client_ai",
        status="completed",
        from_number="+77015550011",
        to_number="+77270000001",
        duration_sec=3,
        metrics={},
        contact_id=None,
    )
    item = call_items(_DB(), [call], mask=True)[0]
    assert item["from_number"] == "+770******11" and item["to_number"].endswith("01")
    assert "5550011" not in item["from_number"]


# ---------------------------------------------------------------- outbound (ТЗ §13.3)
def test_outbound_requires_consent(client, db, world, at_t0):
    make_booking(db, world, start=T0 + timedelta(days=1))
    r = _outbound(client, world)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONSENT_REQUIRED"
    _consent(db, world)
    world.contact.opted_out = True
    db.commit()
    r = _outbound(client, world)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CONSENT_REQUIRED"
    assert not voice.MOCK_CALLS and db.scalar(select(Call)) is None


def test_outbound_outside_hours(client, db, world, monkeypatch):
    _consent(db, world)
    make_booking(db, world, start=T0 + timedelta(days=1))
    late = T0.replace(hour=17)  # 22:00 Almaty
    monkeypatch.setattr("app.services.voice_calls.utcnow", lambda: late)
    r = _outbound(client, world)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OUTSIDE_HOURS"
    early = T0.replace(hour=3)  # 08:00 Almaty, before outbound_hours [10, 20)
    monkeypatch.setattr("app.services.voice_calls.utcnow", lambda: early)
    assert _outbound(client, world).json()["detail"]["code"] == "OUTSIDE_HOURS"


def test_outbound_daily_cap(client, db, world, at_t0):
    _consent(db, world)
    make_booking(db, world, start=T0 + timedelta(days=1))
    set_settings(db, world.tenant, voice={"max_outbound_per_day": 2})
    assert _outbound(client, world).status_code == 201
    assert _outbound(client, world, purpose="reminder").status_code == 201
    r = _outbound(client, world, purpose="callback")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DAILY_CAP_REACHED"
    # admin escalation calls do not count towards the client cap; a new local day resets it
    db.add(Call(tenant_id=world.tenant.id, direction="out", purpose="admin_escalation", to_number="+7701"))
    db.commit()
    assert len(db.scalars(select(Call)).all()) == 3


def test_outbound_confirmation_call_flow(client, db, world, at_t0):
    _consent(db, world)
    b = make_booking(db, world, start=T0 + timedelta(days=1, hours=4))  # Tue 15:00 local
    r = _outbound(client, world, booking_id=str(b.id))
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["direction"] == "out" and item["purpose"] == "booking_confirmation"
    assert item["status"] == "ringing" and item["booking_id"] == str(b.id)
    assert item["to_number"] == world.contact.phone_e164 and item["from_number"] == "+77270000001"
    placed = voice.MOCK_CALLS[-1]
    assert placed["to"] == world.contact.phone_e164 and "/answer?call_id=" in placed["answer_url"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "call.outbound")) is not None

    path = placed["answer_url"].removeprefix(get_settings().public_base_url.rstrip("/"))
    xml = client.post(path, data={"CallStatus": "in-progress", "AnsweredBy": "human"}).text
    text = said(xml)
    assert "виртуальный помощник" in text and "Нурлан" in text  # disclosure + first name only
    assert "Мужская стрижка" in text and "17 ноября" in text and "15:00" in text and "Вы придёте" in text
    xml = turn(client, xml, "Да, приду")
    assert "ждём вас" in said(xml) and xml.endswith("<Hangup/></Response>")
    db.expire_all()
    call = db.get(Call, item["id"])
    assert call.status == "completed" and call.started_at is not None
    assert call.metrics["client_confirmed"] is True and call.metrics["turns"] == 1
    assert db.get(Conversation, call.conversation_id).channel == "voice"


def test_outbound_reschedule_uses_the_same_ai_loop(client, db, world, at_t0):
    _consent(db, world)
    b = make_booking(db, world, start=T0 + timedelta(days=1, hours=4))
    item = _outbound(client, world, purpose="reschedule", booking_id=str(b.id)).json()
    path = voice.MOCK_CALLS[-1]["answer_url"].removeprefix(get_settings().public_base_url.rstrip("/"))
    xml = client.post(path, data={"AnsweredBy": "human"}).text
    assert "перенос" in said(xml)
    conv = db.get(Conversation, db.get(Call, item["id"]).conversation_id)
    assert conv.state["intent"] == "reschedule" and conv.state["reschedule_number"] == f"BK-{b.number:06d}"


def test_outbound_voicemail_is_not_an_answer(client, db, world, at_t0):
    _consent(db, world)
    make_booking(db, world, start=T0 + timedelta(days=1))
    item = _outbound(client, world, purpose="reminder").json()
    path = voice.MOCK_CALLS[-1]["answer_url"].removeprefix(get_settings().public_base_url.rstrip("/"))
    xml = client.post(path, data={"AnsweredBy": "machine_start"}).text
    assert "<Say" not in xml and "<Hangup/>" in xml  # nothing is left on a voicemail
    db.expire_all()
    assert db.get(Call, item["id"]).status == "no_answer"


def test_outbound_status_callbacks(client, db, world, at_t0):
    _consent(db, world)
    make_booking(db, world, start=T0 + timedelta(days=1))
    item = _outbound(client, world, purpose="reminder").json()
    r = client.post(f"/v1/webhooks/voice/mock/status?call_id={item['id']}", data={"CallStatus": "busy"})
    assert r.status_code == 200
    db.expire_all()
    call = db.get(Call, item["id"])
    assert call.status == "busy" and call.ended_at is not None


def test_outbound_validation(client, db, world, other_world, at_t0):
    _consent(db, world)
    r = _outbound(client, world)  # no upcoming booking for a confirmation call
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BOOKING_REQUIRED"
    assert _outbound(client, world, purpose="callback").status_code == 201  # callback needs no booking
    r = _outbound(client, world, purpose="marketing")
    assert r.status_code == 422
    r = client.post(
        "/v1/calls/outbound",
        json={"contact_id": str(world.contact.id), "purpose": "callback"},
        headers=other_world.headers(),
    )
    assert r.status_code == 404  # foreign contact
    assert _outbound(client, world, purpose="callback", user=world.analyst).status_code == 403


# ---------------------------------------------------------------- reports (ТЗ §13.4)
def _call(db, world, *, status="completed", started=True, duration=60, purpose="client_ai", **metrics):
    c = Call(
        tenant_id=world.tenant.id,
        direction="in" if purpose == "client_ai" else "out",
        purpose=purpose,
        to_number="+77270000001",
        status=status,
        started_at=T0 if started else None,
        duration_sec=duration,
        created_at=T0,
        metrics=metrics,
    )
    db.add(c)
    db.commit()
    return c


def test_reports_voice_numbers(client, db, world, other_world):
    _call(db, world, duration=60, first_response_ms=800, turns=4, recognition_retries=1, booking_created=True)
    _call(db, world, duration=120, first_response_ms=1200, turns=3, transferred_to_human=True)
    _call(db, world, duration=30, first_response_ms=1000, turns=3, recognition_retries=1)
    _call(db, world, status="no_answer", started=False, duration=0, purpose="reminder")
    _call(db, world, purpose="admin_escalation", duration=500, transferred_to_human=True)  # excluded
    _call(db, other_world, duration=999)  # other tenant
    day = T0.date().isoformat()
    r = client.get(f"/v1/reports/voice?date_from={day}&date_to={day}", headers=world.headers())
    assert r.status_code == 200
    assert r.json() == {
        "calls": 4,
        "answered": 3,
        "avg_duration_sec": 70.0,
        "avg_first_response_ms": 1000.0,
        "transfer_rate": 33.3,
        "booking_rate": 33.3,
        "recognition_retry_rate": 16.7,  # 2 failed of 12 utterances
    }
    nxt = (T0.date() + timedelta(days=1)).isoformat()
    r = client.get(f"/v1/reports/voice?date_from={nxt}", headers=world.headers(world.analyst))
    assert r.json() == {
        "calls": 0,
        "answered": 0,
        "avg_duration_sec": 0.0,
        "avg_first_response_ms": 0.0,
        "transfer_rate": 0.0,
        "booking_rate": 0.0,
        "recognition_retry_rate": 0.0,
    }
    assert client.get("/v1/reports/voice", headers=world.headers(world.staff_user)).status_code == 403


def test_simulated_call_feeds_report(client, db, world):
    client.post(
        "/v1/calls/simulate",
        headers=world.headers(),
        json={"phone": "+77015550011", "utterances": BOOKING_UTTERANCES},
    )
    rep = client.get("/v1/reports/voice", headers=world.headers()).json()
    assert rep["calls"] == 1 and rep["answered"] == 1 and rep["booking_rate"] == 100.0
