"""Booking engine: conflict-free reservations (ТЗ §8).

Transaction algorithm (ТЗ §8.3), implemented by :func:`create_booking` / :func:`reschedule_booking`:

1. The caller's transaction is open (functions never commit; API/worker owns ``db.commit()``).
2. ``SELECT staff ... FOR UPDATE`` serialises all reservations of one staff member; limited resources of
   the required type are locked the same way. Locks are always taken in ascending id order.
3. The slot is re-validated against the fresh schedule, holds and overlaps (:mod:`availability`).
4. The booking is inserted inside a SAVEPOINT. The PostgreSQL exclusion constraints
   (``ex_bookings_staff_overlap`` / ``ex_bookings_resource_overlap``) are the final guard; a violation
   becomes ``409 SLOT_UNAVAILABLE`` with alternatives. ``uq_booking_idempotency`` makes retries return the
   original booking.
5. Outbox events and audit records are written in the same transaction.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingItem,
    Branch,
    Lead,
    Resource,
    Service,
    Staff,
    StaffService,
    Tenant,
    Visit,
)
from app.services import availability, crm
from app.services.audit import audit
from app.services.availability import Rules, Slot
from app.services.common import DomainError, emit_event, get_or_404, next_number, tz, utcnow
from app.services.tenant_settings import merged_settings

# ТЗ §8.2 state machine. "rescheduled" is reached only through reschedule_booking().
TRANSITIONS: dict[str, frozenset[str]] = {
    "held": frozenset({"pending", "confirmed", "cancelled"}),
    "pending": frozenset({"confirmed", "cancelled"}),
    "confirmed": frozenset({"checked_in", "completed", "no_show", "cancelled"}),
    "checked_in": frozenset({"completed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "no_show": frozenset(),
    "rescheduled": frozenset(),
}
INITIAL_STATUSES = frozenset({"held", "pending", "confirmed"})
RESCHEDULABLE_STATUSES = frozenset({"held", "pending", "confirmed"})
# Actors bound by the organisation's cancel/reschedule deadline and by min_lead_time (ТЗ §8.4).
CLIENT_ACTORS = frozenset({"ai", "client"})
_NUMBER_RE = re.compile(r"^(?:BK-?|#)?0*(\d{1,9})$", re.IGNORECASE)
_MAX_ALTERNATIVES = 6


def format_number(number: int) -> str:
    return f"BK-{number:06d}"


# ---------------------------------------------------------------- helpers
def _rules(db: Session, tenant_id: uuid.UUID) -> tuple[Rules, dict[str, Any]]:
    settings = merged_settings(db.get(Tenant, tenant_id))
    return Rules.from_settings(settings), settings


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DomainError(422, "NAIVE_DATETIME", "start_at must include a UTC offset")
    return value.astimezone(UTC)


def _earliest(actor_type: str, now: datetime, rules: Rules) -> datetime:
    """AI/clients respect min_lead_time; staff at the desk may book walk-ins right away."""
    if actor_type in CLIENT_ACTORS:
        return now + timedelta(minutes=rules.min_lead_time_min)
    return now


def _active_service(db: Session, tenant_id: uuid.UUID, service_id: uuid.UUID) -> Service:
    service = get_or_404(db, Service, service_id, tenant_id)
    if not service.active:
        raise DomainError(422, "SERVICE_INACTIVE", "Service is not available")
    return service


def _staff_link(db: Session, staff_id: uuid.UUID, service_id: uuid.UUID) -> StaffService | None:
    return db.get(StaffService, (staff_id, service_id))


def _lock_staff(db: Session, tenant_id: uuid.UUID, staff_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Staff]:
    """Row locks are taken on fresh data: pending changes are flushed, identity-map rows refreshed."""
    db.flush()
    rows = db.scalars(
        select(Staff)
        .where(Staff.tenant_id == tenant_id, Staff.id.in_(sorted(set(staff_ids))))
        .order_by(Staff.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {s.id: s for s in rows}


def _lock_resources(db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID, resource_type: str) -> None:
    db.scalars(
        select(Resource.id)
        .where(
            Resource.tenant_id == tenant_id, Resource.branch_id == branch_id, Resource.type == resource_type
        )
        .order_by(Resource.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def _by_idempotency(db: Session, tenant_id: uuid.UUID, key: str | None) -> Booking | None:
    if not key:
        return None
    return db.scalar(select(Booking).where(Booking.tenant_id == tenant_id, Booking.idempotency_key == key))


def _is_idempotency_violation(exc: IntegrityError) -> bool:
    return "uq_booking_idempotency" in str(exc.orig)


def _event_payload(b: Booking) -> dict[str, Any]:
    return {
        "booking_id": str(b.id),
        "number": format_number(b.number),
        "status": b.status,
        "contact_id": str(b.contact_id),
        "staff_id": str(b.staff_id),
        "branch_id": str(b.branch_id),
        "service_ids": [str(i.service_id) for i in b.items],
        "start_at": b.start_at.isoformat(),
        "end_at": b.end_at.isoformat(),
        "channel": b.channel,
        "source": b.source,
        "total_price": str(b.total_price),
    }


def _load_booking(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    contact_id: uuid.UUID | None,
) -> Booking:
    """Locked, tenant-scoped (and, for AI/client calls, contact-scoped) fetch; foreign ids look missing (§7.3)."""
    db.flush()
    b = db.scalar(
        select(Booking)
        .where(Booking.tenant_id == tenant_id, Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if b is None or (contact_id is not None and b.contact_id != _contact_root(db, tenant_id, contact_id)):
        raise DomainError(404, "NOT_FOUND", "Booking not found")
    return b


def _contact_root(db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> uuid.UUID | None:
    try:
        return crm.resolve_contact(db, tenant_id, contact_id).id
    except DomainError:
        return None


def _booking_lead(db: Session, tenant_id: uuid.UUID, booking_id: uuid.UUID) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.tenant_id == tenant_id, Lead.booking_id == booking_id))


def _check_deadline(b: Booking, actor_type: str, settings: dict[str, Any], now: datetime) -> None:
    hours = float(settings.get("cancel_deadline_hours") or 0)
    if actor_type in CLIENT_ACTORS and b.start_at - now < timedelta(hours=hours):
        raise DomainError(
            409,
            "DEADLINE_PASSED",
            f"Changes are allowed no later than {hours:g} h before the visit; an administrator can help",
            deadline_hours=hours,
        )


def _transition(b: Booking, status: str) -> None:
    if status not in TRANSITIONS.get(b.status, frozenset()):
        raise DomainError(
            409, "INVALID_TRANSITION", f"Cannot change status from {b.status} to {status}", current=b.status
        )


# ---------------------------------------------------------------- availability
def get_available_slots(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    service_id: uuid.UUID,
    date_from: date,
    date_to: date,
    staff_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> list[Slot]:
    """Free slots for a service within local dates ``date_from..date_to`` (ТЗ §7.2, §8.1)."""
    now = now or utcnow()
    service = _active_service(db, tenant_id, service_id)
    rules, _ = _rules(db, tenant_id)
    staff = availability.eligible_staff(db, tenant_id, service.id, staff_id=staff_id, branch_id=branch_id)
    slots = availability.compute_slots(
        db, tenant_id, service=service, staff=staff, day_from=date_from, day_to=date_to, now=now, rules=rules
    )
    return slots[:limit]


def find_alternatives(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    service: Service,
    start_at: datetime,
    staff_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
    now: datetime,
    earliest: datetime | None = None,
    exclude_booking_ids: Sequence[uuid.UUID] = (),
    limit: int = _MAX_ALTERNATIVES,
    strict_branch: bool = False,
) -> list[dict[str, Any]]:
    """ТЗ §4.2: nearest slots for the same staff, then other staff, then the following days.

    Other branches are offered only to the AI/client flow and only when settings allow it; a human who picked
    a branch explicitly (``strict_branch``) gets alternatives in that branch only.
    """
    rules, settings = _rules(db, tenant_id)
    ai = settings.get("ai", {})
    other_branch_ok = ai.get("allow_other_branch", True) and not (strict_branch and branch_id)
    branch_filter = None if other_branch_ok else branch_id
    staff = availability.eligible_staff(db, tenant_id, service.id, branch_id=branch_filter)
    if staff_id is not None and not ai.get("allow_other_staff", True):
        staff = [s for s in staff if s[0].id == staff_id]
    if not staff:
        return []
    zone = availability.branch_zone(db, branch_id or staff[0][0].branch_id)
    day = start_at.astimezone(zone).date()
    slots = availability.compute_slots(
        db,
        tenant_id,
        service=service,
        staff=staff,
        day_from=day,
        day_to=day + timedelta(days=7),
        now=now,
        rules=rules,
        earliest=earliest,
        exclude_booking_ids=exclude_booking_ids,
    )

    def distance(s: Slot) -> float:
        return abs((s.start_at - start_at).total_seconds())

    same_day = [s for s in slots if s.start_at.astimezone(zone).date() == day]
    ordered = (
        sorted((s for s in same_day if s.staff_id == staff_id), key=distance)[:3]
        + sorted((s for s in same_day if s.staff_id != staff_id), key=distance)[:3]
        + [s for s in slots if s.start_at.astimezone(zone).date() > day]
    )
    out: list[dict[str, Any]] = []
    for s in ordered:
        if s.start_at != start_at or s.staff_id != staff_id:
            out.append(s.to_dict())
        if len(out) >= limit:
            break
    return out


def _unavailable(
    db: Session,
    tenant_id: uuid.UUID,
    service: Service,
    start_at: datetime,
    staff_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
    now: datetime,
    earliest: datetime,
    exclude: Sequence[uuid.UUID] = (),
    *,
    strict_branch: bool = False,
) -> DomainError:
    alternatives = find_alternatives(
        db,
        tenant_id,
        service=service,
        start_at=start_at,
        staff_id=staff_id,
        branch_id=branch_id,
        now=now,
        earliest=earliest,
        exclude_booking_ids=exclude,
        strict_branch=strict_branch,
    )
    return DomainError(
        409, "SLOT_UNAVAILABLE", "The selected time is no longer available", alternatives=alternatives
    )


# ---------------------------------------------------------------- holds
def _release_holds(
    db: Session, now: datetime, *, tenant_id: uuid.UUID | None = None, staff_id: uuid.UUID | None = None
) -> int:
    db.flush()
    stmt = (
        select(Booking)
        .where(Booking.status == "held", Booking.hold_expires_at.is_not(None), Booking.hold_expires_at <= now)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if tenant_id is not None:
        stmt = stmt.where(Booking.tenant_id == tenant_id)
    if staff_id is not None:
        stmt = stmt.where(Booking.staff_id == staff_id)
    released = db.scalars(stmt).all()
    for b in released:
        b.status = "cancelled"
        b.cancel_reason = "hold_expired"
        emit_event(
            db,
            b.tenant_id,
            "booking.status_changed",
            "booking",
            b.id,
            {**_event_payload(b), "from": "held", "reason": "hold_expired"},
        )
        audit(db, b.tenant_id, "system", None, "booking.hold_expired", "booking", str(b.id), {})
    if released:
        db.flush()
    return len(released)


def release_expired_holds(db: Session, now: datetime | None = None) -> int:
    """Worker job: cancel ``held`` bookings whose TTL passed (ТЗ §8.2). Returns how many were released."""
    return _release_holds(db, now or utcnow())


# ---------------------------------------------------------------- create
def _insert_booking(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    staff: Staff,
    service: Service,
    slot: Slot,
    contact_id: uuid.UUID,
    status: str,
    now: datetime,
    settings: dict[str, Any],
    channel: str,
    source: str,
    idempotency_key: str | None,
    comment: str,
    created_by: uuid.UUID | None,
    rescheduled_from_id: uuid.UUID | None = None,
) -> Booking:
    """Add the booking row (caller wraps this in a savepoint and handles IntegrityError)."""
    link = _staff_link(db, staff.id, service.id)
    price = link.price if link and link.price is not None else service.price
    duration = int((slot.end_at - slot.start_at).total_seconds() // 60)
    b = Booking(
        tenant_id=tenant_id,
        number=next_number(db, tenant_id, "booking"),
        branch_id=staff.branch_id,
        contact_id=contact_id,
        staff_id=staff.id,
        resource_id=slot.resource_id,
        start_at=slot.start_at,
        end_at=slot.end_at,
        occupied_start=slot.start_at - timedelta(minutes=service.buffer_before_min or 0),
        occupied_end=slot.end_at + timedelta(minutes=service.buffer_after_min or 0),
        status=status,
        hold_expires_at=now + timedelta(minutes=int(settings["hold_ttl_min"])) if status == "held" else None,
        channel=channel,
        source=source,
        idempotency_key=idempotency_key,
        comment=comment,
        total_price=Decimal(price),
        created_by=created_by,
        rescheduled_from_id=rescheduled_from_id,
    )
    b.items.append(
        BookingItem(service_id=service.id, price_snapshot=Decimal(price), duration_snapshot=duration)
    )
    db.add(b)
    db.flush()
    return b


def _reserve(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    staff_id: uuid.UUID,
    service: Service,
    start_at: datetime,
    now: datetime,
    earliest: datetime,
    rules: Rules,
) -> tuple[Staff, Slot] | None:
    """Steps 2-3 of ТЗ §8.3: lock and re-validate. Returns None when the slot is not free."""
    staff = _lock_staff(db, tenant_id, [staff_id]).get(staff_id)
    if staff is None or not staff.active:
        return None
    link = _staff_link(db, staff.id, service.id)
    if link is None:
        return None
    _release_holds(db, now, tenant_id=tenant_id, staff_id=staff.id)
    if service.resource_type:
        _lock_resources(db, tenant_id, staff.branch_id, service.resource_type)
    slot = availability.find_slot(
        db,
        tenant_id,
        service=service,
        staff=staff,
        duration_override=link.duration_min,
        start_at=start_at,
        now=now,
        rules=rules,
        earliest=earliest,
    )
    return (staff, slot) if slot else None


def create_booking(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID,
    service_id: uuid.UUID,
    start_at: datetime | None = None,
    staff_id: uuid.UUID | None = None,
    slot_id: str | None = None,
    branch_id: uuid.UUID | None = None,
    idempotency_key: str | None,
    channel: str,
    source: str,
    actor_type: str,
    actor_id: str | None,
    comment: str = "",
    status: str = "confirmed",
) -> Booking:
    """Atomically reserve a slot (ТЗ §8.3). Same ``idempotency_key`` returns the existing booking.

    Without ``staff_id`` any eligible staff member free at ``start_at`` is chosen.
    Raises ``DomainError(409, "SLOT_UNAVAILABLE", alternatives=[...])``.
    """
    existing = _by_idempotency(db, tenant_id, idempotency_key)
    if existing is not None:
        return existing
    if status not in INITIAL_STATUSES:
        raise DomainError(422, "BAD_STATUS", f"Booking cannot be created as {status}")
    service = _active_service(db, tenant_id, service_id)
    if slot_id:
        staff_id, start_at, slot_service_id = availability.parse_slot_id(slot_id)
        if slot_service_id != service.id:
            raise DomainError(422, "BAD_SLOT_ID", "slot_id belongs to another service")
    if start_at is None:
        raise DomainError(422, "START_REQUIRED", "start_at or slot_id is required")
    start_at = _as_utc(start_at)
    contact = crm.resolve_contact(db, tenant_id, contact_id)
    now = utcnow()
    rules, settings = _rules(db, tenant_id)
    earliest = _earliest(actor_type, now, rules)
    strict = actor_type == "user"  # desk staff chose the branch explicitly

    if staff_id is not None:
        candidates = [staff_id]
    else:
        candidates = sorted(
            s.id for s, _ in availability.eligible_staff(db, tenant_id, service.id, branch_id=branch_id)
        )
    reserved = None
    for candidate in candidates:
        reserved = _reserve(
            db,
            tenant_id,
            staff_id=candidate,
            service=service,
            start_at=start_at,
            now=now,
            earliest=earliest,
            rules=rules,
        )
        if reserved:
            break
    if reserved is None:
        # A concurrent request with the SAME key may have taken this very slot while we waited for the staff
        # lock: that is a retry of one logical request, so return its booking instead of 409 (load test).
        existing = _by_idempotency(db, tenant_id, idempotency_key)
        if existing is not None:
            return existing
        raise _unavailable(
            db, tenant_id, service, start_at, staff_id, branch_id, now, earliest, strict_branch=strict
        )
    staff, slot = reserved
    # A concurrent request with the same key may have committed while we waited for the lock.
    existing = _by_idempotency(db, tenant_id, idempotency_key)
    if existing is not None:
        return existing

    try:
        with db.begin_nested():
            b = _insert_booking(
                db,
                tenant_id,
                staff=staff,
                service=service,
                slot=slot,
                contact_id=contact.id,
                status=status,
                now=now,
                settings=settings,
                channel=channel,
                source=source,
                idempotency_key=idempotency_key,
                comment=comment,
                created_by=_user_id(actor_type, actor_id),
            )
    except IntegrityError as exc:
        if _is_idempotency_violation(exc) and (existing := _by_idempotency(db, tenant_id, idempotency_key)):
            return existing
        raise _unavailable(
            db, tenant_id, service, start_at, staff.id, staff.branch_id, now, earliest, strict_branch=strict
        ) from exc

    payload = _event_payload(b)
    emit_event(db, tenant_id, "booking.created", "booking", b.id, payload)
    if status == "confirmed":
        emit_event(db, tenant_id, "booking.confirmed", "booking", b.id, payload)
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "booking.create",
        "booking",
        str(b.id),
        {
            "number": b.number,
            "status": status,
            "start_at": payload["start_at"],
            "staff_id": payload["staff_id"],
            "channel": channel,
            "source": source,
        },
    )
    _attach_lead(db, tenant_id, b, service.id, actor_type, actor_id)
    contact.last_contact_at = now
    return b


def _user_id(actor_type: str, actor_id: str | None) -> uuid.UUID | None:
    if actor_type != "user" or not actor_id:
        return None
    try:
        return uuid.UUID(actor_id)
    except ValueError:
        return None


def _lead_stage_for(status: str) -> str:
    return "booked" if status == "confirmed" else "booking_pending"


def _attach_lead(
    db: Session,
    tenant_id: uuid.UUID,
    b: Booking,
    service_id: uuid.UUID,
    actor_type: str,
    actor_id: str | None,
) -> None:
    """Link the booking to the contact's open lead; a lead tied to another booking spawns a new one.

    New leads inherit the contact's acquisition source (ads/site/qr/whatsapp/...) for funnel analytics.
    """
    source = crm.resolve_contact(db, tenant_id, b.contact_id).source
    lead = crm.open_or_get_lead(db, tenant_id, b.contact_id, source=source, service_id=service_id)
    if lead.booking_id is not None and lead.booking_id != b.id:
        lead = crm.create_lead(db, tenant_id, b.contact_id, source=source, service_id=service_id)
    lead.booking_id = b.id
    lead.branch_id = lead.branch_id or b.branch_id
    crm.set_lead_stage(
        db, tenant_id, lead.id, _lead_stage_for(b.status), actor_type=actor_type, actor_id=actor_id
    )


# ---------------------------------------------------------------- reschedule / cancel / status
def reschedule_booking(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    new_start_at: datetime | None = None,
    new_staff_id: uuid.UUID | None = None,
    new_slot_id: str | None = None,
    actor_type: str,
    actor_id: str | None,
    contact_id: uuid.UUID | None = None,
) -> Booking:
    """ТЗ §8.4: reserve the new slot, then release the old one — atomically. Returns the NEW booking.

    Both rows change inside one SAVEPOINT: the old booking turns ``rescheduled`` and the new one is inserted
    (so a shift by a few minutes for the same staff does not collide with itself). If the exclusion
    constraint fires, the savepoint rolls back and the original booking stays untouched.
    """
    old = _load_booking(db, tenant_id, booking_id, contact_id)
    if old.status not in RESCHEDULABLE_STATUSES:
        raise DomainError(
            409, "INVALID_TRANSITION", f"Cannot reschedule a {old.status} booking", current=old.status
        )
    now = utcnow()
    rules, settings = _rules(db, tenant_id)
    _check_deadline(old, actor_type, settings, now)
    service = _active_service(db, tenant_id, old.items[0].service_id)
    if new_slot_id:
        new_staff_id, new_start_at, slot_service_id = availability.parse_slot_id(new_slot_id)
        if slot_service_id != service.id:
            raise DomainError(422, "BAD_SLOT_ID", "slot_id belongs to another service")
    if new_start_at is None:
        raise DomainError(422, "START_REQUIRED", "new start_at or slot_id is required")
    start_at = _as_utc(new_start_at)
    staff_id = new_staff_id or old.staff_id
    earliest = _earliest(actor_type, now, rules)

    locked = _lock_staff(db, tenant_id, [old.staff_id, staff_id])
    staff = locked.get(staff_id)
    link = _staff_link(db, staff_id, service.id) if staff else None
    slot = None
    if staff is not None and staff.active and link is not None:
        _release_holds(db, now, tenant_id=tenant_id, staff_id=staff.id)
        if service.resource_type:
            _lock_resources(db, tenant_id, staff.branch_id, service.resource_type)
        slot = availability.find_slot(
            db,
            tenant_id,
            service=service,
            staff=staff,
            duration_override=link.duration_min,
            start_at=start_at,
            now=now,
            rules=rules,
            earliest=earliest,
            exclude_booking_ids=[old.id],
        )
    if slot is None or staff is None:
        raise _unavailable(
            db,
            tenant_id,
            service,
            start_at,
            staff_id,
            old.branch_id,
            now,
            earliest,
            [old.id],
            strict_branch=actor_type == "user",
        )

    previous_status = old.status
    try:
        with db.begin_nested():
            old.status = "rescheduled"
            db.flush()
            new = _insert_booking(
                db,
                tenant_id,
                staff=staff,
                service=service,
                slot=slot,
                contact_id=old.contact_id,
                status=previous_status,
                now=now,
                settings=settings,
                channel=old.channel,
                source=old.source,
                idempotency_key=None,
                comment=old.comment,
                created_by=_user_id(actor_type, actor_id),
                rescheduled_from_id=old.id,
            )
            old.rescheduled_to_id = new.id
            db.flush()
    except IntegrityError as exc:
        raise _unavailable(
            db,
            tenant_id,
            service,
            start_at,
            staff_id,
            old.branch_id,
            now,
            earliest,
            [old.id],
            strict_branch=actor_type == "user",
        ) from exc

    payload = {
        **_event_payload(new),
        "old_booking_id": str(old.id),
        "old_start_at": old.start_at.isoformat(),
        "old_staff_id": str(old.staff_id),
        "actor_type": actor_type,
    }
    emit_event(db, tenant_id, "booking.rescheduled", "booking", new.id, payload)
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "booking.reschedule",
        "booking",
        str(old.id),
        {"new_booking_id": str(new.id), "new_start_at": payload["start_at"], "new_staff_id": str(staff.id)},
    )
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "booking.create",
        "booking",
        str(new.id),
        {"number": new.number, "rescheduled_from_id": str(old.id), "start_at": payload["start_at"]},
    )
    lead = _booking_lead(db, tenant_id, old.id)
    if lead is not None:
        lead.booking_id = new.id
    if actor_type in CLIENT_ACTORS:
        crm.add_contact_tags(db, tenant_id, old.contact_id, ["перенёс запись"])
    return new


def cancel_booking(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    reason: str,
    actor_type: str,
    actor_id: str | None,
    contact_id: uuid.UUID | None = None,
) -> Booking:
    """ТЗ §8.4 / AT-09: frees the slot; AI/client actors must respect ``cancel_deadline_hours``."""
    b = _load_booking(db, tenant_id, booking_id, contact_id)
    _transition(b, "cancelled")
    now = utcnow()
    _, settings = _rules(db, tenant_id)
    _check_deadline(b, actor_type, settings, now)
    previous = b.status
    b.status = "cancelled"
    b.cancel_reason = (reason or "")[:500]
    b.hold_expires_at = None
    db.flush()
    emit_event(
        db,
        tenant_id,
        "booking.cancelled",
        "booking",
        b.id,
        {**_event_payload(b), "from": previous, "reason": b.cancel_reason, "actor_type": actor_type},
    )
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "booking.cancel",
        "booking",
        str(b.id),
        {"from": previous, "reason": b.cancel_reason},
    )
    lead = _booking_lead(db, tenant_id, b.id)
    if lead is not None and lead.stage not in crm.CLOSED_LEAD_STAGES:
        crm.set_lead_stage(
            db,
            tenant_id,
            lead.id,
            "cancelled",
            reason=b.cancel_reason,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    if actor_type in CLIENT_ACTORS:
        crm.add_contact_tags(db, tenant_id, b.contact_id, ["отменил запись"])
    return b


def set_booking_status(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    status: str,
    *,
    actor_type: str,
    actor_id: str | None,
) -> Booking:
    """Validated status transition (ТЗ §8.2). Check-in/complete create a Visit (ТЗ §14.1)."""
    if status == "cancelled":
        return cancel_booking(db, tenant_id, booking_id, reason="", actor_type=actor_type, actor_id=actor_id)
    b = _load_booking(db, tenant_id, booking_id, None)
    _transition(b, status)
    previous = b.status
    b.status = status
    if status != "held":
        b.hold_expires_at = None
    db.flush()
    payload = {**_event_payload(b), "from": previous, "to": status}
    emit_event(db, tenant_id, "booking.status_changed", "booking", b.id, payload)
    if status == "confirmed":
        emit_event(db, tenant_id, "booking.confirmed", "booking", b.id, payload)
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "booking.status",
        "booking",
        str(b.id),
        {"from": previous, "to": status},
    )

    lead = _booking_lead(db, tenant_id, b.id)
    if status in ("checked_in", "completed"):
        _ensure_visit(db, tenant_id, b)
        if lead is not None:
            crm.set_lead_stage(db, tenant_id, lead.id, "visited", actor_type=actor_type, actor_id=actor_id)
    elif status == "confirmed" and lead is not None and lead.stage not in crm.CLOSED_LEAD_STAGES:
        crm.set_lead_stage(db, tenant_id, lead.id, "booked", actor_type=actor_type, actor_id=actor_id)
    elif status == "no_show":
        crm.add_contact_tags(db, tenant_id, b.contact_id, ["не пришёл"])
    return b


def _ensure_visit(db: Session, tenant_id: uuid.UUID, b: Booking) -> Visit:
    visit = db.scalar(select(Visit).where(Visit.tenant_id == tenant_id, Visit.booking_id == b.id))
    if visit is None:
        visit = Visit(
            tenant_id=tenant_id,
            booking_id=b.id,
            contact_id=b.contact_id,
            staff_id=b.staff_id,
            branch_id=b.branch_id,
            visited_at=b.start_at,
            amount_due=b.total_price,
        )
        db.add(visit)
        db.flush()
    return visit


def update_comment(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    comment: str,
    *,
    actor_type: str,
    actor_id: str | None,
) -> Booking:
    b = _load_booking(db, tenant_id, booking_id, None)
    b.comment = comment
    audit(db, tenant_id, actor_type, actor_id, "booking.comment", "booking", str(b.id), {})
    return b


# ---------------------------------------------------------------- read side
def get_booking_for_contact(
    db: Session,
    tenant_id: uuid.UUID,
    booking_id_or_number: str | uuid.UUID,
    contact_id: uuid.UUID,
) -> Booking | None:
    """ТЗ §7.2 ``get_booking``: accepts a UUID, ``BK-000123``, ``#123`` or ``123``.

    Only bookings of the (verified) contact are returned; anything else is ``None``.
    """
    root = _contact_root(db, tenant_id, contact_id)
    if root is None:
        return None
    stmt = select(Booking).where(Booking.tenant_id == tenant_id, Booking.contact_id == root)
    raw = str(booking_id_or_number).strip()
    try:
        return db.scalar(stmt.where(Booking.id == uuid.UUID(raw)))
    except ValueError:
        match = _NUMBER_RE.match(raw)
        return db.scalar(stmt.where(Booking.number == int(match.group(1)))) if match else None


def list_contact_bookings(
    db: Session,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    upcoming_only: bool = True,
) -> list[Booking]:
    root = _contact_root(db, tenant_id, contact_id)
    if root is None:
        return []
    stmt = select(Booking).where(Booking.tenant_id == tenant_id, Booking.contact_id == root)
    if upcoming_only:
        stmt = stmt.where(Booking.status.in_(ACTIVE_BOOKING_STATUSES), Booking.end_at > utcnow())
        return list(db.scalars(stmt.order_by(Booking.start_at)))
    return list(db.scalars(stmt.order_by(Booking.start_at.desc())))


def booking_to_dict(b: Booking) -> dict[str, Any]:
    """Client-safe representation for AI tools and messages (local time in the branch time zone)."""
    db = object_session(b)
    staff = db.get(Staff, b.staff_id) if db else None
    branch = db.get(Branch, b.branch_id) if db else None
    zone = tz(branch.timezone if branch else None)
    services = []
    for item in b.items:
        service = db.get(Service, item.service_id) if db else None
        services.append(
            {
                "service_id": str(item.service_id),
                "title_ru": service.title_ru if service else "",
                "title_kk": service.title_kk if service else "",
                "price": str(item.price_snapshot),
                "duration_min": item.duration_snapshot,
            }
        )
    local = b.start_at.astimezone(zone)
    return {
        "booking_id": str(b.id),
        "number": format_number(b.number),
        "status": b.status,
        "start_at": b.start_at.isoformat(),
        "end_at": b.end_at.isoformat(),
        "local_date": local.date().isoformat(),
        "local_time": local.strftime("%H:%M"),
        "timezone": zone.key,
        "staff_id": str(b.staff_id),
        "staff_name": staff.name if staff else "",
        "branch_id": str(b.branch_id),
        "branch_name": branch.name if branch else "",
        "branch_address": branch.address if branch else "",
        "services": services,
        "total_price": str(b.total_price),
        "hold_expires_at": b.hold_expires_at.isoformat() if b.hold_expires_at else None,
    }
