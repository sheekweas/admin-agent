"""API: catalogue, availability, bookings, CRM routes; AT-09, staff-role limits, tenant isolation / IDOR."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.models import AuditLog, Booking, OutboxEvent
from tests.booking_helpers import at, future_day, local_hhmm


def _book(client, world, start, *, staff=None, user=None, key=None, **extra):
    headers = world.headers(user)
    if key:
        headers["Idempotency-Key"] = key
    body = {
        "contact_id": str(world.contact.id),
        "service_id": str(world.haircut.id),
        "staff_id": str((staff or world.barber1).id),
        "start_at": start.isoformat(),
        **extra,
    }
    return client.post("/v1/bookings", json=body, headers=headers)


def _slot_times(client, world, day, staff=None):
    r = client.get(
        "/v1/availability",
        headers=world.headers(),
        params={
            "service_id": str(world.haircut.id),
            "date_from": day.isoformat(),
            "staff_id": str((staff or world.barber1).id),
        },
    )
    assert r.status_code == 200, r.text
    return [local_hhmm(datetime.fromisoformat(i["start_at"])) for i in r.json()["items"]]


def test_availability_and_create_booking(client, world):
    day = future_day()
    r = client.get(
        "/v1/availability",
        headers=world.headers(),
        params={
            "service_id": str(world.haircut.id),
            "date_from": day.isoformat(),
            "date_to": day.isoformat(),
        },
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert {"slot_id", "staff_id", "staff_name", "branch_id", "start_at", "end_at"} <= set(items[0])
    assert len(items) == 2 * 34

    r = client.post(
        "/v1/bookings",
        headers={**world.headers(world.admin), "Idempotency-Key": "k-1"},
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "slot_id": items[0]["slot_id"],
            "comment": "первый раз",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "confirmed" and data["number"] == 1 and data["comment"] == "первый раз"
    assert data["contact"]["phone_e164"] == "+77071234567"
    assert (
        data["services"][0]["title_ru"] == "Мужская стрижка"
        and data["paid_amount"] == "0.00"
        and data["total_price"] == "5000.00"
    )
    # replay with the same Idempotency-Key → same booking
    again = client.post(
        "/v1/bookings",
        headers={**world.headers(world.admin), "Idempotency-Key": "k-1"},
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "slot_id": items[0]["slot_id"],
        },
    )
    assert again.status_code == 201 and again.json()["id"] == data["id"]


def test_conflict_409_with_alternatives(client, world):
    start = at(future_day(), 12)
    assert _book(client, world, start).status_code == 201
    r = _book(client, world, start)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SLOT_UNAVAILABLE" and detail["alternatives"]


def test_create_with_inline_contact(client, world, db):
    r = client.post(
        "/v1/bookings",
        headers=world.headers(),
        json={
            "contact": {"name": "Новый", "phone": "+7 701 555 66 77"},
            "service_id": str(world.beard.id),
            "start_at": at(future_day(), 12).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["contact"]["phone_e164"] == "+77015556677"
    r = client.post(
        "/v1/bookings",
        headers=world.headers(),
        json={"service_id": str(world.beard.id), "start_at": at(future_day(), 12).isoformat()},
    )
    assert r.status_code == 422


def test_at09_cancel_frees_slot_notifies_and_audits(client, world, db):
    day = future_day()
    b = _book(client, world, at(day, 12)).json()
    assert "12:00" not in _slot_times(client, world, day)
    r = client.patch(
        f"/v1/bookings/{b['id']}",
        headers=world.headers(world.admin),
        json={"action": "cancel", "reason": "клиент заболел"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "клиент заболел"
    assert "12:00" in _slot_times(client, world, day)
    assert db.scalar(select(OutboxEvent).where(OutboxEvent.event_type == "booking.cancelled")) is not None
    history = client.get(f"/v1/bookings/{b['id']}/history", headers=world.headers()).json()["items"]
    assert [h["action"] for h in history] == ["booking.create", "booking.cancel"]
    assert history[1]["actor_id"] == str(world.admin.id)


def test_reschedule_status_and_comment(client, world):
    day = future_day()
    b = _book(client, world, at(day, 12)).json()
    r = client.patch(
        f"/v1/bookings/{b['id']}",
        headers=world.headers(),
        json={"action": "reschedule", "start_at": at(day, 16).isoformat(), "staff_id": str(world.barber2.id)},
    )
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] != b["id"] and new["rescheduled_from_id"] == b["id"] and new["staff"]["name"] == "Ерлан"
    assert client.get(f"/v1/bookings/{b['id']}", headers=world.headers()).json()["status"] == "rescheduled"
    r = client.patch(
        f"/v1/bookings/{new['id']}",
        headers=world.headers(),
        json={"action": "status", "status": "completed", "comment": "всё ок"},
    )
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["comment"] == "всё ок"
    r = client.patch(
        f"/v1/bookings/{new['id']}", headers=world.headers(), json={"action": "status", "status": "confirmed"}
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "INVALID_TRANSITION"


def test_list_filters(client, world):
    day = future_day()
    _book(client, world, at(day, 12))
    _book(client, world, at(day, 12), staff=world.barber2)
    _book(client, world, at(future_day(5), 12))
    r = client.get(
        "/v1/bookings",
        headers=world.headers(),
        params={"date_from": day.isoformat(), "date_to": day.isoformat()},
    )
    assert r.json()["total"] == 2
    r = client.get("/v1/bookings", headers=world.headers(), params={"staff_id": str(world.barber2.id)})
    assert [i["staff"]["id"] for i in r.json()["items"]] == [str(world.barber2.id)]
    r = client.get("/v1/bookings", headers=world.headers(), params={"status": "cancelled"})
    assert r.json() == {"items": [], "total": 0}


def test_staff_role_sees_and_touches_only_own(client, world):
    day = future_day()
    own = _book(client, world, at(day, 12)).json()  # barber1 is the staff user
    other = _book(client, world, at(day, 12), staff=world.barber2).json()
    h = world.headers(world.staff_user)

    items = client.get("/v1/bookings", headers=h).json()["items"]
    assert [i["id"] for i in items] == [own["id"]]
    assert client.get(f"/v1/bookings/{other['id']}", headers=h).status_code == 404
    assert (
        client.patch(
            f"/v1/bookings/{other['id']}", headers=h, json={"action": "status", "status": "checked_in"}
        ).status_code
        == 404
    )
    # cancel/reschedule are admin operations; marking own visit is allowed
    assert (
        client.patch(
            f"/v1/bookings/{own['id']}", headers=h, json={"action": "cancel", "reason": "x"}
        ).status_code
        == 403
    )
    r = client.patch(
        f"/v1/bookings/{own['id']}", headers=h, json={"action": "status", "status": "checked_in"}
    )
    assert r.status_code == 200 and r.json()["status"] == "checked_in"
    # bookings for another master are refused
    assert _book(client, world, at(day, 16), staff=world.barber2, user=world.staff_user).status_code == 403
    # schedule & shift requests: only self
    assert (
        client.get(
            f"/v1/staff/{world.barber2.id}/schedule", headers=h, params={"date_from": day.isoformat()}
        ).status_code
        == 403
    )
    r = client.post(
        "/v1/staff/shift-requests",
        headers=h,
        json={"staff_id": str(world.barber2.id), "kind": "day_off", "date": day.isoformat()},
    )
    assert r.status_code == 403
    r = client.post(
        "/v1/staff/shift-requests",
        headers=h,
        json={"kind": "early_leave", "date": day.isoformat(), "new_end": "11:00", "reason": "врач"},
    )
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["staff_id"] == str(world.barber1.id) and [c["id"] for c in req["conflicts"]] == [own["id"]]
    assert (
        client.post(
            f"/v1/staff/shift-requests/{req['id']}/decision", headers=h, json={"approve": True}
        ).status_code
        == 403
    )
    r = client.post(
        f"/v1/staff/shift-requests/{req['id']}/decision",
        headers=world.headers(world.admin),
        json={"approve": True, "comment": "ок"},
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    listed = client.get("/v1/staff/shift-requests", headers=h, params={"status": "approved"}).json()
    assert listed["total"] == 1
    # staff cannot read the client base
    assert client.get("/v1/contacts", headers=h).status_code == 403


def test_tenant_isolation_idor(client, world, other_world):
    b = _book(client, world, at(future_day(), 12)).json()
    h = other_world.headers()
    assert client.get(f"/v1/bookings/{b['id']}", headers=h).status_code == 404
    assert client.patch(f"/v1/bookings/{b['id']}", headers=h, json={"action": "cancel"}).status_code == 404
    assert client.get(f"/v1/bookings/{b['id']}/history", headers=h).status_code == 404
    assert client.get(f"/v1/contacts/{world.contact.id}", headers=h).status_code == 404
    assert client.get("/v1/bookings", headers=h).json()["total"] == 0
    assert client.get(f"/v1/staff/{world.barber1.id}", headers=h).status_code == 404
    r = client.post(
        "/v1/bookings",
        headers=h,
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "start_at": at(future_day(), 15).isoformat(),
        },
    )
    assert r.status_code == 404
    # tenant_id in the body is ignored entirely
    r = client.post(
        "/v1/contacts",
        headers=h,
        json={"name": "X", "phone": "+77019990000", "tenant_id": str(world.tenant.id)},
    )
    assert r.status_code == 201
    assert (
        client.get("/v1/contacts", headers=world.headers(), params={"q": "+77019990000"}).json()["total"] == 0
    )


def test_analyst_read_only_and_masked(client, world):
    _book(client, world, at(future_day(), 12))
    h = world.headers(world.analyst)
    items = client.get("/v1/bookings", headers=h).json()["items"]
    assert "*" in items[0]["contact"]["phone_e164"]
    assert _book(client, world, at(future_day(), 15), user=world.analyst).status_code == 403
    assert client.get("/v1/contacts", headers=h).json()["items"][0]["phone_e164"].count("*") > 0


def test_catalogue_staff_and_schedule_endpoints(client, world):
    h = world.headers()
    r = client.post("/v1/branches", headers=h, json={"name": "Астана", "timezone": "Asia/Almaty"})
    assert r.status_code == 201
    branch_id = r.json()["id"]
    assert (
        client.post("/v1/branches", headers=h, json={"name": "x", "timezone": "Mars/Base"}).status_code == 422
    )
    assert client.get("/v1/branches", headers=h).json()["total"] == 2
    assert (
        client.post("/v1/branches", headers=world.headers(world.admin), json={"name": "y"}).status_code == 403
    )

    r = client.post(
        "/v1/staff",
        headers=h,
        json={"branch_id": branch_id, "name": "Дамир", "service_ids": [str(world.haircut.id)]},
    )
    assert r.status_code == 201, r.text
    staff_id = r.json()["id"]
    shifts = [
        {
            "weekday": d,
            "start_time": "09:00",
            "end_time": "18:00",
            "breaks": [{"start": "13:00", "end": "14:00"}],
        }
        for d in range(7)
    ]
    r = client.put(f"/v1/staff/{staff_id}/shifts", headers=h, json=shifts)
    assert r.status_code == 200 and r.json()[0]["start_time"] == "09:00"
    bad = [{"weekday": 0, "start_time": "18:00", "end_time": "09:00"}]
    assert client.put(f"/v1/staff/{staff_id}/shifts", headers=h, json=bad).status_code == 422

    day = future_day()
    r = client.post(
        f"/v1/staff/{staff_id}/exceptions",
        headers=h,
        json={"date": day.isoformat(), "kind": "late_start", "start_time": "11:00"},
    )
    assert r.status_code == 201 and r.json()["conflicts"] == []
    sched = client.get(
        f"/v1/staff/{staff_id}/schedule", headers=h, params={"date_from": day.isoformat()}
    ).json()
    assert sched["timezone"] == "Asia/Almaty"
    assert [
        (local_hhmm(datetime.fromisoformat(i["start"])), local_hhmm(datetime.fromisoformat(i["end"])))
        for i in sched["days"][0]["intervals"]
    ] == [("11:00", "13:00"), ("14:00", "18:00")]
    exc_id = sched["days"][0]["exceptions"][0]["id"]
    assert client.delete(f"/v1/staff/{staff_id}/exceptions/{exc_id}", headers=h).status_code == 204

    r = client.post(
        "/v1/services",
        headers=h,
        json={
            "title_ru": "Детская стрижка",
            "title_kk": "Балалар",
            "duration_min": 40,
            "price": "3500",
            "staff_ids": [staff_id],
        },
    )
    assert r.status_code == 201 and r.json()["staff_ids"] == [staff_id]
    sid = r.json()["id"]
    assert client.get("/v1/services", headers=h, params={"branch_id": branch_id}).json()["total"] == 2
    r = client.patch(f"/v1/services/{sid}", headers=h, json={"price": "4000", "published": False})
    assert r.json()["price"] == "4000.00" and r.json()["published"] is False
    assert client.delete(f"/v1/services/{sid}", headers=h).status_code == 204
    assert client.get("/v1/services", headers=h, params={"q": "Детская"}).json()["total"] == 0


def test_contacts_crm_endpoints(client, world, db):
    h = world.headers()
    r = client.post("/v1/contacts", headers=h, json={"name": "Асель", "phone": "+7 702 111 22 33"})
    assert r.status_code == 201
    cid = r.json()["id"]
    dup = client.post("/v1/contacts", headers=h, json={"name": "Асель 2", "phone": "+77021112233"})
    assert dup.status_code == 409 and dup.json()["detail"]["existing_id"] == cid
    r = client.patch(f"/v1/contacts/{cid}", headers=h, json={"tags": ["VIP"], "opted_out": True})
    assert r.json()["tags"] == ["VIP"] and r.json()["opted_out"] is True
    assert client.get("/v1/contacts", headers=h, params={"tag": "VIP"}).json()["total"] == 1
    assert client.get("/v1/contacts", headers=h, params={"q": "1112233"}).json()["total"] == 1
    assert (
        client.post(f"/v1/contacts/{cid}/notes", headers=h, json={"text": "звонить после 18"}).status_code
        == 201
    )
    timeline = client.get(f"/v1/contacts/{cid}/timeline", headers=h).json()
    assert timeline["items"][0]["kind"] == "note" and timeline["items"][0]["text"] == "звонить после 18"

    r = client.post(f"/v1/contacts/{world.contact.id}/merge", headers=h, json={"other_id": cid})
    assert r.status_code == 200 and "VIP" in r.json()["tags"]
    assert client.get(f"/v1/contacts/{cid}", headers=h).json()["id"] == str(world.contact.id)
    assert client.get("/v1/contacts", headers=h).json()["total"] == 1

    # CSV export: owner only, audited
    assert client.get("/v1/contacts/export.csv", headers=world.headers(world.admin)).status_code == 403
    r = client.get("/v1/contacts/export.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "+77071234567" in r.text and "Нурлан" in r.text
    assert db.scalar(select(AuditLog).where(AuditLog.action == "contact.export")).payload["count"] == 1


def test_leads_endpoints(client, world):
    _book(client, world, at(future_day(), 12))
    h = world.headers()
    leads = client.get("/v1/leads", headers=h, params={"contact_id": str(world.contact.id)}).json()
    assert leads["total"] == 1
    lead = leads["items"][0]
    assert lead["stage"] == "booked" and lead["contact"]["id"] == str(world.contact.id)
    r = client.patch(
        f"/v1/leads/{lead['id']}",
        headers=h,
        json={"stage": "lost", "lost_reason": "дорого", "tags": ["спросил цену"]},
    )
    assert r.status_code == 200 and r.json()["stage"] == "lost" and r.json()["lost_reason"] == "дорого"
    funnel = client.get("/v1/leads/funnel", headers=h).json()
    counts = {i["stage"]: i["count"] for i in funnel["items"]}
    assert counts["lost"] == 1 and counts["booked"] == 0 and next(iter(counts)) == "new"
    assert client.patch(f"/v1/leads/{lead['id']}", headers=h, json={"stage": "nope"}).status_code == 422
    assert client.get("/v1/leads", headers=world.headers(world.staff_user)).status_code == 403


def test_booking_rows_scoped_to_tenant_in_db(db, world, client):
    """Sanity: rows created through the API carry the caller's tenant id."""
    _book(client, world, at(future_day(), 12))
    assert {b.tenant_id for b in db.scalars(select(Booking))} == {world.tenant.id}
