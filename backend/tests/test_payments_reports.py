"""Finance & reports (ТЗ §14): AT-08, AT-14, formulas, local dates, permissions, export."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import AIRun, AuditLog, Contact, Message, OutboxEvent, Payment
from app.services import payments as pay
from app.services.common import DomainError
from app.services.reports import ReportFilter, available_minutes, overview
from tests.ops_helpers import T0, make_booking, make_conversation, make_lead, make_visit

DAY = date(2026, 11, 16)  # T0 local date (Almaty)


def _ov(db, world, d0=DAY, d1=DAY, **kw) -> dict:
    return overview(db, world.tenant.id, ReportFilter(d0, d1, **kw))


def test_at08_confirmed_booking_without_payment_has_zero_revenue(db, world):
    make_booking(db, world, start=T0)
    k = _ov(db, world)["kpi"]
    assert k["bookings"] == 1 and k["revenue"] == "0.00" and k["net_revenue"] == "0.00"


def test_partial_and_multiple_payments_then_refund_at14(db, world):
    b = make_booking(db, world, start=T0, status="completed")
    p1 = pay.confirm_payment(
        db,
        world.tenant.id,
        booking_id=b.id,
        amount="2000",
        kind="prepayment",
        method="online",
        paid_at=T0,
        actor_id=world.admin.id,
    )
    pay.confirm_payment(db, world.tenant.id, booking_id=b.id, amount=Decimal(3000), method="cash", paid_at=T0)
    db.commit()
    assert pay.booking_paid_amount(db, world.tenant.id, b.id) == Decimal(5000)
    k = _ov(db, world)["kpi"]
    assert (k["revenue"], k["avg_check"], k["unpaid_completed"]) == ("5000.00", "5000.00", 0)

    with pytest.raises(DomainError) as e:
        pay.refund_payment(db, world.tenant.id, p1.id, amount="2500", reason="too much")
    assert e.value.code == "REFUND_EXCEEDS_PAID"
    pay.refund_payment(
        db, world.tenant.id, p1.id, amount="500", reason="discount", paid_at=T0 + timedelta(days=1)
    )
    db.commit()
    # refund is dated by its own day: day 1 unchanged, day 2 negative, period net recalculated
    assert _ov(db, world)["kpi"]["net_revenue"] == "5000.00"
    r = _ov(db, world, DAY, DAY + timedelta(days=1))
    assert (r["kpi"]["revenue"], r["kpi"]["refunds"], r["kpi"]["net_revenue"]) == (
        "5000.00",
        "500.00",
        "4500.00",
    )
    assert [d["amount"] for d in r["revenue_by_day"]] == ["5000.00", "-500.00"]
    assert r["kpi"]["unpaid_completed"] == 1  # 4500 < 5000
    refund = pay.refund_payment(db, world.tenant.id, p1.id, amount="1500", reason="rest", paid_at=T0)
    db.commit()
    db.refresh(p1)
    assert p1.status == "refunded"
    # cancelling an erroneous refund restores the payment
    pay.cancel_payment(db, world.tenant.id, refund.id)
    db.commit()
    db.refresh(p1)
    assert p1.status == "confirmed"
    with pytest.raises(DomainError):
        pay.cancel_payment(db, world.tenant.id, p1.id)  # still has an active 500 refund
    events = [e.event_type for e in db.query(OutboxEvent).order_by(OutboxEvent.created_at)]
    assert events.count("payment.confirmed") == 2 and events.count("payment.refunded") == 2
    actions = {a.action for a in db.query(AuditLog).all()}
    assert {"finance.payment.confirmed", "finance.payment.refunded", "finance.payment.cancelled"} <= actions


def test_cancelled_payment_is_excluded_and_transaction_ref_idempotent(db, world):
    b = make_booking(db, world, start=T0)
    p = pay.confirm_payment(
        db, world.tenant.id, booking_id=b.id, amount="1000", transaction_ref="kaspi-1", paid_at=T0
    )
    again = pay.confirm_payment(
        db, world.tenant.id, booking_id=b.id, amount="1000", transaction_ref="kaspi-1"
    )
    assert again.id == p.id
    pay.cancel_payment(db, world.tenant.id, p.id, reason="typo")
    db.commit()
    assert _ov(db, world)["kpi"]["revenue"] == "0.00"


def test_revenue_uses_branch_local_date(db, world):
    b = make_booking(db, world, start=T0)
    late = datetime(2026, 11, 15, 20, 30, tzinfo=UTC)  # = 16.11 01:30 in Almaty
    pay.confirm_payment(db, world.tenant.id, booking_id=b.id, amount="700", paid_at=late)
    db.commit()
    assert _ov(db, world)["kpi"]["revenue"] == "700.00"
    assert _ov(db, world, DAY - timedelta(days=1), DAY - timedelta(days=1))["kpi"]["revenue"] == "0.00"


def test_kpi_formulas(db, world):
    other = Contact(tenant_id=world.tenant.id, phone_e164="+77070000009", name="Ая", source="ads")
    db.add(other)
    db.commit()
    b1 = make_booking(db, world, start=T0, status="completed")
    make_visit(db, world, b1)
    make_booking(db, world, start=T0 + timedelta(hours=2), status="no_show", staff=world.barber2)
    make_booking(
        db,
        world,
        start=T0 + timedelta(hours=3),
        status="confirmed",
        service=world.beard,
        price="3000",
        minutes=30,
    )
    make_booking(db, world, start=T0 + timedelta(hours=4), status="cancelled", contact=other)
    for st in ("qualified", "booked", "new"):
        make_lead(db, world, stage=st, contact=other if st == "new" else None)
    make_lead(db, world, stage="booked", contact=other, source="site")
    pay.confirm_payment(db, world.tenant.id, booking_id=b1.id, amount="5000", paid_at=T0)
    # AI conversations: one solved by AI alone, one with a human reply
    for author in ("ai", "human"):
        conv = make_conversation(db, world)
        db.add(
            AIRun(
                tenant_id=world.tenant.id,
                conversation_id=conv.id,
                provider="mock",
                model="m",
                prompt_version="v1",
            )
        )
        db.add(
            Message(
                tenant_id=world.tenant.id, conversation_id=conv.id, direction="out", author=author, text="x"
            )
        )
    db.commit()
    r = _ov(db, world)
    k = r["kpi"]
    assert (k["bookings"], k["visits"], k["no_shows"], k["cancellations"]) == (3, 1, 1, 1)
    assert k["no_show_rate"] == 33.3 and k["conversion_to_visit"] == 33.3
    # 2 unique contacts with qualified+ leads, both of them converted into a booking (spec §14.3, bounded)
    assert k["new_leads"] == 4 and k["conversion_to_booking"] == 100.0
    assert k["ai_resolution_rate"] == 50.0
    assert {s["source"]: s["count"] for s in r["lead_sources"]} == {"ads": 3, "site": 1}
    funnel = {f["stage"]: f["count"] for f in r["funnel"]}
    assert funnel["booked"] == 2 and funnel["new"] == 1 and funnel["visited"] == 0
    assert r["load_by_hour"][11]["bookings"] == 1 and len(r["load_by_hour"]) == 24
    top = {s["title"]: s for s in r["top_services"]}
    assert top["Мужская стрижка"]["revenue"] == "5000.00" and top["Мужская стрижка"]["count"] == 2
    assert top["Борода"]["revenue"] == "0.00" and top["Борода"]["count"] == 1
    staff = {s["name"]: s for s in r["top_staff"]}
    # barber1: 60 (completed) + 30 (confirmed) booked minutes / 600 working minutes (10-21 minus 1h break)
    assert staff["Арман"]["utilization"] == 15.0 and staff["Арман"]["visits"] == 1
    assert staff["Арман"]["revenue"] == "5000.00"
    # filters
    assert _ov(db, world, staff_id=world.barber2.id)["kpi"]["bookings"] == 1
    assert _ov(db, world, service_id=world.beard.id)["kpi"]["bookings"] == 1
    assert _ov(db, world, branch_ids=[])["kpi"]["bookings"] == 0


def test_service_revenue_is_allocated_by_line_price(db, world):
    from app.models import BookingItem

    b = make_booking(db, world, start=T0, price="5000")
    db.add(
        BookingItem(
            booking_id=b.id, service_id=world.beard.id, price_snapshot=Decimal(3000), duration_snapshot=30
        )
    )
    db.commit()
    pay.confirm_payment(db, world.tenant.id, booking_id=b.id, amount="4000", paid_at=T0)  # partial payment
    db.commit()
    top = {s["title"]: s["revenue"] for s in _ov(db, world)["top_services"]}
    assert top == {"Мужская стрижка": "2500.00", "Борода": "1500.00"}


def test_available_minutes_with_exceptions(db, world):
    from app.models import ScheduleException, Shift

    shifts = db.query(Shift).filter_by(staff_id=world.barber1.id).all()
    assert available_minutes(shifts, [], [DAY]) == 600
    early = ScheduleException(
        staff_id=world.barber1.id, date=DAY, kind="early_leave", end_time=datetime(2000, 1, 1, 18).time()
    )
    assert available_minutes(shifts, [early], [DAY]) == 420
    off = ScheduleException(staff_id=world.barber1.id, date=DAY, kind="day_off")
    assert available_minutes(shifts, [off], [DAY, DAY + timedelta(days=1)]) == 600


def test_payments_api_permissions(client, db, world):
    b = make_booking(db, world, start=T0)
    admin, owner = world.headers(world.admin), world.headers(world.owner)
    r = client.post(
        "/v1/payments",
        headers=admin,
        json={"booking_id": str(b.id), "amount": "1500.50", "method": "card", "kind": "payment"},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["amount"] == "1500.50" and r.json()["created_by"] == str(world.admin.id)
    assert client.get(f"/v1/payments?booking_id={b.id}", headers=admin).json()["total"] == 1
    assert client.get("/v1/payments", headers=admin).status_code == 403  # full ledger is finance.read
    assert client.get("/v1/payments", headers=world.headers(world.analyst)).json()["total"] == 1
    assert (
        client.post(
            "/v1/payments",
            headers=world.headers(world.staff_user),
            json={"booking_id": str(b.id), "amount": "1"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/payments/{pid}/refund", headers=admin, json={"amount": "100", "reason": "x"}
        ).status_code
        == 403
    )
    r = client.post(f"/v1/payments/{pid}/refund", headers=owner, json={"amount": "100", "reason": "x"})
    assert r.status_code == 201 and r.json()["kind"] == "refund"
    r = client.post(f"/v1/payments/{pid}/refund", headers=owner, json={"amount": "2000", "reason": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REFUND_EXCEEDS_PAID"
    assert (
        client.post("/v1/payments", headers=admin, json={"booking_id": str(b.id), "amount": "-5"}).status_code
        == 422
    )
    assert db.query(Payment).count() == 2


def test_overview_api_masks_money_for_admin_and_exports(client, db, world):
    b = make_booking(db, world, start=T0)
    pay.confirm_payment(db, world.tenant.id, booking_id=b.id, amount="5000", paid_at=T0)
    db.commit()
    q = "date_from=2026-11-16&date_to=2026-11-17"
    r = client.get(f"/v1/reports/overview?{q}", headers=world.headers(world.owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kpi"]["revenue"] == "5000.00" and len(body["revenue_by_day"]) == 2
    assert set(body) >= {
        "kpi",
        "revenue_by_day",
        "bookings_by_day",
        "top_services",
        "top_staff",
        "lead_sources",
        "funnel",
        "load_by_hour",
        "cancellations_by_day",
    }
    admin = client.get(f"/v1/reports/overview?{q}", headers=world.headers(world.admin)).json()
    assert admin["kpi"]["revenue"] is None and admin["kpi"]["bookings"] == 1
    assert client.get(f"/v1/reports/overview?{q}", headers=world.headers(world.staff_user)).status_code == 403
    csv = client.get(f"/v1/reports/export.csv?{q}", headers=world.headers(world.owner))
    assert csv.status_code == 200 and "Выручка" in csv.content.decode("utf-8-sig")
    xlsx = client.get(f"/v1/reports/export.xlsx?{q}", headers=world.headers(world.analyst))
    assert xlsx.status_code == 200
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx.content))
    assert "KPI" in wb.sheetnames and wb["По дням"]["B2"].value == 5000.0
    assert client.get(f"/v1/reports/export.csv?{q}", headers=world.headers(world.admin)).status_code == 403
