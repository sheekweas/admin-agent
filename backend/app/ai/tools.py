"""Typed backend tools exposed to the AI agent (ТЗ §7.2) and their executor.

Security model (ТЗ §7.3, §20.3):
- tenant_id, contact_id, conversation_id are taken from `ToolContext` (the conversation), NEVER from the
  model. Input schemas do not even have such fields and `extra="forbid"` rejects them.
- Every booking lookup/mutation goes through an ownership check for the conversation's contact; foreign
  bookings look exactly like missing ones.
- create_booking's idempotency key is derived from the triggering inbound message, so a re-delivered
  webhook / retried worker job cannot create a second booking.
- Mutations run inside a SAVEPOINT; a failing tool never leaves the session half-written.
- In `dry_run` (knowledge sandbox) every mutating tool is stubbed.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Booking, Branch, Contact, Conversation, Service, Staff, StaffService, Tenant
from app.services.common import DomainError, tz, utcnow
from app.services.tenant_settings import merged_settings

log = logging.getLogger("ai.tools")

MAX_SLOT_RANGE_DAYS = 14
MAX_SLOTS_RETURNED = 12

# Tags the AI may suggest (ТЗ §10.3). The CRM keeps control: crm.add_contact_tags drops anything else.
TAG_ASKED_PRICE = "спросил цену"
TAG_WANTS_TODAY = "хочет сегодня"
TAG_NO_SLOT = "нет свободного слота"
TAG_WAITING_ADMIN = "ждёт администратора"
TAG_VOICE = "голосовое сообщение"
AI_ALLOWED_TAGS = [
    TAG_ASKED_PRICE,
    TAG_WANTS_TODAY,
    TAG_NO_SLOT,
    TAG_WAITING_ADMIN,
    "жалоба",
    "просит перезвонить",
]

LEAD_STAGE_RANK = {
    "new": 0,
    "info_requested": 1,
    "qualified": 2,
    "booking_pending": 3,
    "booked": 4,
    "visited": 5,
    "repeat": 5,
}


# ---------------------------------------------------------------- context
@dataclass
class ToolContext:
    db: Session
    tenant_id: uuid.UUID
    contact_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    trigger_message_id: uuid.UUID | None
    channel: str = "whatsapp"
    language: str = "ru"
    branch_id: uuid.UUID | None = None
    timezone: str = "Asia/Almaty"
    dry_run: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    booking_done: bool = False  # a create/reschedule/cancel succeeded this turn
    escalated: bool = False
    _tz_cache: dict[uuid.UUID, ZoneInfo] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return tz(self.timezone)

    def branch_tz(self, branch_id: uuid.UUID | None) -> ZoneInfo:
        if branch_id is None:
            return self.tz
        if branch_id not in self._tz_cache:
            name = self.db.scalar(
                select(Branch.timezone).where(Branch.id == branch_id, Branch.tenant_id == self.tenant_id)
            )
            self._tz_cache[branch_id] = tz(name or self.timezone)
        return self._tz_cache[branch_id]

    def today(self) -> date:
        return utcnow().astimezone(self.tz).date()


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# ---------------------------------------------------------------- input models
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ListServicesIn(_In):
    branch_id: uuid.UUID | None = None


class GetSlotsIn(_In):
    service_id: uuid.UUID
    date_from: date
    date_to: date | None = None
    staff_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    preferred_time: str | None = Field(default=None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")


class CreateBookingIn(_In):
    slot_id: str = Field(min_length=3, max_length=300)
    service_id: uuid.UUID
    comment: str = Field(default="", max_length=500)


class BookingRefIn(_In):
    booking: str = Field(min_length=1, max_length=64)


class RescheduleIn(BookingRefIn):
    new_slot_id: str = Field(min_length=3, max_length=300)


class CancelIn(BookingRefIn):
    reason: str = Field(default="", max_length=500)


class EmptyIn(_In):
    pass


class UpdateLeadIn(_In):
    stage: Literal["info_requested", "qualified", "booking_pending", "lost"]
    service_id: uuid.UUID | None = None
    reason: str = Field(default="", max_length=300)


class EscalateIn(_In):
    question: str = Field(min_length=2, max_length=2000)
    priority: Literal["normal", "high", "urgent"] = "normal"


class AddTagIn(_In):
    tag: str = Field(max_length=50)


class SetNameIn(_In):
    name: str = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------- JSON schemas sent to the model (strict)
def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_UUID = {"type": "string", "format": "uuid"}
_BOOKING_REF = {
    "type": "string",
    "description": "Booking number as shown to the client (e.g. BK-000123) or id.",
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_services",
        "description": "List the organisation's published services with real prices and durations. Call it "
        "when the client asks what services exist or what something costs, or to find the "
        "service_id before checking slots.",
        "input_schema": _obj({"branch_id": {**_UUID, "description": "Optional branch filter."}}, []),
    },
    {
        "name": "get_available_slots",
        "description": "Get real free time slots for a service. Call it before proposing any time to the "
        "client. Dates are local dates of the branch (YYYY-MM-DD); range at most 14 days.",
        "input_schema": _obj(
            {
                "service_id": _UUID,
                "date_from": {"type": "string", "format": "date"},
                "date_to": {
                    "type": "string",
                    "format": "date",
                    "description": "Inclusive; defaults to date_from.",
                },
                "staff_id": {**_UUID, "description": "Only if the client asked for a specific specialist."},
                "branch_id": _UUID,
                "preferred_time": {"type": "string", "description": "HH:MM local; nearest slots come first."},
            },
            ["service_id", "date_from"],
        ),
    },
    {
        "name": "create_booking",
        "description": "Book a slot returned by get_available_slots for THIS client. Call only after the "
        "client explicitly confirmed the date, time and service. Returns the booking number.",
        "input_schema": _obj(
            {
                "slot_id": {
                    "type": "string",
                    "description": "slot_id exactly as returned by get_available_slots.",
                },
                "service_id": _UUID,
                "comment": {"type": "string"},
            },
            ["slot_id", "service_id"],
        ),
    },
    {
        "name": "reschedule_booking",
        "description": "Move one of THIS client's bookings to a new slot from get_available_slots, after "
        "the client confirmed the new time.",
        "input_schema": _obj(
            {"booking": _BOOKING_REF, "new_slot_id": {"type": "string"}}, ["booking", "new_slot_id"]
        ),
    },
    {
        "name": "cancel_booking",
        "description": "Cancel one of THIS client's bookings after the client explicitly confirmed the "
        "cancellation.",
        "input_schema": _obj({"booking": _BOOKING_REF, "reason": {"type": "string"}}, ["booking"]),
    },
    {
        "name": "get_booking",
        "description": "Get details of one of THIS client's bookings by its number.",
        "input_schema": _obj({"booking": _BOOKING_REF}, ["booking"]),
    },
    {
        "name": "list_my_bookings",
        "description": "List THIS client's upcoming bookings (for questions like 'when is my appointment', "
        "or before cancelling/rescheduling).",
        "input_schema": _obj({}, []),
    },
    {
        "name": "update_lead",
        "description": "Move the client's current request along the sales funnel.",
        "input_schema": _obj(
            {
                "stage": {
                    "type": "string",
                    "enum": ["info_requested", "qualified", "booking_pending", "lost"],
                },
                "service_id": _UUID,
                "reason": {"type": "string", "description": "Required for 'lost': why the client declined."},
            },
            ["stage"],
        ),
    },
    {
        "name": "escalate_to_admin",
        "description": "Create a task for a human administrator. Call it whenever you cannot answer reliably "
        "from the catalogue/knowledge (unknown price, discount, policy, medical question), on "
        "complaints, or when the client asks for a human. Then tell the client an "
        "administrator will reply.",
        "input_schema": _obj(
            {
                "question": {
                    "type": "string",
                    "description": "The client's question, concise, in the client's words.",
                },
                "priority": {"type": "string", "enum": ["normal", "high", "urgent"]},
            },
            ["question"],
        ),
    },
    {
        "name": "add_tag",
        "description": "Suggest a CRM tag for this client (only from the allowed list).",
        "input_schema": _obj({"tag": {"type": "string", "enum": AI_ALLOWED_TAGS}}, ["tag"]),
    },
    {
        "name": "set_contact_name",
        "description": "Save the name the client told you.",
        "input_schema": _obj({"name": {"type": "string"}}, ["name"]),
    },
]
for _t in TOOL_DEFINITIONS:
    _t["strict"] = True

MUTATING_TOOLS = {
    "create_booking",
    "reschedule_booking",
    "cancel_booking",
    "update_lead",
    "escalate_to_admin",
    "add_tag",
    "set_contact_name",
}


# ---------------------------------------------------------------- helpers
def booking_number(b: Booking) -> str:
    return f"BK-{b.number:06d}"


def _money(v: Decimal | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if v == v.to_integral_value() else f"{v:.2f}"


def _service_title(s: Service, lang: str) -> str:
    return (s.title_kk if lang == "kk" and s.title_kk else s.title_ru) or s.title_ru


def _require_contact(ctx: ToolContext) -> uuid.UUID:
    if ctx.contact_id is None:
        raise ToolFailure("NO_CLIENT", "This conversation has no identified client.")
    return ctx.contact_id


def _own_booking(ctx: ToolContext, ref: str) -> Booking:
    from app.services import booking as booking_svc

    contact_id = _require_contact(ctx)
    ref = ref.strip().lstrip("№").strip()
    b = booking_svc.get_booking_for_contact(ctx.db, ctx.tenant_id, ref, contact_id)
    # Defence in depth: re-check ownership/tenant regardless of the service implementation.
    if b is None or b.tenant_id != ctx.tenant_id or b.contact_id != contact_id:
        raise ToolFailure("NOT_FOUND", "Booking not found.")
    return b


def booking_view(ctx: ToolContext, b: Booking) -> dict[str, Any]:
    db = ctx.db
    staff = db.get(Staff, b.staff_id)
    branch = db.get(Branch, b.branch_id)
    service = None
    if b.items:
        service = db.get(Service, b.items[0].service_id)
    local = b.start_at.astimezone(ctx.branch_tz(b.branch_id))
    return {
        "booking_number": booking_number(b),
        "status": b.status,
        "date": local.date().isoformat(),
        "time": f"{local:%H:%M}",
        "service": _service_title(service, ctx.language) if service else "",
        "service_id": str(service.id) if service else None,
        "staff_name": staff.name if staff else "",
        "branch": branch.name if branch else "",
        "address": branch.address if branch else "",
        "price": _money(b.total_price),
    }


def _slot_view(ctx: ToolContext, slot: Any) -> dict[str, Any]:
    if isinstance(slot, dict):
        start = slot.get("start_at")
        start = datetime.fromisoformat(start) if isinstance(start, str) else start
        staff_id, staff_name = slot.get("staff_id"), slot.get("staff_name", "")
        branch_id, slot_id = slot.get("branch_id"), slot.get("slot_id")
    else:
        start, staff_id, staff_name = slot.start_at, slot.staff_id, slot.staff_name
        branch_id, slot_id = slot.branch_id, slot.slot_id
    bid = uuid.UUID(str(branch_id)) if branch_id else None
    local = start.astimezone(ctx.branch_tz(bid))
    return {
        "slot_id": slot_id,
        "date": local.date().isoformat(),
        "time": f"{local:%H:%M}",
        "staff_id": str(staff_id),
        "staff_name": staff_name,
        "branch_id": str(branch_id) if branch_id else None,
    }


def advance_lead_stage(
    ctx: ToolContext, stage: str, *, reason: str = "", service_id: uuid.UUID | None = None
) -> str | None:
    """Move the contact's open lead forward (never backwards, except to 'lost'). Returns the new stage."""
    if ctx.contact_id is None or ctx.dry_run:
        return None
    from app.services import crm

    lead = crm.open_or_get_lead(
        ctx.db, ctx.tenant_id, ctx.contact_id, source=ctx.channel, service_id=service_id
    )
    if service_id and lead.service_id is None:
        lead.service_id = service_id
    current = LEAD_STAGE_RANK.get(lead.stage, 0)
    if stage == "lost":
        if lead.stage in ("booked", "visited"):
            return lead.stage
    elif LEAD_STAGE_RANK.get(stage, 0) <= current:
        return lead.stage
    crm.set_lead_stage(ctx.db, ctx.tenant_id, lead.id, stage, reason=reason, actor_type="ai", actor_id=None)
    return stage


def allowed_tags() -> list[str]:
    from app.services import crm

    return [t for t in AI_ALLOWED_TAGS if crm.is_allowed_auto_tag(t)]


# ---------------------------------------------------------------- tool implementations
def _list_services(ctx: ToolContext, p: ListServicesIn) -> dict[str, Any]:
    q = select(Service).where(
        Service.tenant_id == ctx.tenant_id, Service.published.is_(True), Service.active.is_(True)
    )
    if p.branch_id:
        q = q.where(
            Service.id.in_(
                select(StaffService.service_id)
                .join(Staff, Staff.id == StaffService.staff_id)
                .where(
                    Staff.branch_id == p.branch_id, Staff.tenant_id == ctx.tenant_id, Staff.active.is_(True)
                )
            )
        )
    services = ctx.db.scalars(q.order_by(Service.category, Service.title_ru)).all()
    return {
        "services": [
            {
                "service_id": str(s.id),
                "title": _service_title(s, ctx.language),
                "category": s.category,
                "duration_min": s.duration_min,
                "price": _money(s.price),
            }
            for s in services
        ]
    }


def _get_slots(ctx: ToolContext, p: GetSlotsIn) -> dict[str, Any]:
    from app.services import booking as booking_svc

    date_to = p.date_to or p.date_from
    if p.date_from < ctx.today():
        raise ToolFailure("DATE_IN_PAST", "The requested date is in the past.")
    if date_to < p.date_from or (date_to - p.date_from).days > MAX_SLOT_RANGE_DAYS:
        raise ToolFailure("BAD_RANGE", f"date_to must be within {MAX_SLOT_RANGE_DAYS} days after date_from.")
    svc = ctx.db.scalar(
        select(Service).where(
            Service.id == p.service_id,
            Service.tenant_id == ctx.tenant_id,
            Service.published.is_(True),
            Service.active.is_(True),
        )
    )
    if svc is None:
        raise ToolFailure("UNKNOWN_SERVICE", "Service not found; call list_services.")
    slots = booking_svc.get_available_slots(
        ctx.db,
        ctx.tenant_id,
        service_id=p.service_id,
        date_from=p.date_from,
        date_to=date_to,
        staff_id=p.staff_id,
        branch_id=p.branch_id or None,
        limit=200,
    )
    views = [_slot_view(ctx, s) for s in slots]
    if p.preferred_time:
        hh, mm = (int(x) for x in p.preferred_time.split(":"))
        want = hh * 60 + mm

        def dist(v: dict[str, Any]) -> tuple[str, int]:
            h, m = (int(x) for x in v["time"].split(":"))
            return v["date"], abs(h * 60 + m - want)

        views.sort(key=dist)
    return {
        "service": _service_title(svc, ctx.language),
        "duration_min": svc.duration_min,
        "price": _money(svc.price),
        "slots": views[:MAX_SLOTS_RETURNED],
        "total_found": len(views),
    }


def _idempotency_key(ctx: ToolContext, slot_id: str) -> str:
    digest = hashlib.sha256(slot_id.encode()).hexdigest()[:16]
    return f"ai:{ctx.trigger_message_id or uuid.uuid4()}:{digest}"


def _domain_message(exc: DomainError) -> str:
    return str(exc.detail.get("message", "")) if isinstance(exc.detail, dict) else str(exc.detail)


def _alternatives(ctx: ToolContext, exc: DomainError) -> list[dict[str, Any]]:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    out = []
    for alt in (detail.get("alternatives") or [])[:5]:
        try:
            out.append(_slot_view(ctx, alt))
        except (KeyError, TypeError, ValueError):  # alternatives are best-effort
            log.debug("unparseable alternative slot %r", alt)
    return out


def _create_booking(ctx: ToolContext, p: CreateBookingIn) -> dict[str, Any]:
    from app.services import booking as booking_svc

    if ctx.dry_run:
        return {"dry_run": True, "booking_number": "BK-TEST", "status": "confirmed"}
    contact_id = _require_contact(ctx)
    tenant = ctx.db.get(Tenant, ctx.tenant_id)
    pending = bool(merged_settings(tenant).get("requires_admin_confirmation")) if tenant else False
    try:
        b = booking_svc.create_booking(
            ctx.db,
            ctx.tenant_id,
            contact_id=contact_id,
            service_id=p.service_id,
            slot_id=p.slot_id,
            idempotency_key=_idempotency_key(ctx, p.slot_id),
            channel=ctx.channel,
            source="ai_agent",
            actor_type="ai",
            actor_id=None,
            comment=p.comment,
            status="pending" if pending else "confirmed",
        )
    except DomainError as exc:
        raise ToolFailure(exc.code, _domain_message(exc), alternatives=_alternatives(ctx, exc)) from exc
    ctx.booking_done = True
    return booking_view(ctx, b)


def _reschedule(ctx: ToolContext, p: RescheduleIn) -> dict[str, Any]:
    from app.services import booking as booking_svc

    b = _own_booking(ctx, p.booking)
    if ctx.dry_run:
        return {"dry_run": True, "booking_number": booking_number(b)}
    try:
        new = booking_svc.reschedule_booking(
            ctx.db,
            ctx.tenant_id,
            b.id,
            new_slot_id=p.new_slot_id,
            actor_type="ai",
            actor_id=None,
            contact_id=ctx.contact_id,
        )
    except DomainError as exc:
        raise ToolFailure(exc.code, _domain_message(exc), alternatives=_alternatives(ctx, exc)) from exc
    ctx.booking_done = True
    return {"previous_booking_number": booking_number(b), **booking_view(ctx, new)}


def _cancel(ctx: ToolContext, p: CancelIn) -> dict[str, Any]:
    from app.services import booking as booking_svc

    b = _own_booking(ctx, p.booking)
    if ctx.dry_run:
        return {"dry_run": True, "booking_number": booking_number(b), "status": "cancelled"}
    b = booking_svc.cancel_booking(
        ctx.db,
        ctx.tenant_id,
        b.id,
        reason=p.reason or "client request (AI)",
        actor_type="ai",
        actor_id=None,
        contact_id=ctx.contact_id,
    )
    ctx.booking_done = True
    return booking_view(ctx, b)


def _get_booking(ctx: ToolContext, p: BookingRefIn) -> dict[str, Any]:
    return booking_view(ctx, _own_booking(ctx, p.booking))


def _list_my_bookings(ctx: ToolContext, _: EmptyIn) -> dict[str, Any]:
    from app.services import booking as booking_svc

    if ctx.contact_id is None:
        return {"bookings": []}
    items = booking_svc.list_contact_bookings(ctx.db, ctx.tenant_id, ctx.contact_id, upcoming_only=True)
    return {"bookings": [booking_view(ctx, b) for b in items if b.contact_id == ctx.contact_id][:10]}


def _update_lead(ctx: ToolContext, p: UpdateLeadIn) -> dict[str, Any]:
    _require_contact(ctx)
    if ctx.dry_run:
        return {"dry_run": True, "stage": p.stage}
    if p.service_id and not ctx.db.scalar(
        select(Service.id).where(Service.id == p.service_id, Service.tenant_id == ctx.tenant_id)
    ):
        raise ToolFailure("UNKNOWN_SERVICE", "Service not found.")
    return {"stage": advance_lead_stage(ctx, p.stage, reason=p.reason, service_id=p.service_id)}


def _escalate(ctx: ToolContext, p: EscalateIn) -> dict[str, Any]:
    if ctx.dry_run:
        ctx.escalated = True
        return {"dry_run": True, "task_created": True}
    from app.services import escalation

    conv = None
    if ctx.conversation_id:
        conv = ctx.db.scalar(
            select(Conversation).where(
                Conversation.id == ctx.conversation_id, Conversation.tenant_id == ctx.tenant_id
            )
        )
    task = escalation.create_admin_task(
        ctx.db,
        ctx.tenant_id,
        question=p.question,
        conversation_id=ctx.conversation_id,
        contact_id=ctx.contact_id,
        kind="question",
        priority=p.priority,
        branch_id=conv.branch_id if conv else ctx.branch_id,
        payload={"source": "ai_agent", "language": ctx.language},
    )
    if conv is not None:
        conv.mode = "waiting_admin"
    ctx.escalated = True
    if ctx.contact_id:
        from app.services import crm

        crm.add_contact_tags(ctx.db, ctx.tenant_id, ctx.contact_id, [TAG_WAITING_ADMIN])
    return {"task_created": True, "task_number": getattr(task, "number", None)}


def _add_tag(ctx: ToolContext, p: AddTagIn) -> dict[str, Any]:
    contact_id = _require_contact(ctx)
    if p.tag not in allowed_tags():
        raise ToolFailure("TAG_NOT_ALLOWED", "This tag is not allowed.")
    if ctx.dry_run:
        return {"dry_run": True, "tag": p.tag}
    from app.services import crm

    crm.add_contact_tags(ctx.db, ctx.tenant_id, contact_id, [p.tag])
    return {"tag": p.tag}


def _set_name(ctx: ToolContext, p: SetNameIn) -> dict[str, Any]:
    contact_id = _require_contact(ctx)
    name = re.sub(r"[\r\n\t<>{}]", " ", p.name).strip()[:100]
    if not name:
        raise ToolFailure("INVALID_INPUT", "Empty name.")
    if ctx.dry_run:
        return {"dry_run": True, "name": name}
    contact = ctx.db.scalar(
        select(Contact).where(Contact.id == contact_id, Contact.tenant_id == ctx.tenant_id)
    )
    if contact is None:
        raise ToolFailure("NO_CLIENT", "Client not found.")
    contact.name = name
    return {"name": name}


_REGISTRY: dict[str, tuple[type[_In], Callable[[ToolContext, Any], dict[str, Any]]]] = {
    "list_services": (ListServicesIn, _list_services),
    "get_available_slots": (GetSlotsIn, _get_slots),
    "create_booking": (CreateBookingIn, _create_booking),
    "reschedule_booking": (RescheduleIn, _reschedule),
    "cancel_booking": (CancelIn, _cancel),
    "get_booking": (BookingRefIn, _get_booking),
    "list_my_bookings": (EmptyIn, _list_my_bookings),
    "update_lead": (UpdateLeadIn, _update_lead),
    "escalate_to_admin": (EscalateIn, _escalate),
    "add_tag": (AddTagIn, _add_tag),
    "set_contact_name": (SetNameIn, _set_name),
}
assert set(_REGISTRY) == {t["name"] for t in TOOL_DEFINITIONS}


AUDIT_MAX_STR = 500
AUDIT_MAX_KEYS = 20


def _audit_value(v: Any, depth: int = 0) -> Any:
    """Bounded copy of model-supplied input for the AIRun audit (a hostile model must not bloat it)."""
    if isinstance(v, str):
        return v if len(v) <= AUDIT_MAX_STR else v[:AUDIT_MAX_STR] + f"…[+{len(v) - AUDIT_MAX_STR}]"
    if isinstance(v, bool | int | float) or v is None:
        return v
    if depth >= 3:
        return "…"
    if isinstance(v, dict):
        items = list(v.items())[:AUDIT_MAX_KEYS]
        return {str(k)[:100]: _audit_value(x, depth + 1) for k, x in items}
    if isinstance(v, list | tuple):
        return [_audit_value(x, depth + 1) for x in list(v)[:AUDIT_MAX_KEYS]]
    return _audit_value(repr(v), depth)


def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> dict[str, Any]:
    """Validate and run one tool call. Always returns a JSON-serialisable dict with "ok"."""
    started = time.perf_counter()
    name = str(name)[:100]
    entry: dict[str, Any] = {
        "name": name,
        "input": _audit_value(raw_input) if isinstance(raw_input, dict) else {},
    }
    result: dict[str, Any]
    spec = _REGISTRY.get(name)
    if spec is None:
        result = {"ok": False, "error": "UNKNOWN_TOOL", "message": "Unknown tool."}
    else:
        model, fn = spec
        try:
            params = model.model_validate(raw_input or {})
        except ValidationError as exc:
            result = {
                "ok": False,
                "error": "INVALID_INPUT",
                "message": "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10]
                )[:1000],
            }
        else:
            sp = ctx.db.begin_nested()
            try:
                result = {"ok": True, **fn(ctx, params)}
                ctx.db.flush()
                sp.commit()
            except ToolFailure as exc:
                sp.rollback()
                result = {"ok": False, "error": exc.code, "message": exc.message, **exc.extra}
            except DomainError as exc:
                sp.rollback()
                result = {"ok": False, "error": exc.code, "message": _domain_message(exc)}
            except Exception:
                sp.rollback()
                log.exception("tool %s failed", name)
                result = {"ok": False, "error": "INTERNAL", "message": "Temporary error, try again later."}
    entry.update(ok=result["ok"], error=result.get("error"), ms=int((time.perf_counter() - started) * 1000))
    if result["ok"]:
        entry["result"] = {
            k: v
            for k, v in result.items()
            if k
            in (
                "booking_number",
                "status",
                "date",
                "time",
                "total_found",
                "task_number",
                "stage",
                "tag",
                "dry_run",
            )
        }
    ctx.calls.append(entry)
    return result


def tool_executor(ctx: ToolContext) -> Callable[[str, Any], dict[str, Any]]:
    return lambda name, raw: execute_tool(ctx, name, raw)
