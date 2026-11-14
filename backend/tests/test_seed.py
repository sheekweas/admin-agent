"""Demo seed: realistic, constraint-safe and idempotent."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import Booking, Lead, Membership, OutboxEvent, Visit
from app.seed import TENANT_NAME, seed
from app.services import booking as booking_svc


def test_seed_is_idempotent_and_consistent(db):
    demo = seed(db)
    db.commit()
    assert demo is not None and demo.tenant.name == TENANT_NAME
    assert len(demo.branches) == 2 and len(demo.staff) == 5 and len(demo.services) == 8
    assert len(demo.contacts) == 40
    assert 90 <= len(demo.bookings) <= 180
    statuses = {b.status for b in demo.bookings}
    assert {"completed", "confirmed", "cancelled", "no_show"} <= statuses
    assert db.scalar(select(func.count()).select_from(Visit)) == sum(
        b.status == "completed" for b in demo.bookings
    )
    stages = set(db.scalars(select(Lead.stage)))
    assert {
        "new",
        "info_requested",
        "qualified",
        "booked",
        "visited",
        "lost",
        "cancelled",
        "repeat",
    } <= stages
    assert db.scalar(select(func.count()).select_from(Membership)) == 5
    assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0  # seeding never notifies anyone
    # finance & knowledge demo data
    from app.models import KnowledgeArticle, Payment

    assert db.scalar(select(func.count()).select_from(Payment).where(Payment.kind == "payment")) > 0
    assert db.scalar(select(func.count()).select_from(Payment).where(Payment.kind == "refund")) == 1
    assert (
        db.scalar(
            select(func.count()).select_from(KnowledgeArticle).where(KnowledgeArticle.status == "published")
        )
        == 7
    )
    numbers = list(db.scalars(select(Booking.number)))
    assert len(numbers) == len(set(numbers))

    assert seed(db) is None  # second run is a no-op

    # the booking engine works on top of the demo data
    service = demo.services[0]
    slots = booking_svc.get_available_slots(
        db,
        demo.tenant.id,
        service_id=service.id,
        date_from=demo.bookings[-1].start_at.date(),
        date_to=demo.bookings[-1].start_at.date(),
    )
    assert slots
