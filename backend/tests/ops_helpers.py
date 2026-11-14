"""Helpers for ops tests (escalation, notifications, finance) — independent of the booking engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.channels import telegram, voice
from app.models import Booking, BookingItem, Conversation, Lead, Visit
from app.services.common import next_number

# Monday 2026-11-16 06:00 UTC = 11:00 in Asia/Almaty (UTC+5): inside working hours, outside quiet hours.
T0 = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)


def reset_mocks() -> None:
    telegram.reset_mock()
    voice.MOCK_CALLS.clear()


def tg_texts(chat_id=None) -> list[str]:
    return [m["text"] for m in telegram.mock_messages(chat_id)]


def make_conversation(db, world, *, mode: str = "ai", language: str = "ru") -> Conversation:
    conv = Conversation(
        tenant_id=world.tenant.id,
        contact_id=world.contact.id,
        branch_id=world.branch.id,
        channel="whatsapp",
        mode=mode,
        language=language,
        last_message_at=T0,
    )
    db.add(conv)
    db.commit()
    return conv


def make_booking(
    db,
    world,
    *,
    start: datetime,
    status: str = "confirmed",
    staff=None,
    service=None,
    price: str = "5000",
    source: str = "admin",
    contact=None,
    minutes: int = 60,
    created_at: datetime = T0,
) -> Booking:
    staff = staff or world.barber1
    service = service or world.haircut
    end = start + timedelta(minutes=minutes)
    b = Booking(
        tenant_id=world.tenant.id,
        number=next_number(db, world.tenant.id, "booking"),
        branch_id=world.branch.id,
        contact_id=(contact or world.contact).id,
        staff_id=staff.id,
        start_at=start,
        end_at=end,
        occupied_start=start,
        occupied_end=end,
        status=status,
        channel="admin",
        source=source,
        total_price=Decimal(price),
        created_at=created_at,
    )
    b.items.append(
        BookingItem(service_id=service.id, price_snapshot=Decimal(price), duration_snapshot=minutes)
    )
    db.add(b)
    db.commit()
    return b


def make_visit(db, world, booking: Booking) -> Visit:
    v = Visit(
        tenant_id=world.tenant.id,
        booking_id=booking.id,
        contact_id=booking.contact_id,
        staff_id=booking.staff_id,
        branch_id=booking.branch_id,
        visited_at=booking.start_at,
        amount_due=booking.total_price,
    )
    db.add(v)
    db.commit()
    return v


def make_lead(
    db, world, *, stage: str = "qualified", source: str = "ads", created_at: datetime = T0, contact=None
) -> Lead:
    lead = Lead(
        tenant_id=world.tenant.id,
        contact_id=(contact or world.contact).id,
        branch_id=world.branch.id,
        source=source,
        stage=stage,
        created_at=created_at,
    )
    db.add(lead)
    db.commit()
    return lead


class SentToClient:
    """Recorder replacing app.services.messaging.send_client_message."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, db, tenant_id, conversation_id, text, *, author="ai", author_user_id=None, **kw):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "text": text,
                "author": author,
                "author_user_id": author_user_id,
            }
        )

        class _Msg:
            id = f"msg-{len(self.calls)}"

        return _Msg()
