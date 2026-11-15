"""Data model (ТЗ §17). PostgreSQL is the source of truth.

Every tenant-owned row carries tenant_id. All service-layer queries MUST filter by tenant_id
taken from the authenticated context (never from the request body).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
    func,
    or_,
    update,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm import Session as OrmSession

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------- enums (stored as strings)
class Role(str, enum.Enum):
    superadmin = "superadmin"
    owner = "owner"
    admin = "admin"
    staff = "staff"
    analyst = "analyst"


class BookingStatus(str, enum.Enum):
    held = "held"
    pending = "pending"
    confirmed = "confirmed"
    checked_in = "checked_in"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"
    rescheduled = "rescheduled"


# Statuses that occupy a slot (used in the DB exclusion constraint and availability).
ACTIVE_BOOKING_STATUSES = ("held", "pending", "confirmed", "checked_in")


class LeadStage(str, enum.Enum):
    new = "new"
    info_requested = "info_requested"
    qualified = "qualified"
    booking_pending = "booking_pending"
    booked = "booked"
    visited = "visited"
    lost = "lost"
    cancelled = "cancelled"
    repeat = "repeat"


class ConversationMode(str, enum.Enum):
    ai = "ai"  # AI answers
    human = "human"  # human took over; AI silent until explicit return
    waiting_admin = "waiting_admin"  # AI escalated, waits for admin answer


class TaskStatus(str, enum.Enum):
    open = "open"
    acked = "acked"
    resolved = "resolved"
    expired = "expired"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    refunded = "refunded"


class ShiftRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


# ---------------------------------------------------------------- tenancy
class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    niche: Mapped[str] = mapped_column(String(50), default="barbershop")  # barbershop|beauty|dental|other
    plan: Mapped[str] = mapped_column(String(50), default="pilot")
    status: Mapped[str] = mapped_column(String(20), default="active")
    default_language: Mapped[str] = mapped_column(String(5), default="ru")  # ru|kk
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    # Business rules: slot_step_min, hold_ttl_min, cancel_deadline_hours, reminder_hours [24,2],
    # escalation {repeat_after_min:5, call_after_min:10, max_call_attempts:2, quiet_hours:[22,8], sla_min:30},
    # ai {enabled, tone, forbidden_topics, allow_other_branch, allow_other_staff}, requires_admin_confirmation
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)


class Branch(TimestampMixin, Base):
    __tablename__ = "branches"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(500), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    contacts: Mapped[dict] = mapped_column(JSONType, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    ui_language: Mapped[str] = mapped_column(String(5), default="ru")
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))  # Role
    branch_scope: Mapped[list] = mapped_column(JSONType, default=list)  # [] = all branches
    is_escalation_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_escalation_backup: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="memberships")


# ---------------------------------------------------------------- catalogue & staff
class Service(TimestampMixin, Base):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    category: Mapped[str] = mapped_column(String(100), default="")
    title_ru: Mapped[str] = mapped_column(String(200))
    title_kk: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    duration_min: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    buffer_before_min: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after_min: Mapped[int] = mapped_column(Integer, default=0)
    resource_type: Mapped[str | None] = mapped_column(String(50))  # e.g. "chair", "cabinet"
    published: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Staff(TimestampMixin, Base):
    __tablename__ = "staff"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    specialization: Mapped[str] = mapped_column(String(200), default="")
    color: Mapped[str] = mapped_column(String(9), default="#4f46e5")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffService(Base):
    """Which staff performs which service, with optional per-staff duration/price override."""

    __tablename__ = "staff_services"
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    duration_min: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class Resource(TimestampMixin, Base):
    __tablename__ = "resources"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Shift(TimestampMixin, Base):
    """Recurring weekly working hours (weekday 0=Mon) in branch local time."""

    __tablename__ = "shifts"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    breaks: Mapped[list] = mapped_column(JSONType, default=list)  # [{"start":"13:00","end":"14:00"}]
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


class ScheduleException(TimestampMixin, Base):
    """Date-specific override: day off, vacation, sick, early leave, late start, overtime, block."""

    __tablename__ = "schedule_exceptions"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    staff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("staff.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(
        String(30)
    )  # day_off|vacation|sick|early_leave|late_start|overtime|block
    start_time: Mapped[time | None] = mapped_column(
        Time
    )  # new working start (late_start/overtime) or block start
    end_time: Mapped[time | None] = mapped_column(Time)  # new working end (early_leave/overtime) or block end
    reason: Mapped[str] = mapped_column(String(500), default="")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class ShiftRequest(TimestampMixin, Base):
    __tablename__ = "shift_requests"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("staff.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # same kinds as ScheduleException
    date: Mapped[date] = mapped_column(Date)
    old_start: Mapped[time | None] = mapped_column(Time)
    old_end: Mapped[time | None] = mapped_column(Time)
    new_start: Mapped[time | None] = mapped_column(Time)
    new_end: Mapped[time | None] = mapped_column(Time)
    reason: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    conflicts: Mapped[list] = mapped_column(JSONType, default=list)  # booking ids computed on create/decision
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str] = mapped_column(String(500), default="")


# ---------------------------------------------------------------- CRM
class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "phone_e164", name="uq_contact_tenant_phone"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200), default="")
    language: Mapped[str] = mapped_column(String(5), default="ru")
    source: Mapped[str] = mapped_column(
        String(30), default="whatsapp"
    )  # ads|site|qr|repeat|call|whatsapp|manual
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    preferences: Mapped[dict] = mapped_column(JSONType, default=dict)  # {"staff_id": ...}
    # {"processing": true, "marketing": false, "outbound_calls": false, "call_recording": false}
    consent_flags: Mapped[dict] = mapped_column(JSONType, default=dict)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContactChannel(Base):
    """External identity per channel (WhatsApp wa_id, Telegram id, phone) -> contact."""

    __tablename__ = "contact_channels"
    __table_args__ = (UniqueConstraint("tenant_id", "channel", "external_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(100))


class ContactNote(TimestampMixin, Base):
    __tablename__ = "contact_notes"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)


class Lead(TimestampMixin, Base):
    """One lead/opportunity per significant intent (ТЗ §10.2)."""

    __tablename__ = "leads"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id"))
    source: Mapped[str] = mapped_column(String(30), default="whatsapp")
    stage: Mapped[str] = mapped_column(String(30), default="new", index=True)
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    lost_reason: Mapped[str] = mapped_column(String(500), default="")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- conversations
class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    channel: Mapped[str] = mapped_column(String(20), default="whatsapp")
    mode: Mapped[str] = mapped_column(String(20), default="ai")  # ConversationMode
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|closed
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    language: Mapped[str] = mapped_column(String(5), default="ru")
    state: Mapped[dict] = mapped_column(JSONType, default=dict)  # dialogue slots: service, date, staff ...
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("tenant_id", "provider_message_id", name="uq_message_provider_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(10))  # in|out
    author: Mapped[str] = mapped_column(String(10), default="client")  # client|ai|human|system
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    message_type: Mapped[str] = mapped_column(String(20), default="text")  # text|audio|image|document|note
    text: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str | None] = mapped_column(String(1000))
    language: Mapped[str | None] = mapped_column(String(5))
    is_internal_note: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_status: Mapped[str] = mapped_column(
        String(20), default="received"
    )  # received|queued|sent|delivered|read|failed
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)


class AIRun(Base):
    """Audit of each AI turn: prompt version, knowledge used, tool calls (ТЗ §7.3)."""

    __tablename__ = "ai_runs"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(60))
    prompt_version: Mapped[str] = mapped_column(String(30))
    knowledge_article_ids: Mapped[list] = mapped_column(JSONType, default=list)
    tool_calls: Mapped[list] = mapped_column(JSONType, default=list)
    reply_text: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeArticle(TimestampMixin, Base):
    __tablename__ = "knowledge_articles"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    section: Mapped[str] = mapped_column(
        String(30), default="faq"
    )  # company|branches|services|prices|staff|promo|rules|faq|scripts
    language: Mapped[str] = mapped_column(String(5), default="ru")
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|published|archived
    version: Mapped[int] = mapped_column(Integer, default=1)
    author_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    test_questions: Mapped[list] = mapped_column(JSONType, default=list)


# ---------------------------------------------------------------- bookings
class Booking(TimestampMixin, Base):
    """Overlap protection: see migration — EXCLUDE USING gist (staff_id WITH =, tstzrange(start_at, end_at)
    WITH &&) WHERE status in ACTIVE_BOOKING_STATUSES. start_at/end_at include buffers? No:
    occupied_start/occupied_end include buffers and are what the constraint uses."""

    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_booking_idempotency"),
        Index("ix_bookings_tenant_start", "tenant_id", "start_at"),
        Index("ix_bookings_staff_start", "staff_id", "start_at"),
        Index("ix_bookings_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[int] = mapped_column(Integer, index=True)  # human readable BK-000123 per tenant
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("staff.id"))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resources.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occupied_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occupied_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(20), default="admin")  # whatsapp|admin|phone|api
    source: Mapped[str] = mapped_column(String(30), default="admin")  # ai_agent|admin|integration
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id")
    )
    rescheduled_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    cancel_reason: Mapped[str] = mapped_column(String(500), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    items: Mapped[list[BookingItem]] = relationship(back_populates="booking", cascade="all, delete-orphan")


class BookingItem(Base):
    __tablename__ = "booking_items"
    id: Mapped[uuid.UUID] = uuid_pk()
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id"))
    price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    duration_snapshot: Mapped[int] = mapped_column(Integer)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)

    booking: Mapped[Booking] = relationship(back_populates="items")


class Visit(TimestampMixin, Base):
    """Actually rendered service (ТЗ §14.1). Created on check-in/complete."""

    __tablename__ = "visits"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("staff.id"))
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    comment: Mapped[str] = mapped_column(Text, default="")


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), index=True
    )
    visit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("visits.id"), index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    staff_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("staff.id"))
    kind: Mapped[str] = mapped_column(String(20), default="payment")  # payment|prepayment|refund|adjustment
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # refunds stored positive with kind=refund
    currency: Mapped[str] = mapped_column(String(3), default="KZT")
    method: Mapped[str] = mapped_column(String(20), default="cash")  # cash|card|transfer|online
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    confirmation_source: Mapped[str] = mapped_column(String(30), default="admin")  # admin|cashbox|provider
    transaction_ref: Mapped[str | None] = mapped_column(String(200))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    comment: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    refund_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("payments.id"))


# ---------------------------------------------------------------- admin tasks & notifications
class AdminTask(TimestampMixin, Base):
    __tablename__ = "admin_tasks"
    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[int] = mapped_column(Integer)
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))
    kind: Mapped[str] = mapped_column(
        String(30), default="question"
    )  # question|complaint|schedule_conflict|callback|delivery_failed
    question: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low|normal|high|urgent
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    escalation_level: Mapped[int] = mapped_column(
        Integer, default=0
    )  # 0 sent, 1 repeat, 2 call, 3 backup call
    next_escalation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer: Mapped[str] = mapped_column(Text, default="")
    add_to_knowledge: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)


class Notification(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_dedupe"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    recipient: Mapped[str] = mapped_column(String(200))  # chat id / phone / url
    channel: Mapped[str] = mapped_column(String(20))  # telegram|whatsapp|voice|webhook
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|failed|cancelled|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)


class Call(TimestampMixin, Base):
    __tablename__ = "calls"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    direction: Mapped[str] = mapped_column(String(10))  # in|out
    # admin_escalation|client_ai|booking_confirmation|reminder|callback|reschedule
    purpose: Mapped[str] = mapped_column(String(30))
    to_number: Mapped[str] = mapped_column(String(30))
    from_number: Mapped[str | None] = mapped_column(String(30))
    admin_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("admin_tasks.id"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))
    status: Mapped[str] = mapped_column(
        String(20), default="queued"
    )  # queued|ringing|in_progress|answered|completed|no_answer|busy|failed|cancelled
    dtmf: Mapped[str | None] = mapped_column(String(10))
    provider_ref: Mapped[str | None] = mapped_column(String(200), index=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    transcript: Mapped[str] = mapped_column(Text, default="")  # plain text, one "speaker: text" line per turn
    recording_url: Mapped[str | None] = mapped_column(String(1000))
    # --- AI voice channel (stage 4, additive; see alembic 0002_voice_calls)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id"))
    language: Mapped[str | None] = mapped_column(String(5))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # answered
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # [{"speaker": "client"|"ai"|"admin"|"system", "text", "at", "confidence"}]
    transcript_items: Mapped[list] = mapped_column(JSONType, default=list)
    # {first_response_ms, turns, recognition_retries, transferred_to_human, booking_created}
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)  # turn-loop state (seq, failures, last TwiML)


# ---------------------------------------------------------------- integrations, outbox, audit
class Integration(TimestampMixin, Base):
    """Per-tenant channel config. secrets are stored encrypted (see app.security.encrypt_secret)."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "external_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"))
    kind: Mapped[str] = mapped_column(String(30))  # whatsapp|telegram|voice|zapier
    external_id: Mapped[str] = mapped_column(String(200), default="")  # WA phone_number_id, TG bot id...
    config: Mapped[dict] = mapped_column(JSONType, default=dict)  # non-secret config
    secrets_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # ok|error|unknown
    last_error: Mapped[str | None] = mapped_column(Text)


class WebhookSubscription(TimestampMixin, Base):
    __tablename__ = "webhook_subscriptions"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    url: Mapped[str] = mapped_column(String(1000))
    events: Mapped[list] = mapped_column(JSONType, default=list)
    secret: Mapped[str] = mapped_column(String(200))
    include_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class OutboxEvent(Base):
    """Transactional outbox (ТЗ §18.2). Written in the same transaction as the state change."""

    __tablename__ = "outbox_events"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = tenant_fk()
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|processed|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # retry backoff (ops)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # user|ai|system|integration|staff
    actor_id: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Counter(Base):
    """Per-tenant human-readable sequence numbers (bookings, tasks)."""

    __tablename__ = "counters"
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- auth sessions (v1.1.0, migration 0003)
class AuthSession(Base):
    """Server-side login session; the JWT carries its id as ``sid`` and is valid only while the row is active
    (not revoked, not expired). Looked up by primary key on every authenticated request."""

    __tablename__ = "auth_sessions"
    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


def _revoke_sessions_on_deactivation(session, flush_context) -> None:
    """Deactivating a user revokes all their sessions; deactivating a membership revokes that user's sessions
    issued for that tenant. Runs for every code path (API, CLI, scripts) that flips ``active`` to False."""
    table = AuthSession.__table__
    conds = []
    for obj in session.dirty:
        if not isinstance(obj, User | Membership):
            continue
        hist = sa_inspect(obj).attrs.active.history
        if not hist.added or hist.added[0] is not False:
            continue
        if isinstance(obj, User):
            conds.append(table.c.user_id == obj.id)
        else:
            conds.append((table.c.user_id == obj.user_id) & (table.c.tenant_id == obj.tenant_id))
    if conds:
        session.connection().execute(
            update(table).where(table.c.revoked_at.is_(None), or_(*conds)).values(revoked_at=func.now())
        )


if not event.contains(OrmSession, "after_flush", _revoke_sessions_on_deactivation):
    event.listen(OrmSession, "after_flush", _revoke_sessions_on_deactivation)
