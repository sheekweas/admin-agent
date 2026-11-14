"""Availability engine (ТЗ §8.1, §9.1).

Pipeline for one staff member and one local calendar day (branch time zone):

1. Weekly shifts for the weekday (respecting ``valid_from``/``valid_to``) minus their breaks.
2. Date exceptions: ``late_start``/``early_leave`` clip the day, ``overtime`` adds an interval,
   ``block`` removes one, ``day_off``/``vacation``/``sick`` close the whole day.
3. Candidate service starts lie on the local ``slot_step_min`` grid (counted from local midnight).
   The *occupied* interval ``[start - buffer_before, end + buffer_after)`` must fit entirely inside a
   working interval (AT-17: duration, buffers and shift closing are all honoured).
4. The occupied interval must not overlap active bookings of that staff member; ``held`` bookings
   whose ``hold_expires_at`` has passed are treated as free (they are released lazily on booking).
5. If the service needs a limited resource (``service.resource_type``) at least one active resource of
   that type in the staff member's branch must still have spare capacity.
6. ``min_lead_time_min`` and ``booking_horizon_days`` tenant settings bound the search window.

All returned datetimes are tz-aware UTC. The DB exclusion constraint remains the final guard; this module
only answers "what can be offered" and is reused by :mod:`app.services.booking` for re-validation.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    Branch,
    Resource,
    ScheduleException,
    Service,
    Shift,
    Staff,
    StaffService,
)
from app.services.common import DomainError, tz

WHOLE_DAY_EXCEPTION_KINDS = frozenset({"day_off", "vacation", "sick"})
EXCEPTION_KINDS = WHOLE_DAY_EXCEPTION_KINDS | {"early_leave", "late_start", "overtime", "block"}
# Order in which exceptions of one day are applied: clipping first, then additions, then removals.
_EXCEPTION_ORDER = {"late_start": 0, "early_leave": 0, "overtime": 1, "block": 2}

Interval = tuple[datetime, datetime]


# ---------------------------------------------------------------- slots
@dataclass(frozen=True)
class Slot:
    """A bookable start time for one staff member (ТЗ §7.2 ``get_available_slots``)."""

    staff_id: uuid.UUID
    staff_name: str
    branch_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    slot_id: str
    # Resource that would be used; internal (not exposed to clients/AI).
    resource_id: uuid.UUID | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "staff_id": str(self.staff_id),
            "staff_name": self.staff_name,
            "branch_id": str(self.branch_id),
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
        }


def make_slot_id(staff_id: uuid.UUID, start_at: datetime, service_id: uuid.UUID) -> str:
    """Opaque slot id ``<staff_id>|<iso start utc>|<service_id>`` (docs/CONTRACTS.md)."""
    return f"{staff_id}|{start_at.astimezone(UTC).isoformat()}|{service_id}"


def parse_slot_id(slot_id: str) -> tuple[uuid.UUID, datetime, uuid.UUID]:
    try:
        staff_raw, start_raw, service_raw = slot_id.split("|")
        start_at = datetime.fromisoformat(start_raw)
        if start_at.tzinfo is None:
            raise ValueError("naive datetime")
        return uuid.UUID(staff_raw), start_at.astimezone(UTC), uuid.UUID(service_raw)
    except ValueError as exc:
        raise DomainError(422, "BAD_SLOT_ID", "Malformed slot_id") from exc


# ---------------------------------------------------------------- interval arithmetic
def _union(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _subtract(intervals: list[Interval], cut: Interval) -> list[Interval]:
    out: list[Interval] = []
    for start, end in intervals:
        if cut[1] <= start or cut[0] >= end:
            out.append((start, end))
            continue
        if start < cut[0]:
            out.append((start, cut[0]))
        if cut[1] < end:
            out.append((cut[1], end))
    return out


def _clip(
    intervals: list[Interval], lo: datetime | None = None, hi: datetime | None = None
) -> list[Interval]:
    out: list[Interval] = []
    for start, end in intervals:
        s = max(start, lo) if lo else start
        e = min(end, hi) if hi else end
        if e > s:
            out.append((s, e))
    return out


def overlaps(a: Interval, b: Interval) -> bool:
    """Half-open overlap, identical to tstzrange '[)' && semantics used by the DB constraint."""
    return a[0] < b[1] and b[0] < a[1]


def _at(day: date, t: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, t, tzinfo=zone)


def _end_at(day: date, t: time, zone: ZoneInfo) -> datetime:
    """End-of-interval time; 00:00 means midnight at the end of ``day``."""
    if t == time(0):
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    return datetime.combine(day, t, tzinfo=zone)


def local_day_window(day: date, zone: ZoneInfo) -> Interval:
    """``[00:00, 24:00)`` of a local calendar day."""
    return _at(day, time(0), zone), _end_at(day, time(0), zone)


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise DomainError(422, "BAD_TIME", f"Bad time value: {value!r}") from exc


# ---------------------------------------------------------------- schedule resolution
def resolve_day(
    day: date, zone: ZoneInfo, shifts: Sequence[Shift], exceptions: Sequence[ScheduleException]
) -> list[Interval]:
    """Working intervals of one staff member on local ``day`` (tz-aware, in ``zone``), ТЗ §9.1."""
    base: list[Interval] = []
    for shift in shifts:
        if shift.weekday != day.weekday():
            continue
        if (shift.valid_from and day < shift.valid_from) or (shift.valid_to and day > shift.valid_to):
            continue
        parts = [(_at(day, shift.start_time, zone), _end_at(day, shift.end_time, zone))]
        for br in shift.breaks or []:
            parts = _subtract(
                parts, (_at(day, parse_hhmm(br["start"]), zone), _end_at(day, parse_hhmm(br["end"]), zone))
            )
        base.extend(parts)
    work = _union(base)

    if any(e.kind in WHOLE_DAY_EXCEPTION_KINDS for e in exceptions):
        return []
    for exc in sorted(exceptions, key=lambda e: _EXCEPTION_ORDER.get(e.kind, 9)):
        if exc.kind == "late_start" and exc.start_time is not None:
            work = _clip(work, lo=_at(day, exc.start_time, zone))
        elif exc.kind == "early_leave" and exc.end_time is not None:
            work = _clip(work, hi=_end_at(day, exc.end_time, zone))
        elif exc.kind == "overtime" and exc.start_time is not None and exc.end_time is not None:
            work = _union([*work, (_at(day, exc.start_time, zone), _end_at(day, exc.end_time, zone))])
        elif exc.kind == "block":
            start = _at(day, exc.start_time or time(0), zone)
            end = _end_at(day, exc.end_time or time(0), zone)
            work = _subtract(work, (start, end))
    return work


def load_shifts(
    db: Session, tenant_id: uuid.UUID, staff_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Shift]]:
    out: dict[uuid.UUID, list[Shift]] = defaultdict(list)
    if staff_ids:
        rows = db.scalars(select(Shift).where(Shift.tenant_id == tenant_id, Shift.staff_id.in_(staff_ids)))
        for s in rows:
            out[s.staff_id].append(s)
    return out


def load_exceptions(
    db: Session, tenant_id: uuid.UUID, staff_ids: Sequence[uuid.UUID], day_from: date, day_to: date
) -> dict[tuple[uuid.UUID, date], list[ScheduleException]]:
    out: dict[tuple[uuid.UUID, date], list[ScheduleException]] = defaultdict(list)
    if staff_ids:
        rows = db.scalars(
            select(ScheduleException).where(
                ScheduleException.tenant_id == tenant_id,
                ScheduleException.staff_id.in_(staff_ids),
                ScheduleException.date >= day_from,
                ScheduleException.date <= day_to,
            )
        )
        for e in rows:
            out[(e.staff_id, e.date)].append(e)
    return out


def working_intervals(
    db: Session, tenant_id: uuid.UUID, staff: Staff, day_from: date, day_to: date
) -> dict[date, list[Interval]]:
    """Resolved working hours per local day for one staff member (used by the schedule API)."""
    zone = branch_zone(db, staff.branch_id)
    shifts = load_shifts(db, tenant_id, [staff.id])[staff.id]
    exceptions = load_exceptions(db, tenant_id, [staff.id], day_from, day_to)
    return {
        d: resolve_day(d, zone, shifts, exceptions.get((staff.id, d), [])) for d in _days(day_from, day_to)
    }


def _days(day_from: date, day_to: date) -> Iterable[date]:
    for i in range((day_to - day_from).days + 1):
        yield day_from + timedelta(days=i)


def branch_zone(db: Session, branch_id: uuid.UUID) -> ZoneInfo:
    return tz(db.scalar(select(Branch.timezone).where(Branch.id == branch_id)))


# ---------------------------------------------------------------- occupancy
def _active_filter(now: datetime):
    """Bookings that occupy time: active statuses, excluding holds whose TTL has passed."""
    return and_(
        Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        or_(Booking.status != "held", Booking.hold_expires_at.is_(None), Booking.hold_expires_at > now),
    )


def load_busy(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    staff_ids: Sequence[uuid.UUID] = (),
    resource_ids: Sequence[uuid.UUID] = (),
    window: Interval,
    now: datetime,
    exclude_booking_ids: Sequence[uuid.UUID] = (),
) -> tuple[dict[uuid.UUID, list[Interval]], dict[uuid.UUID, list[Interval]]]:
    """Occupied intervals per staff and per resource within ``window``."""
    by_staff: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    by_resource: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    owners = []
    if staff_ids:
        owners.append(Booking.staff_id.in_(staff_ids))
    if resource_ids:
        owners.append(Booking.resource_id.in_(resource_ids))
    if not owners:
        return by_staff, by_resource
    stmt = select(Booking.staff_id, Booking.resource_id, Booking.occupied_start, Booking.occupied_end).where(
        Booking.tenant_id == tenant_id,
        or_(*owners),
        _active_filter(now),
        Booking.occupied_start < window[1],
        Booking.occupied_end > window[0],
    )
    if exclude_booking_ids:
        stmt = stmt.where(Booking.id.not_in(exclude_booking_ids))
    for staff_id, resource_id, start, end in db.execute(stmt):
        by_staff[staff_id].append((start, end))
        if resource_id is not None:
            by_resource[resource_id].append((start, end))
    return by_staff, by_resource


def _pick_resource(
    resources: Sequence[Resource], busy: dict[uuid.UUID, list[Interval]], occupied: Interval
) -> Resource | None:
    for res in resources:
        in_use = sum(1 for iv in busy.get(res.id, ()) if overlaps(iv, occupied))
        if in_use < max(res.capacity, 1):
            return res
    return None


# ---------------------------------------------------------------- slot generation
@dataclass(frozen=True)
class Rules:
    """Tenant rules relevant to slot generation (see app.services.tenant_settings)."""

    slot_step_min: int
    min_lead_time_min: int
    booking_horizon_days: int

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> Rules:
        return cls(
            slot_step_min=max(int(settings["slot_step_min"]), 1),
            min_lead_time_min=int(settings["min_lead_time_min"]),
            booking_horizon_days=int(settings["booking_horizon_days"]),
        )


def eligible_staff(
    db: Session,
    tenant_id: uuid.UUID,
    service_id: uuid.UUID,
    *,
    staff_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
) -> list[tuple[Staff, int | None]]:
    """Active staff (in active branches) who perform the service, with per-staff duration override."""
    stmt = (
        select(Staff, StaffService.duration_min)
        .join(StaffService, StaffService.staff_id == Staff.id)
        .join(Branch, Branch.id == Staff.branch_id)
        .where(
            Staff.tenant_id == tenant_id,
            Staff.active.is_(True),
            Branch.active.is_(True),
            StaffService.service_id == service_id,
        )
        .order_by(Staff.name, Staff.id)
    )
    if staff_id is not None:
        stmt = stmt.where(Staff.id == staff_id)
    if branch_id is not None:
        stmt = stmt.where(Staff.branch_id == branch_id)
    return [(s, d) for s, d in db.execute(stmt).all()]


def _ceil_to_grid(moment: datetime, step_min: int) -> datetime:
    """Round a local datetime up to the next multiple of ``step_min`` minutes since local midnight."""
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = math.ceil((moment - midnight).total_seconds() / 60 / step_min) * step_min
    return midnight + timedelta(minutes=minutes)


def compute_slots(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    service: Service,
    staff: Sequence[tuple[Staff, int | None]],
    day_from: date,
    day_to: date,
    now: datetime,
    rules: Rules,
    earliest: datetime | None = None,
    exclude_booking_ids: Sequence[uuid.UUID] = (),
) -> list[Slot]:
    """All free slots for ``service`` among ``staff`` on local days ``day_from..day_to`` (inclusive).

    ``earliest`` defaults to ``now + min_lead_time_min``. Results are sorted by start time, then staff name.
    """
    if not staff or day_to < day_from or not service.active:
        return []
    earliest = earliest if earliest is not None else now + timedelta(minutes=rules.min_lead_time_min)
    staff_ids = [s.id for s, _ in staff]
    branch_ids = {s.branch_id for s, _ in staff}
    zones = {b.id: tz(b.timezone) for b in db.scalars(select(Branch).where(Branch.id.in_(branch_ids)))}
    resources: dict[uuid.UUID, list[Resource]] = defaultdict(list)
    if service.resource_type:
        for res in db.scalars(
            select(Resource)
            .where(
                Resource.tenant_id == tenant_id,
                Resource.branch_id.in_(branch_ids),
                Resource.type == service.resource_type,
                Resource.active.is_(True),
            )
            .order_by(Resource.name, Resource.id)
        ):
            resources[res.branch_id].append(res)

    # Generous UTC window covering the local days in any time zone.
    window = (
        datetime.combine(day_from - timedelta(days=1), time(0), tzinfo=UTC),
        datetime.combine(day_to + timedelta(days=2), time(0), tzinfo=UTC),
    )
    shifts = load_shifts(db, tenant_id, staff_ids)
    exceptions = load_exceptions(db, tenant_id, staff_ids, day_from, day_to)
    busy_staff, busy_resource = load_busy(
        db,
        tenant_id,
        staff_ids=staff_ids,
        resource_ids=[r.id for rs in resources.values() for r in rs],
        window=window,
        now=now,
        exclude_booking_ids=exclude_booking_ids,
    )

    step = timedelta(minutes=rules.slot_step_min)
    before = timedelta(minutes=service.buffer_before_min or 0)
    after = timedelta(minutes=service.buffer_after_min or 0)
    slots: list[Slot] = []
    for member, duration_override in staff:
        zone = zones.get(member.branch_id, tz(None))
        if service.resource_type and not resources[member.branch_id]:
            continue  # the branch has no resource of the required type
        duration = timedelta(minutes=duration_override or service.duration_min)
        last_day = min(day_to, now.astimezone(zone).date() + timedelta(days=rules.booking_horizon_days))
        for day in _days(day_from, last_day):
            for work_start, work_end in resolve_day(
                day, zone, shifts.get(member.id, []), exceptions.get((member.id, day), [])
            ):
                start = _ceil_to_grid(work_start + before, rules.slot_step_min)
                while start + duration + after <= work_end:
                    occupied = (start - before, start + duration + after)
                    if start >= earliest and not any(overlaps(occupied, iv) for iv in busy_staff[member.id]):
                        resource = (
                            _pick_resource(resources[member.branch_id], busy_resource, occupied)
                            if service.resource_type
                            else None
                        )
                        if resource is not None or not service.resource_type:
                            start_utc = start.astimezone(UTC)
                            slots.append(
                                Slot(
                                    staff_id=member.id,
                                    staff_name=member.name,
                                    branch_id=member.branch_id,
                                    start_at=start_utc,
                                    end_at=(start + duration).astimezone(UTC),
                                    slot_id=make_slot_id(member.id, start_utc, service.id),
                                    resource_id=resource.id if resource else None,
                                )
                            )
                    start += step
    slots.sort(key=lambda s: (s.start_at, s.staff_name, str(s.staff_id)))
    return slots


def find_slot(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    service: Service,
    staff: Staff,
    duration_override: int | None,
    start_at: datetime,
    now: datetime,
    rules: Rules,
    earliest: datetime | None = None,
    exclude_booking_ids: Sequence[uuid.UUID] = (),
) -> Slot | None:
    """Re-validate one concrete start time for one staff member (booking transaction, ТЗ §8.3 step 3)."""
    zone = branch_zone(db, staff.branch_id)
    local_day = start_at.astimezone(zone).date()
    candidates = compute_slots(
        db,
        tenant_id,
        service=service,
        staff=[(staff, duration_override)],
        day_from=local_day,
        day_to=local_day,
        now=now,
        rules=rules,
        earliest=earliest,
        exclude_booking_ids=exclude_booking_ids,
    )
    target = start_at.astimezone(UTC)
    return next((s for s in candidates if s.start_at == target), None)
