"""Shared helpers: outbox events, counters, phone normalization, time zones, errors."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import phonenumbers
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Counter, OutboxEvent

# Integration events (ТЗ §16.2) + internal ones
EVENT_TYPES = {
    "lead.created",
    "lead.status_changed",
    "booking.created",
    "booking.confirmed",
    "booking.cancelled",
    "booking.rescheduled",
    "booking.status_changed",
    "staff.schedule_changed",
    "shift_request.created",
    "shift_request.decided",
    "admin_task.created",
    "admin_task.acked",
    "admin_task.resolved",
    "payment.confirmed",
    "payment.refunded",
    "payment.cancelled",
    "message.delivery_failed",
}


class DomainError(HTTPException):
    """Business error with machine-readable code: {"code": ..., "message": ..., **extra}."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(status_code=status_code, detail={"code": code, "message": message, **extra})
        self.code = code


def utcnow() -> datetime:
    return datetime.now(UTC)


def tz(name: str | None) -> ZoneInfo:
    return ZoneInfo(name or "Asia/Almaty")


def emit_event(
    db: Session,
    tenant_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> OutboxEvent:
    """Write outbox event in the SAME transaction as the state change (ТЗ §18.2)."""
    assert event_type in EVENT_TYPES, event_type
    ev = OutboxEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
    )
    db.add(ev)
    return ev


def next_number(db: Session, tenant_id: uuid.UUID, name: str) -> int:
    """Atomic per-tenant counter (row lock via upsert)."""
    stmt = (
        insert(Counter)
        .values(tenant_id=tenant_id, name=name, value=1)
        .on_conflict_do_update(index_elements=["tenant_id", "name"], set_={"value": Counter.value + 1})
        .returning(Counter.value)
    )
    return int(db.execute(stmt).scalar_one())


def normalize_phone(raw: str | None, default_region: str = "KZ") -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if default_region == "KZ" and not raw.startswith("+") and len(digits) == 11 and digits[0] == "8":
        raw = "+7" + digits[1:]  # KZ/RU domestic format: 8 707 123 45 67
    elif raw and raw[0].isdigit() and len(digits) >= 11:
        raw = "+" + digits  # WhatsApp wa_id comes without '+'
    try:
        num = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def mask_phone(phone: str | None) -> str:
    if not phone or len(phone) < 6:
        return phone or ""
    return phone[:4] + "*" * (len(phone) - 6) + phone[-2:]


def get_or_404(db: Session, model, obj_id, tenant_id: uuid.UUID):
    """Tenant-scoped fetch; foreign-tenant ids look exactly like missing ones (no IDOR oracle)."""
    try:
        oid = obj_id if isinstance(obj_id, uuid.UUID) else uuid.UUID(str(obj_id))
    except ValueError as exc:
        raise DomainError(404, "NOT_FOUND", "Not found") from exc
    obj = db.scalar(select(model).where(model.id == oid, model.tenant_id == tenant_id))
    if obj is None:
        raise DomainError(404, "NOT_FOUND", f"{model.__name__} not found")
    return obj
