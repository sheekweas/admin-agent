"""Regressions found by frontend integration QA (stage 2)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import Booking, Branch, Contact, Lead, Shift, Staff, StaffService
from app.services import booking as booking_svc
from app.services.reports import ReportFilter, overview

ALMATY = ZoneInfo("Asia/Almaty")


def _tomorrow_at(h: int) -> datetime:
    d = datetime.now(ALMATY).date() + timedelta(days=1)
    return datetime.combine(d, time(h, 0), ALMATY)


def test_conversion_to_booking_never_exceeds_100(world, db):
    # one qualified lead, but several bookings made by the desk without leads
    lead = Lead(tenant_id=world.tenant.id, contact_id=world.contact.id, stage="qualified", source="whatsapp")
    db.add(lead)
    db.commit()
    for i, h in enumerate((11, 12, 13)):
        c = Contact(tenant_id=world.tenant.id, phone_e164=f"+7707000000{i}", name=f"Walk-in {i}")
        db.add(c)
        db.flush()
        booking_svc.create_booking(
            db,
            world.tenant.id,
            contact_id=c.id,
            service_id=world.haircut.id,
            start_at=_tomorrow_at(h),
            staff_id=world.barber1.id,
            idempotency_key=f"walkin-{i}",
            channel="admin",
            source="admin",
            actor_type="user",
            actor_id=str(world.admin.id),
        )
    db.commit()
    today = datetime.now(ALMATY).date()
    kpi = overview(
        db,
        world.tenant.id,
        ReportFilter(date_from=today - timedelta(days=1), date_to=today + timedelta(days=2)),
    )["kpi"]
    assert 0 <= kpi["conversion_to_booking"] <= 100


def _second_branch(world, db):
    br = Branch(tenant_id=world.tenant.id, name="Астана", timezone="Asia/Almaty")
    db.add(br)
    db.flush()
    st = Staff(tenant_id=world.tenant.id, branch_id=br.id, name="Астанинский мастер")
    db.add(st)
    db.flush()
    db.add(StaffService(staff_id=st.id, service_id=world.haircut.id))
    for wd in range(7):
        db.add(
            Shift(
                tenant_id=world.tenant.id, staff_id=st.id, weekday=wd, start_time=time(10), end_time=time(21)
            )
        )
    db.commit()
    return br, st


def _fill_branch(world, db, start):
    for i, st in enumerate((world.barber1, world.barber2)):
        booking_svc.create_booking(
            db,
            world.tenant.id,
            contact_id=world.contact.id,
            service_id=world.haircut.id,
            start_at=start,
            staff_id=st.id,
            idempotency_key=f"fill-{i}",
            channel="admin",
            source="admin",
            actor_type="user",
            actor_id=str(world.admin.id),
        )
    db.commit()


def test_desk_alternatives_stay_in_requested_branch(client, world, db):
    _second_branch(world, db)  # other branch has free slots that must NOT be offered
    start = _tomorrow_at(16)
    _fill_branch(world, db, start)
    r = client.post(
        "/v1/bookings",
        headers={**world.headers(world.admin), "Idempotency-Key": "qa-1"},
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "staff_id": str(world.barber1.id),
            "start_at": start.isoformat(),
            "branch_id": str(world.branch.id),
        },
    )
    assert r.status_code == 409
    alts = r.json()["detail"]["alternatives"]
    assert alts and all(a["branch_id"] == str(world.branch.id) for a in alts)


def test_staff_role_alternatives_only_own(client, world, db):
    start = _tomorrow_at(16)
    _fill_branch(world, db, start)
    r = client.post(
        "/v1/bookings",
        headers={**world.headers(world.staff_user), "Idempotency-Key": "qa-2"},
        json={
            "contact_id": str(world.contact.id),
            "service_id": str(world.haircut.id),
            "start_at": start.isoformat(),
        },
    )
    assert r.status_code == 409
    alts = r.json()["detail"]["alternatives"]
    assert alts and all(a["staff_id"] == str(world.barber1.id) for a in alts)
    assert db.query(Booking).count() == 2


def test_mock_agent_prefers_exact_service_over_combo():
    """Stage-4 QA: "шаш қию" must match "Шаш қию", not the "Шаш қию + сақал" combo."""
    from app.ai.mock_llm import _Turn

    turn = _Turn.__new__(_Turn)
    turn.raw = "ертең шаш қиюға жазылғым келеді"
    turn._services = [
        {"id": "combo", "title": "Шаш қию + сақал"},
        {"id": "cut", "title": "Шаш қию"},
        {"id": "beard", "title": "Сақал"},
    ]
    assert turn.match_service()["id"] == "cut"
    turn.raw = "шаш қию және сақал"
    assert turn.match_service()["id"] == "combo"


def test_mock_agent_asks_when_services_tie():
    """v1.0 QA: "стрижка" fits "Мужская стрижка" and "Стрижка бороды" equally -> ask, never guess a price."""
    from app.ai.mock_llm import _Turn

    turn = _Turn.__new__(_Turn)
    turn.raw = "Сколько стоит стрижка?"
    turn._services = [{"title": "Стрижка бороды"}, {"title": "Мужская стрижка"}]
    assert turn.match_service() is None
    assert {s["title"] for s in turn.ambiguous} == {"Стрижка бороды", "Мужская стрижка"}
    turn.raw = "Сколько стоит мужская стрижка?"
    assert turn.match_service()["title"] == "Мужская стрижка" and turn.ambiguous == []
