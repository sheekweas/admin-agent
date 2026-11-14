"""Outbox dispatcher (ТЗ §18.2, AT-10).

Handlers are DB-only: they turn an event into ``Notification`` rows (Telegram / client WhatsApp / webhooks)
with deterministic dedupe keys, so re-processing an event never duplicates anything. External delivery and
its retries happen in ``notifications.send_due_notifications``. If a handler fails, the event stays
``pending`` with exponential backoff (``next_attempt_at``) and becomes ``failed`` after OUTBOX_MAX_ATTEMPTS
(visible and manually retryable in GET /v1/integrations/events?source=outbox).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AdminTask, Booking, Contact, OutboxEvent, ShiftRequest, Staff, Tenant
from app.services import escalation, webhooks_out
from app.services import notifications as nt
from app.services.common import utcnow

log = logging.getLogger("outbox")

OUTBOX_MAX_ATTEMPTS = 10
SHIFT_KIND_RU = {
    "day_off": "выходной",
    "vacation": "отпуск",
    "sick": "больничный",
    "early_leave": "ранний уход",
    "late_start": "поздний приход",
    "overtime": "доп. время",
    "block": "блокировка",
}


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _lang(db: Session, tenant_id: uuid.UUID) -> str:
    return nt.lang_of(db.get(Tenant, tenant_id).default_language)


def _load(db: Session, model, ev: OutboxEvent, key: str):
    obj_id = ev.aggregate_id or _uuid((ev.payload or {}).get(key))
    obj = db.get(model, obj_id) if obj_id else None
    return obj if obj is not None and obj.tenant_id == ev.tenant_id else None


# ====================================================================== handlers
def _booking_alert(db: Session, ev: OutboxEvent, booking: Booking, kind: str, key: str, **extra: Any) -> None:
    lang = _lang(db, ev.tenant_id)
    ctx = {**nt.booking_context(db, booking, lang), **extra}
    text = nt.booking_text_with_link(nt.render(kind, lang, **ctx), booking)
    nt.notify_admins(
        db,
        ev.tenant_id,
        kind,
        text,
        buttons=nt.booking_buttons(db, booking, lang),
        branch_id=booking.branch_id,
        dedupe_key=key,
        payload={"booking_id": str(booking.id)},
        event_id=ev.id,
    )


def _client_confirmation(db: Session, ev: OutboxEvent, booking: Booking) -> None:
    # Changes made by the AI agent were already confirmed in the chat itself (ТЗ §26.1). A reschedule keeps
    # the original source, so for it the actor decides: an admin moving an AI booking must notify the client.
    actor = (ev.payload or {}).get("actor_type")
    by_ai = (
        actor == "ai" if ev.event_type == "booking.rescheduled" and actor else booking.source == "ai_agent"
    )
    if by_ai:
        return
    nt.queue_client_notification(
        db,
        ev.tenant_id,
        kind="booking_confirmation",
        contact_id=booking.contact_id,
        payload={"booking_id": str(booking.id)},
        dedupe_key=f"confirm:{booking.id}",
        event_id=ev.id,
    )


def handle_booking(db: Session, ev: OutboxEvent) -> None:
    b = _load(db, Booking, ev, "booking_id")
    if b is None:
        return
    t = ev.event_type
    if t == "booking.created" and b.status == "pending":
        _booking_alert(db, ev, b, "pending_booking", f"booking:{b.id}:pending")
    elif t == "booking.confirmed":
        if b.status in nt.ACTIVE_FOR_REMINDER:
            _booking_alert(db, ev, b, "new_booking", f"booking:{b.id}:confirmed")
            _client_confirmation(db, ev, b)
            nt.schedule_booking_reminders(db, ev.tenant_id, b)
    elif t == "booking.rescheduled":
        old = db.get(Booking, b.rescheduled_from_id) if b.rescheduled_from_id else None
        if old is not None:
            nt.cancel_booking_reminders(db, ev.tenant_id, old.id)
            lang = _lang(db, ev.tenant_id)
            octx = nt.booking_context(db, old, lang)
            _booking_alert(
                db,
                ev,
                b,
                "rescheduled",
                f"booking:{b.id}:rescheduled",
                old_number=octx["number"],
                old_date=octx["date"],
                old_time=octx["time"],
            )
        if b.status in nt.ACTIVE_FOR_REMINDER:
            _client_confirmation(db, ev, b)
            nt.schedule_booking_reminders(db, ev.tenant_id, b)
    elif t == "booking.cancelled":
        nt.cancel_booking_reminders(db, ev.tenant_id, b.id)
        _booking_alert(db, ev, b, "cancelled", f"booking:{b.id}:cancelled")
    elif t == "booking.status_changed":
        if b.status in nt.ACTIVE_FOR_REMINDER:
            nt.schedule_booking_reminders(db, ev.tenant_id, b)
        else:
            nt.cancel_booking_reminders(db, ev.tenant_id, b.id)


def handle_task(db: Session, ev: OutboxEvent) -> None:
    task = _load(db, AdminTask, ev, "task_id")
    if task is None:
        return
    if ev.event_type == "admin_task.created":
        escalation.send_initial_alert(db, task, event_id=ev.id)
    elif ev.event_type == "admin_task.acked":
        escalation.on_task_acked(db, task)


def handle_shift_request(db: Session, ev: OutboxEvent) -> None:
    req = _load(db, ShiftRequest, ev, "shift_request_id")
    if req is None or req.status != "pending":
        return
    staff = db.get(Staff, req.staff_id)
    lang = _lang(db, ev.tenant_id)
    new_time = "–".join(t.strftime("%H:%M") for t in (req.new_start, req.new_end) if t) or "—"
    text = nt.render(
        "shift_request",
        lang,
        staff=staff.name if staff else "—",
        date=req.date.strftime("%d.%m.%Y"),
        kind=SHIFT_KIND_RU.get(req.kind, req.kind) if lang == "ru" else req.kind,
        new_time=new_time,
        reason=req.reason or "—",
        conflicts=len(req.conflicts or []),
    )
    buttons = [
        [
            {"text": nt.label("approve", lang), "callback_data": f"shift:approve:{req.id}"},
            {"text": nt.label("reject", lang), "callback_data": f"shift:reject:{req.id}"},
        ]
    ]
    nt.notify_admins(
        db,
        ev.tenant_id,
        "shift_request",
        text,
        buttons=buttons,
        branch_id=staff.branch_id if staff else None,
        dedupe_key=f"shift:{req.id}",
        payload={"shift_request_id": str(req.id)},
        event_id=ev.id,
    )


def handle_delivery_failed(db: Session, ev: OutboxEvent) -> None:
    p = ev.payload or {}
    contact = db.get(Contact, _uuid(p.get("contact_id"))) if _uuid(p.get("contact_id")) else None
    client = "—"
    if contact is not None and contact.tenant_id == ev.tenant_id:
        client = f"{contact.name or ''} {nt.format_phone(contact.phone_e164)}".strip()
    lang = _lang(db, ev.tenant_id)
    text = nt.render(
        "delivery_failed",
        lang,
        client=client,
        channel=p.get("channel") or "whatsapp",
        error=str(p.get("error") or p.get("reason") or "—")[:300],
    )
    nt.notify_admins(
        db,
        ev.tenant_id,
        "delivery_failed",
        text,
        branch_id=contact.branch_id if contact is not None else None,
        dedupe_key=f"ev:{ev.id}",
        event_id=ev.id,
    )


HANDLERS = {
    "booking.created": handle_booking,
    "booking.confirmed": handle_booking,
    "booking.rescheduled": handle_booking,
    "booking.cancelled": handle_booking,
    "booking.status_changed": handle_booking,
    "admin_task.created": handle_task,
    "admin_task.acked": handle_task,
    "shift_request.created": handle_shift_request,
    "message.delivery_failed": handle_delivery_failed,
    # staff.schedule_changed conflicts arrive as schedule_conflict admin tasks (services.schedule)
}


def handle_event(db: Session, ev: OutboxEvent) -> None:
    handler = HANDLERS.get(ev.event_type)
    if handler is not None:
        handler(db, ev)
    webhooks_out.enqueue_for_event(db, ev)


# ====================================================================== dispatcher
def dispatch_outbox(
    db: Session, now: datetime | None = None, *, limit: int = 100, deliver: bool = True
) -> int:
    """Process due pending events (FOR UPDATE SKIP LOCKED, commit per event), then deliver the notifications
    they produced. Returns the number of processed events."""
    now = now or utcnow()
    ids = db.scalars(
        select(OutboxEvent.id)
        .where(
            OutboxEvent.status == "pending",
            or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
        )
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    ).all()
    db.commit()
    processed = 0
    for ev_id in ids:
        ev = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.id == ev_id, OutboxEvent.status == "pending")
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).first()
        if ev is None:
            db.rollback()
            continue
        sp = db.begin_nested()
        try:
            handle_event(db, ev)
            sp.commit()
            ev.status, ev.processed_at, ev.last_error, ev.next_attempt_at = "processed", now, None, None
            processed += 1
        except Exception as exc:
            sp.rollback()
            log.exception("outbox handler failed event=%s type=%s", ev.id, ev.event_type)
            ev.attempts = (ev.attempts or 0) + 1
            ev.last_error = f"{exc.__class__.__name__}: {exc}"[:1000]
            if ev.attempts >= OUTBOX_MAX_ATTEMPTS:
                ev.status, ev.next_attempt_at = "failed", None
            else:
                ev.next_attempt_at = now + nt.backoff(ev.attempts)
        db.commit()
    if deliver:
        nt.send_due_notifications(db, now)
    return processed


def retry_event(db: Session, tenant_id: uuid.UUID, event_id: uuid.UUID) -> OutboxEvent:
    from app.services.common import get_or_404

    ev = get_or_404(db, OutboxEvent, event_id, tenant_id)
    ev.status, ev.attempts, ev.next_attempt_at = "pending", 0, None
    return ev
