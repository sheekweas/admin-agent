"""End-to-end journeys through the public HTTP surface only (ТЗ §22.1 "E2E").

Every action a client, admin, barber or owner takes goes through the API: signed WhatsApp webhooks,
Telegram callback webhooks, the IVR (voice) webhook and the REST API with JWTs. Background work is driven
by calling the Celery task bodies (``app.workers.tasks.run_*``) synchronously, with an injected ``now``
where the scenario needs time to pass (5/10-minute escalation, reminders, retry backoff).
The DB is only *read* for assertions that have no public endpoint (notification rows, call ids).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.channels import telegram, voice
from app.channels import whatsapp as wa
from app.models import AdminTask, Call, Lead, Notification, OutboxEvent
from app.services.common import utcnow
from app.services.integrations import telegram_webhook_secret
from app.workers import tasks
from tests.ai_helpers import post_webhook, wa_payload

ALMATY = ZoneInfo("Asia/Almaty")
PNID = "PNID-E2E-1"
CLIENT_WA = "77015550101"
CLIENT_PHONE = "+77015550101"


@pytest.fixture(autouse=True)
def _mocks():
    wa.clear_mock_outbox()
    telegram.reset_mock()
    voice.MOCK_CALLS.clear()
    yield
    wa.clear_mock_outbox()
    telegram.reset_mock()
    voice.MOCK_CALLS.clear()


# ---------------------------------------------------------------- helpers (HTTP only)
def _connect_whatsapp(client, world) -> None:
    r = client.put(
        "/v1/integrations/whatsapp",
        headers=world.headers(world.owner),
        json={"config": {"phone_number_id": PNID}, "secrets": {"access_token": "e2e-token"}, "enabled": True},
    )
    assert r.status_code == 200, r.text


def _wa(client, text: str, *, wa_id: str = CLIENT_WA, name: str = "", mid: str | None = None) -> dict:
    r = post_webhook(client, wa_payload(pnid=PNID, wa_id=wa_id, text=text, name=name, mid=mid))
    assert r.status_code == 200, r.text
    return r.json()


def _wa_texts(to: str = CLIENT_WA) -> list[str]:
    return [
        m.get("text", {}).get("body", "") if isinstance(m.get("text"), dict) else str(m.get("text", ""))
        for m in wa.mock_outbox()
        if m.get("to") == to
    ]


def _last_wa(to: str = CLIENT_WA) -> str:
    texts = _wa_texts(to)
    assert texts, f"nothing was sent to {to}"
    return texts[-1]


def _tg(chat: str) -> list[str]:
    return [m["text"] for m in telegram.mock_messages(chat)]


def _tg_post(client, world, update: dict):
    r = client.post(
        f"/v1/webhooks/telegram/{world.tenant.id}",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": telegram_webhook_secret(world.tenant.id)},
    )
    assert r.status_code == 200, r.text
    return r


def _tg_button(client, world, chat_id: int, data: str, message_id: int = 10):
    return _tg_post(
        client,
        world,
        {
            "update_id": 1,
            "callback_query": {
                "id": f"cb-{data}",
                "from": {"id": chat_id},
                "data": data,
                "message": {"message_id": message_id, "chat": {"id": chat_id}, "text": "alert"},
            },
        },
    )


def _bookings(client, world, **params) -> list[dict]:
    r = client.get("/v1/bookings", headers=world.headers(world.admin), params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _tomorrow_local():
    return (utcnow().astimezone(ALMATY) + timedelta(days=1)).date()


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(ALMATY)


def _no_quiet_hours(client, world) -> None:
    """Escalation calls are suppressed at night (quiet hours); make the scenario independent of wall clock."""
    r = client.patch(
        "/v1/tenant",
        headers=world.headers(world.owner),
        json={"settings": {"escalation": {"quiet_hours": [0, 0]}}},
    )
    assert r.status_code == 200, r.text


# ====================================================================== (a) first message -> visit -> payment
def test_e2e_kk_whatsapp_booking_to_paid_visit_and_repeat_lead(client, world, db):
    _connect_whatsapp(client, world)
    owner, admin = world.headers(world.owner), world.headers(world.admin)

    # 1. A new client writes in Kazakh. The AI asks for the name, proposes the slot and books only on "Иә".
    assert _wa(client, "Сәлеметсіз бе! Ертең сағат 16:00-де шаш қиюға бос орын бар ма?")["accepted"] == 1
    assert "Атыңызды" in _last_wa()
    _wa(client, "Нұрлан")
    assert "растайсыз ба" in _last_wa()
    assert _bookings(client, world) == []  # nothing booked "on words"
    _wa(client, "Иә")
    (b,) = _bookings(client, world)
    start = _local(b["start_at"])
    assert start.date() == _tomorrow_local() and start.strftime("%H:%M") == "16:00"
    assert b["status"] == "confirmed" and b["source"] == "ai_agent" and b["channel"] == "whatsapp"
    assert b["contact"]["phone_e164"] == CLIENT_PHONE and b["contact"]["name"] == "Нұрлан"
    number = f"BK-{b['number']:06d}" if isinstance(b["number"], int) else b["number"]
    confirm = _last_wa()
    assert "Жазылуыңыз расталды" in confirm and number in confirm

    conv = client.get("/v1/conversations", headers=admin).json()["items"]
    assert len(conv) == 1 and conv[0]["language"] == "kk" and conv[0]["mode"] == "ai"

    # 2. Worker: booking.confirmed -> Telegram to both admins (once), reminders scheduled, no duplicate WA.
    wa_before = len(_wa_texts())
    tasks.run_dispatch_outbox()
    tasks.run_dispatch_outbox()  # redelivery is a no-op
    for chat in ("1001", "1002"):
        alerts = [t for t in _tg(chat) if "ЖАҢА ЖАЗЫЛУ" in t or "НОВАЯ ЗАПИСЬ" in t]
        assert len(alerts) == 1 and number in alerts[0]
    assert len(_wa_texts()) == wa_before  # the AI already confirmed in chat; no second confirmation
    reminders = db.scalars(
        select(Notification).where(Notification.kind == "reminder", Notification.status == "pending")
    ).all()
    assert reminders and all(r.payload["booking_id"] == b["id"] for r in reminders)

    # 3. Reminders go out when due (the last one 2 h before the visit), each exactly once.
    due = datetime.fromisoformat(b["start_at"]) - timedelta(hours=2)
    tasks.run_send_due_notifications(now=due - timedelta(hours=3))
    tasks.run_send_due_notifications(now=due + timedelta(seconds=1))
    tasks.run_send_due_notifications(now=due + timedelta(minutes=5))
    assert len(_wa_texts()) == wa_before + len(reminders) and "16:00" in _last_wa()

    # 4. Admin checks the client in and completes the visit in the CRM; payment is recorded.
    for status in ("checked_in", "completed"):
        r = client.patch(
            f"/v1/bookings/{b['id']}", headers=admin, json={"action": "status", "status": status}
        )
        assert r.status_code == 200 and r.json()["status"] == status, r.text
    today = utcnow().astimezone(ALMATY).date()
    q = {"date_from": today.isoformat(), "date_to": (today + timedelta(days=2)).isoformat()}
    kpi = client.get("/v1/reports/overview", headers=owner, params=q).json()["kpi"]
    assert Decimal(str(kpi["revenue"])) == 0 and kpi["unpaid_completed"] == 1  # AT-08: visit without payment

    r = client.post(
        "/v1/payments",
        headers=admin,
        json={"booking_id": b["id"], "amount": "5000", "method": "cash", "kind": "payment"},
    )
    assert r.status_code == 201, r.text
    assert Decimal(client.get(f"/v1/bookings/{b['id']}", headers=admin).json()["paid_amount"]) == 5000

    # 5. Owner overview: revenue, visits and funnel reflect the journey.
    ov = client.get("/v1/reports/overview", headers=owner, params=q).json()
    assert Decimal(str(ov["kpi"]["revenue"])) == 5000 and Decimal(str(ov["kpi"]["net_revenue"])) == 5000
    assert ov["kpi"]["visits"] == 1 and ov["kpi"]["bookings"] == 1 and ov["kpi"]["unpaid_completed"] == 0
    funnel = {f["stage"]: f["count"] for f in ov["funnel"]}
    assert funnel.get("visited", 0) >= 1
    lf = client.get("/v1/leads/funnel", headers=owner, params=q).json()
    assert {i["stage"]: i["count"] for i in lf["items"]}.get("visited") == 1
    leads = client.get("/v1/leads", headers=admin).json()["items"]
    assert len(leads) == 1 and leads[0]["stage"] == "visited"

    # 6. The same client comes back: a NEW lead is opened in the "repeat" stage.
    _wa(client, "Сәлеметсіз бе! Тағы жазылғым келеді")
    leads = client.get("/v1/leads", headers=admin).json()["items"]
    assert len(leads) == 2
    new = next(lead for lead in leads if lead["id"] != b.get("lead_id") and lead["stage"] != "visited")
    assert new["stage"] == "repeat"
    contact = client.get(f"/v1/contacts/{b['contact']['id']}", headers=admin).json()
    assert "повторный клиент" in contact["tags"]


# ====================================================================== (b) AI can't answer -> escalation -> KB
def test_e2e_unknown_question_escalates_calls_and_becomes_knowledge(client, world, db):
    _connect_whatsapp(client, world)
    _no_quiet_hours(client, world)
    admin, owner = world.headers(world.admin), world.headers(world.owner)
    question = "Сколько стоит окрашивание волос?"

    # 1. AI does not know the price: no invented number, an admin task is opened (AT-03).
    _wa(client, question, name="Айгерим")
    reply = _last_wa()
    assert "администратор" in reply.lower() and not any(ch.isdigit() for ch in reply)
    (task,) = client.get("/v1/admin-tasks", headers=admin, params={"status": "open"}).json()["items"]
    assert "окрашивание" in task["question"] and task["escalation_level"] == 0
    t0 = datetime.fromisoformat(task["created_at"])

    tasks.run_dispatch_outbox(now=utcnow() + timedelta(seconds=1))
    assert len([t for t in _tg("1001") if "ВОПРОС" in t]) == 1 and _tg("1002") == []  # T0: primary only

    # 2. No ACK for 5 minutes -> repeat to primary + backup (AT-04).
    assert tasks.run_process_escalations(now=t0 + timedelta(minutes=4, seconds=59)) == 0
    assert tasks.run_process_escalations(now=t0 + timedelta(minutes=5)) == 1
    tasks.run_send_due_notifications(now=t0 + timedelta(minutes=5))
    for chat in ("1001", "1002"):
        assert len([t for t in _tg(chat) if t.startswith("ПОВТОРНО")]) == 1
    assert not voice.MOCK_CALLS

    # 3. Still no ACK at 10 minutes -> a call to the primary admin (AT-05).
    assert tasks.run_process_escalations(now=t0 + timedelta(minutes=10)) == 1
    assert len(voice.MOCK_CALLS) == 1
    call = db.scalars(select(Call).where(Call.admin_task_id == task["id"])).one()
    assert call.to_number == world.admin.phone_e164 and call.status == "ringing"

    # 4. The admin presses "1" in the IVR -> task ACKed by that admin, chain stopped (AT-06).
    r = client.post(f"/v1/webhooks/voice/mock/gather?call_id={call.id}", data={"Digits": "1"})
    assert r.status_code == 200 and "<Hangup/>" in r.text
    t = client.get("/v1/admin-tasks", headers=admin).json()["items"][0]
    assert t["status"] == "acked" and t["assignee"]["id"] == str(world.admin.id)
    tasks.run_process_escalations(now=t0 + timedelta(minutes=20))
    assert len(voice.MOCK_CALLS) == 1  # no backup call after ACK

    # 5. Resolve in the CRM: answer relayed to the client, knowledge draft created.
    answer = "Окрашивание волос стоит от 12000 тенге, длится около 2 часов."
    r = client.post(
        f"/v1/admin-tasks/{task['id']}/resolve",
        headers=admin,
        json={"answer": answer, "relay_to_client": True, "add_to_knowledge": True},
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved", r.text
    assert _last_wa() == f"Администратор уточнил: {answer}"
    drafts = client.get("/v1/knowledge", headers=admin, params={"status": "draft"}).json()["items"]
    assert len(drafts) == 1 and drafts[0]["content"] == answer

    # The AI does not use a draft: until the owner publishes, the next client is escalated again.
    other = "77015550199"
    _wa(client, question, wa_id=other, name="Дана")
    assert "12000" not in _last_wa(other)
    assert db.scalar(select(func.count(AdminTask.id))) == 2

    # 6. Owner publishes (admins cannot), and the AI now answers the same question by itself.
    assert client.post(f"/v1/knowledge/{drafts[0]['id']}/publish", headers=admin).status_code == 403
    r = client.post(f"/v1/knowledge/{drafts[0]['id']}/publish", headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "published"
    _wa(client, question, name="Айгерим")
    assert "12000" in _last_wa()
    assert db.scalar(select(func.count(AdminTask.id))) == 2  # no new task for the original client


# ====================================================================== (c) early leave via Telegram approval
def test_e2e_early_leave_approved_in_telegram_conflict_rescheduled(client, world, db):
    _connect_whatsapp(client, world)
    admin, barber = world.headers(world.admin), world.headers(world.staff_user)
    day = _tomorrow_local()
    at16 = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ALMATY).isoformat()

    # Barber2 is busy at 16:00 (desk booking), so the WhatsApp client lands on barber1 (Арман) at 16:00.
    r = client.post(
        "/v1/bookings",
        headers={**admin, "Idempotency-Key": "e2e-c-desk"},
        json={
            "contact": {"name": "Тимур", "phone": "+77015550177"},
            "service_id": str(world.haircut.id),
            "staff_id": str(world.barber2.id),
            "start_at": at16,
        },
    )
    assert r.status_code == 201, r.text
    _wa(client, "Здравствуйте! Хочу записаться на мужскую стрижку завтра в 16:00", name="Айдар")
    _wa(client, "Да")
    ai_booking = next(b for b in _bookings(client, world) if b["source"] == "ai_agent")
    assert ai_booking["staff"]["id"] == str(world.barber1.id)
    tasks.run_dispatch_outbox()
    tasks.run_send_due_notifications()
    telegram.reset_mock()
    wa_before = len(_wa_texts())

    # 1. The barber asks to leave at 15:00 tomorrow (own staff profile, via API).
    r = client.post(
        "/v1/staff/shift-requests",
        headers=barber,
        json={"kind": "early_leave", "date": day.isoformat(), "new_end": "15:00", "reason": "к врачу"},
    )
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending" and [c["id"] for c in req["conflicts"]] == [ai_booking["id"]]

    # 2. Admins get a Telegram card with Approve / Reject; the primary admin approves by button.
    tasks.run_dispatch_outbox()
    tasks.run_send_due_notifications()
    (card,) = [m for m in telegram.mock_messages("1001") if m["text"].startswith("ЗАПРОС НА ИЗМЕНЕНИЕ")]
    callbacks = [b.get("callback_data") for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert "ЗАПРОС НА ИЗМЕНЕНИЕ СМЕНЫ" in card["text"] and "Затронуто записей: 1" in card["text"]
    assert f"shift:approve:{req['id']}" in callbacks
    _tg_button(client, world, 1001, f"shift:approve:{req['id']}", message_id=card.get("message_id", 10))
    reqs = client.get("/v1/staff/shift-requests", headers=admin).json()["items"]
    assert reqs[0]["status"] == "approved"

    # 3. No booking was cancelled; a schedule_conflict task was opened for the admins.
    assert client.get(f"/v1/bookings/{ai_booking['id']}", headers=admin).json()["status"] == "confirmed"
    conflict = [
        t
        for t in client.get("/v1/admin-tasks", headers=admin).json()["items"]
        if t["kind"] == "schedule_conflict"
    ]
    assert len(conflict) == 1 and conflict[0]["status"] == "open"

    # 4. Availability: barber1's afternoon is closed for new clients; barber2 still works.
    av = client.get(
        "/v1/availability",
        headers=admin,
        params={
            "service_id": str(world.haircut.id),
            "date_from": day.isoformat(),
            "date_to": day.isoformat(),
        },
    ).json()["items"]
    b1 = [_local(s["start_at"]).strftime("%H:%M") for s in av if s["staff_id"] == str(world.barber1.id)]
    b2 = [_local(s["start_at"]).strftime("%H:%M") for s in av if s["staff_id"] == str(world.barber2.id)]
    assert b1 and max(b1) < "15:00" and "17:00" in b2

    # 5. Admin moves the conflicting booking to barber2 at 17:00; the client is notified on WhatsApp.
    at17 = datetime(day.year, day.month, day.day, 17, 0, tzinfo=ALMATY).isoformat()
    r = client.patch(
        f"/v1/bookings/{ai_booking['id']}",
        headers=admin,
        json={"action": "reschedule", "start_at": at17, "staff_id": str(world.barber2.id)},
    )
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["staff"]["id"] == str(world.barber2.id) and _local(moved["start_at"]).hour == 17
    assert client.get(f"/v1/bookings/{ai_booking['id']}", headers=admin).json()["status"] == "rescheduled"
    tasks.run_dispatch_outbox()
    tasks.run_send_due_notifications()
    tasks.run_dispatch_outbox()
    new_msgs = _wa_texts()[wa_before:]
    assert len(new_msgs) == 1 and "17:00" in new_msgs[0], new_msgs  # notified exactly once
    assert any("17:00" in t for t in _tg("1001"))  # admins see the move too


# ====================================================================== (d) Telegram outage
def test_e2e_telegram_outage_retries_with_backoff_and_delivers_once(client, world, db, monkeypatch):
    admin = world.headers(world.admin)
    real_call = telegram.TelegramClient._call
    state = {"down": True, "attempts": 0}

    def flaky(self, method, payload):
        if state["down"]:
            state["attempts"] += 1
            raise telegram.TelegramError("HTTP 502: Bad Gateway", retryable=True)
        return real_call(self, method, payload)

    monkeypatch.setattr(telegram.TelegramClient, "_call", flaky)

    day = _tomorrow_local() + timedelta(days=1)
    r = client.post(
        "/v1/bookings",
        headers={**admin, "Idempotency-Key": "e2e-d-1"},
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "staff_id": str(world.barber1.id),
            "start_at": datetime(day.year, day.month, day.day, 12, 0, tzinfo=ALMATY).isoformat(),
        },
    )
    assert r.status_code == 201, r.text

    t0 = utcnow() + timedelta(seconds=1)
    assert tasks.run_dispatch_outbox(now=t0) >= 1
    ev = db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "booking.confirmed")).one()
    assert ev.status == "processed"  # the event became durable notifications; nothing is lost
    tg = db.scalars(select(Notification).where(Notification.channel == "telegram")).all()
    assert {n.recipient for n in tg} == {"1001", "1002"}
    assert all(n.status == "pending" and n.attempts == 1 and "502" in n.last_error for n in tg)
    first_retry = min(n.next_attempt_at for n in tg)
    assert first_retry >= t0 + timedelta(seconds=30)  # exponential backoff

    # Before the backoff expires nothing is attempted; failures keep increasing the delay.
    attempts = state["attempts"]
    tasks.run_send_due_notifications(now=t0 + timedelta(seconds=5))
    assert state["attempts"] == attempts
    tasks.run_send_due_notifications(now=first_retry)
    db.expire_all()
    tg = db.scalars(select(Notification).where(Notification.channel == "telegram")).all()
    assert all(n.attempts == 2 and n.status == "pending" for n in tg)
    assert all(n.next_attempt_at - first_retry > timedelta(seconds=30) for n in tg)

    # Telegram recovers -> delivered exactly once per admin, further ticks/redeliveries change nothing.
    state["down"] = False
    later = max(n.next_attempt_at for n in tg)
    assert tasks.run_send_due_notifications(now=later) == 2
    for _ in range(3):
        tasks.run_send_due_notifications(now=later + timedelta(hours=1))
        tasks.run_dispatch_outbox(now=later + timedelta(hours=1))
    ev.status = "pending"  # simulate a worker crash after delivery but before commit -> redelivery
    db.commit()
    tasks.run_dispatch_outbox(now=later + timedelta(hours=2))
    for chat in ("1001", "1002"):
        msgs = [t for t in _tg(chat) if t.startswith("НОВАЯ ЗАПИСЬ")]
        assert len(msgs) == 1, msgs
    db.expire_all()
    assert all(
        n.status == "sent" for n in db.scalars(select(Notification).where(Notification.channel == "telegram"))
    )
    assert db.scalar(select(func.count(Lead.id))) >= 1  # CRM data untouched by the outage
