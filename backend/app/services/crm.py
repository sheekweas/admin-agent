"""CRM: contacts, deduplication, merge, leads funnel, tags, opt-out, timeline (ТЗ §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AdminTask,
    Booking,
    Branch,
    Call,
    Contact,
    ContactChannel,
    ContactNote,
    Conversation,
    Lead,
    LeadStage,
    Message,
    Payment,
    Visit,
)
from app.services.audit import audit
from app.services.common import DomainError, emit_event, get_or_404, normalize_phone, utcnow

# ТЗ §10.3: AI may only *suggest* tags; the CRM accepts only these (plus "интересовался <услугой>").
ALLOWED_AUTO_TAGS: tuple[str, ...] = (
    "спросил цену",
    "хочет сегодня",
    "нет свободного слота",
    "ждёт администратора",
    "повторный клиент",
    "не пришёл",
    "отменил запись",
    "перенёс запись",
    "жалоба",
    "просит перезвонить",
    "голосовое сообщение",
)
INTEREST_TAG_PREFIX = "интересовался "

LEAD_STAGES = frozenset(s.value for s in LeadStage)
CLOSED_LEAD_STAGES = frozenset({"visited", "lost", "cancelled"})
CONTACT_SOURCES = frozenset({"ads", "site", "qr", "repeat", "call", "whatsapp", "manual"})
# Channels whose external id *is* the phone number.
_PHONE_CHANNELS = frozenset({"whatsapp", "phone", "voice", "call"})


def is_allowed_auto_tag(tag: str) -> bool:
    return tag in ALLOWED_AUTO_TAGS or (tag.startswith(INTEREST_TAG_PREFIX) and len(tag) <= 80)


# ---------------------------------------------------------------- contacts
def resolve_contact(db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
    """Tenant-scoped fetch that follows ``merged_into_id`` to the surviving card."""
    contact = get_or_404(db, Contact, contact_id, tenant_id)
    seen = {contact.id}
    while contact.merged_into_id is not None and contact.merged_into_id not in seen:
        contact = get_or_404(db, Contact, contact.merged_into_id, tenant_id)
        seen.add(contact.id)
    return contact


def find_contact_by_phone(db: Session, tenant_id: uuid.UUID, phone: str | None) -> Contact | None:
    phone_e164 = normalize_phone(phone)
    if phone_e164 is None:
        return None
    contact = db.scalar(
        select(Contact).where(Contact.tenant_id == tenant_id, Contact.phone_e164 == phone_e164)
    )
    return resolve_contact(db, tenant_id, contact.id) if contact else None


def get_or_create_contact_by_channel(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    channel: str,
    external_id: str,
    phone: str | None,
    name: str | None,
    language: str | None,
) -> Contact:
    """Dedupe by channel identity, then by normalized phone; create otherwise (ТЗ §10.1).

    Safe under concurrent webhooks (AT-02): unique-constraint races are retried inside a savepoint.
    """
    for _ in range(3):
        try:
            with db.begin_nested():
                contact = _get_or_create_contact(db, tenant_id, channel, external_id, phone, name, language)
            return contact
        except IntegrityError:
            continue  # a parallel transaction created the same contact/channel; re-read it
    raise DomainError(409, "CONTACT_CONFLICT", "Could not resolve contact identity")


def _get_or_create_contact(
    db: Session,
    tenant_id: uuid.UUID,
    channel: str,
    external_id: str,
    phone: str | None,
    name: str | None,
    language: str | None,
) -> Contact:
    identity = db.scalar(
        select(ContactChannel).where(
            ContactChannel.tenant_id == tenant_id,
            ContactChannel.channel == channel,
            ContactChannel.external_id == external_id,
        )
    )
    phone_e164 = normalize_phone(phone) or (
        normalize_phone(external_id) if channel in _PHONE_CHANNELS else None
    )
    contact: Contact | None = None
    if identity is not None:
        contact = resolve_contact(db, tenant_id, identity.contact_id)
    elif phone_e164:
        contact = find_contact_by_phone(db, tenant_id, phone_e164)

    if contact is None:
        contact = Contact(
            tenant_id=tenant_id,
            phone_e164=phone_e164,
            name=(name or "").strip()[:200],
            language=language or "ru",
            source=channel if channel in CONTACT_SOURCES else ("call" if channel == "voice" else "manual"),
            consent_flags={"processing": True},
        )
        db.add(contact)
        db.flush()
        audit(
            db, tenant_id, "system", None, "contact.create", "contact", str(contact.id), {"channel": channel}
        )
    else:
        if name and not contact.name:
            contact.name = name.strip()[:200]
        if phone_e164 and not contact.phone_e164:
            contact.phone_e164 = phone_e164
        if language:
            contact.language = language
    if identity is None:
        db.add(
            ContactChannel(
                tenant_id=tenant_id, contact_id=contact.id, channel=channel, external_id=external_id
            )
        )
    elif identity.contact_id != contact.id:
        identity.contact_id = contact.id  # identity pointed at a merged card
    contact.last_contact_at = utcnow()
    db.flush()
    return contact


def create_contact(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    name: str,
    phone: str | None,
    language: str = "ru",
    source: str = "manual",
    branch_id: uuid.UUID | None = None,
    actor_type: str = "user",
    actor_id: str | None = None,
) -> Contact:
    """Manual creation (admin panel). Refuses duplicates by normalized phone."""
    phone_e164 = normalize_phone(phone) if phone else None
    if phone and phone_e164 is None:
        raise DomainError(422, "BAD_PHONE", "Phone number is not valid")
    existing = find_contact_by_phone(db, tenant_id, phone_e164)
    if existing is not None:
        raise DomainError(
            409, "CONTACT_EXISTS", "Contact with this phone already exists", existing_id=str(existing.id)
        )
    if branch_id is not None:
        get_or_404(db, Branch, branch_id, tenant_id)  # no cross-tenant references
    contact = Contact(
        tenant_id=tenant_id,
        name=name.strip()[:200],
        phone_e164=phone_e164,
        language=language,
        source=source,
        branch_id=branch_id,
        consent_flags={"processing": True},
    )
    db.add(contact)
    db.flush()
    if phone_e164:
        db.add(
            ContactChannel(
                tenant_id=tenant_id, contact_id=contact.id, channel="phone", external_id=phone_e164
            )
        )
    audit(
        db, tenant_id, actor_type, actor_id, "contact.create", "contact", str(contact.id), {"source": source}
    )
    return contact


def add_contact_tags(db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID, tags: list[str]) -> None:
    """Add auto-tags (ТЗ §10.3). Tags outside ALLOWED_AUTO_TAGS are ignored: the CRM owns the rules."""
    contact = resolve_contact(db, tenant_id, contact_id)
    current = list(contact.tags or [])
    added = [t for t in dict.fromkeys(t.strip() for t in tags) if is_allowed_auto_tag(t) and t not in current]
    if added:
        contact.tags = current + added
        audit(db, tenant_id, "system", None, "contact.tags_add", "contact", str(contact.id), {"tags": added})


def set_opt_out(
    db: Session,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    opted_out: bool,
    actor_type: str,
    actor_id: str | None,
) -> None:
    """AT-13: opting out also withdraws marketing consent; transactional messages remain allowed."""
    contact = resolve_contact(db, tenant_id, contact_id)
    if contact.opted_out == opted_out:
        return
    contact.opted_out = opted_out
    if opted_out:
        contact.consent_flags = {**(contact.consent_flags or {}), "marketing": False, "outbound_calls": False}
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "contact.opt_out" if opted_out else "contact.opt_in",
        "contact",
        str(contact.id),
        {},
    )


def add_note(
    db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID, text: str, author_id: uuid.UUID
) -> ContactNote:
    contact = resolve_contact(db, tenant_id, contact_id)
    note = ContactNote(tenant_id=tenant_id, contact_id=contact.id, author_id=author_id, text=text)
    db.add(note)
    db.flush()
    audit(
        db,
        tenant_id,
        "user",
        str(author_id),
        "contact.note_add",
        "contact",
        str(contact.id),
        {"note_id": str(note.id)},
    )
    return note


def merge_contacts(
    db: Session,
    tenant_id: uuid.UUID,
    primary_id: uuid.UUID,
    other_id: uuid.UUID,
    *,
    actor_type: str,
    actor_id: str | None,
) -> Contact:
    """Controlled merge (ТЗ §10.1): all of ``other`` moves to ``primary``; ``other`` is soft-deleted."""
    primary = resolve_contact(db, tenant_id, primary_id)
    other = resolve_contact(db, tenant_id, other_id)
    if primary.id == other.id:
        raise DomainError(422, "SAME_CONTACT", "Cannot merge a contact into itself")

    moved: dict[str, int] = {}
    for model in (Booking, Lead, Conversation, ContactChannel, ContactNote, Visit, Payment, AdminTask, Call):
        res = db.execute(
            update(model)
            .where(model.tenant_id == tenant_id, model.contact_id == other.id)
            .values(contact_id=primary.id)
            .execution_options(synchronize_session="fetch")
        )
        moved[model.__tablename__] = res.rowcount

    primary.tags = list(dict.fromkeys([*(primary.tags or []), *(other.tags or [])]))
    primary.preferences = {**(other.preferences or {}), **(primary.preferences or {})}
    primary.opted_out = primary.opted_out or other.opted_out
    primary.name = primary.name or other.name
    primary.branch_id = primary.branch_id or other.branch_id
    if other.last_contact_at and (
        primary.last_contact_at is None or other.last_contact_at > primary.last_contact_at
    ):
        primary.last_contact_at = other.last_contact_at
    other_phone = other.phone_e164
    other.phone_e164 = None  # release the unique (tenant_id, phone_e164) slot
    other.merged_into_id = primary.id
    other.deleted_at = utcnow()
    db.flush()
    if primary.phone_e164 is None and other_phone:
        primary.phone_e164 = other_phone
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "contact.merge",
        "contact",
        str(primary.id),
        {"merged_id": str(other.id), "merged_phone": other_phone, "moved": moved},
    )
    db.flush()
    return primary


# ---------------------------------------------------------------- leads
def _has_past_visit(db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(select(exists().where(Visit.tenant_id == tenant_id, Visit.contact_id == contact_id)))
    )


def create_lead(
    db: Session,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    source: str,
    service_id: uuid.UUID | None = None,
) -> Lead:
    """New lead; returning clients (with a past visit) enter the funnel at stage ``repeat`` (ТЗ §10.2)."""
    contact = resolve_contact(db, tenant_id, contact_id)
    stage = "repeat" if _has_past_visit(db, tenant_id, contact.id) else "new"
    lead = Lead(
        tenant_id=tenant_id,
        contact_id=contact.id,
        branch_id=contact.branch_id,
        service_id=service_id,
        source=source,
        stage=stage,
        tags=[],
    )
    db.add(lead)
    db.flush()
    if stage == "repeat":
        add_contact_tags(db, tenant_id, contact.id, ["повторный клиент"])
    emit_event(
        db,
        tenant_id,
        "lead.created",
        "lead",
        lead.id,
        {"lead_id": str(lead.id), "contact_id": str(contact.id), "source": source, "stage": stage},
    )
    audit(
        db, tenant_id, "system", None, "lead.create", "lead", str(lead.id), {"stage": stage, "source": source}
    )
    return lead


def open_or_get_lead(
    db: Session,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    source: str,
    service_id: uuid.UUID | None = None,
) -> Lead:
    """Reuse the contact's open lead (no duplicates); start a new one after the previous lead closed."""
    contact = resolve_contact(db, tenant_id, contact_id)
    db.flush()
    lead = db.scalar(
        select(Lead)
        .where(
            Lead.tenant_id == tenant_id, Lead.contact_id == contact.id, Lead.stage.not_in(CLOSED_LEAD_STAGES)
        )
        .order_by(Lead.created_at.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lead is None:
        return create_lead(db, tenant_id, contact.id, source=source, service_id=service_id)
    if service_id and lead.service_id is None:
        lead.service_id = service_id
    return lead


def set_lead_stage(
    db: Session,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    stage: str,
    *,
    reason: str = "",
    actor_type: str = "system",
    actor_id: str | None = None,
) -> Lead:
    if stage not in LEAD_STAGES:
        raise DomainError(422, "BAD_STAGE", f"Unknown lead stage: {stage}")
    lead = get_or_404(db, Lead, lead_id, tenant_id)
    previous = lead.stage
    if previous == stage:
        return lead
    lead.stage = stage
    if stage in ("lost", "cancelled") and reason:
        lead.lost_reason = reason[:500]
    lead.closed_at = utcnow() if stage in CLOSED_LEAD_STAGES else None
    emit_event(
        db,
        tenant_id,
        "lead.status_changed",
        "lead",
        lead.id,
        {
            "lead_id": str(lead.id),
            "contact_id": str(lead.contact_id),
            "from": previous,
            "to": stage,
            "reason": reason,
        },
    )
    audit(
        db,
        tenant_id,
        actor_type,
        actor_id,
        "lead.stage",
        "lead",
        str(lead.id),
        {"from": previous, "to": stage, "reason": reason},
    )
    return lead


# ---------------------------------------------------------------- timeline
def _entry(
    kind: str,
    at: datetime,
    ref_id: uuid.UUID,
    title: str,
    text: str = "",
    amount: Decimal | None = None,
) -> dict[str, Any]:
    """Timeline item contract shared with the admin panel (docs/API.md, CRM)."""
    return {
        "kind": kind,
        "at": at.isoformat(),
        "title": title,
        "text": text,
        "amount": str(amount) if amount is not None else None,
        "ref_id": str(ref_id),
    }


def contact_timeline(
    db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID, limit: int = 200
) -> list[dict[str, Any]]:
    """Merged history of a client card, newest first (ТЗ §10.1).

    Items: ``{kind: message|booking|visit|payment|task|note|lead, at, title, text, amount, ref_id}``.
    """
    contact = resolve_contact(db, tenant_id, contact_id)
    entries: list[tuple[datetime, dict[str, Any]]] = []

    def add(
        at: datetime | None,
        kind: str,
        ref_id: uuid.UUID,
        title: str,
        text: str | None = "",
        amount: Decimal | None = None,
    ) -> None:
        if at is not None:
            entries.append((at, _entry(kind, at, ref_id, title, text or "", amount)))

    def recent(model, order_col):
        return db.scalars(
            select(model)
            .where(model.tenant_id == tenant_id, model.contact_id == contact.id)
            .order_by(order_col.desc())
            .limit(limit)
        )

    messages = db.execute(
        select(Message, Conversation.channel)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.tenant_id == tenant_id, Conversation.contact_id == contact.id)
        .order_by(Message.received_at.desc())
        .limit(limit)
    ).all()
    for m, channel in messages:
        title = "internal_note" if m.is_internal_note else f"{channel}:{m.direction}:{m.author}"
        add(m.received_at, "message", m.id, title, m.text)
    for b in recent(Booking, Booking.created_at):
        add(
            b.created_at,
            "booking",
            b.id,
            f"BK-{b.number:06d} · {b.status}",
            b.start_at.isoformat(),
            b.total_price,
        )
    for v in recent(Visit, Visit.visited_at):
        add(v.visited_at, "visit", v.id, "visit", v.comment, v.amount_due)
    for p in recent(Payment, Payment.paid_at):
        add(p.paid_at, "payment", p.id, f"{p.kind} · {p.method} · {p.status}", p.comment, p.amount)
    for t in recent(AdminTask, AdminTask.created_at):
        add(t.created_at, "task", t.id, f"#{t.number} {t.kind} · {t.status}", t.question)
    for n in recent(ContactNote, ContactNote.created_at):
        add(n.created_at, "note", n.id, "note", n.text)
    for lead in recent(Lead, Lead.created_at):
        add(lead.created_at, "lead", lead.id, f"{lead.stage} · {lead.source}", lead.lost_reason)
    entries.sort(key=lambda e: e[0], reverse=True)
    return [data for _, data in entries[:limit]]
