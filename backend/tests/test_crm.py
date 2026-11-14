"""CRM (ТЗ §10): dedupe, merge, leads funnel rules, auto-tags, opt-out, timeline."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog, Booking, Contact, ContactChannel, Conversation, Lead, OutboxEvent, Visit
from app.services import booking as booking_svc
from app.services import crm
from app.services.common import DomainError, utcnow
from tests.booking_helpers import at, future_day


def _wa(db, world, wa_id, name="Клиент", phone=None):
    c = crm.get_or_create_contact_by_channel(
        db, world.tenant.id, channel="whatsapp", external_id=wa_id, phone=phone, name=name, language="kk"
    )
    db.commit()
    return c


def test_dedupe_by_channel_identity_and_phone(db, world):
    a = _wa(db, world, "77015550000", name="Айгерим")
    b = _wa(db, world, "77015550000", name="Другое имя")
    assert a.id == b.id and b.name == "Айгерим" and b.phone_e164 == "+77015550000"
    assert db.query(ContactChannel).filter_by(contact_id=a.id).count() == 1

    # existing CRM contact (+77071234567) is matched by normalized phone from the WhatsApp id
    c = _wa(db, world, "77071234567")
    assert c.id == world.contact.id
    # telegram identity with the same phone in another format → same card, new channel identity
    t = crm.get_or_create_contact_by_channel(
        db,
        world.tenant.id,
        channel="telegram",
        external_id="tg-42",
        phone="+7 (707) 123-45-67",
        name=None,
        language=None,
    )
    db.commit()
    assert t.id == world.contact.id
    assert {
        ch.channel for ch in db.scalars(select(ContactChannel).where(ContactChannel.contact_id == t.id))
    } == {"whatsapp", "telegram"}


def test_tenant_isolation_of_dedupe(db, world, other_world):
    mine = _wa(db, world, "77015550001")
    theirs = crm.get_or_create_contact_by_channel(
        db,
        other_world.tenant.id,
        channel="whatsapp",
        external_id="77015550001",
        phone=None,
        name="X",
        language="ru",
    )
    db.commit()
    assert mine.id != theirs.id and theirs.tenant_id == other_world.tenant.id
    with pytest.raises(DomainError):
        crm.resolve_contact(db, other_world.tenant.id, mine.id)


def test_create_contact_rejects_duplicates(db, world):
    with pytest.raises(DomainError) as e:
        crm.create_contact(db, world.tenant.id, name="Дубль", phone="+7 707 123 45 67")
    assert e.value.code == "CONTACT_EXISTS" and e.value.detail["existing_id"] == str(world.contact.id)
    with pytest.raises(DomainError):
        crm.create_contact(db, world.tenant.id, name="x", phone="123")


def test_one_open_lead_reused_new_after_close(db, world):
    first = crm.open_or_get_lead(db, world.tenant.id, world.contact.id, source="whatsapp")
    again = crm.open_or_get_lead(db, world.tenant.id, world.contact.id, source="whatsapp")
    assert first.id == again.id and first.stage == "new"
    crm.set_lead_stage(db, world.tenant.id, first.id, "info_requested")
    crm.set_lead_stage(db, world.tenant.id, first.id, "lost", reason="дорого")
    db.commit()
    assert first.closed_at is not None and first.lost_reason == "дорого"
    second = crm.open_or_get_lead(db, world.tenant.id, world.contact.id, source="ads")
    db.commit()
    assert second.id != first.id and second.stage == "new"
    events = [e.event_type for e in db.scalars(select(OutboxEvent))]
    assert events.count("lead.created") == 2 and events.count("lead.status_changed") == 2
    with pytest.raises(DomainError):
        crm.set_lead_stage(db, world.tenant.id, second.id, "won")


def test_repeat_stage_for_returning_client(db, world):
    db.add(
        Visit(
            tenant_id=world.tenant.id,
            contact_id=world.contact.id,
            staff_id=world.barber1.id,
            branch_id=world.branch.id,
            visited_at=utcnow(),
        )
    )
    db.commit()
    lead = crm.open_or_get_lead(db, world.tenant.id, world.contact.id, source="whatsapp")
    db.commit()
    assert lead.stage == "repeat"
    db.refresh(world.contact)
    assert "повторный клиент" in world.contact.tags


def test_auto_tags_only_from_allowed_list(db, world):
    crm.add_contact_tags(
        db,
        world.tenant.id,
        world.contact.id,
        ["спросил цену", "VIP", "интересовался окрашиванием", "спросил цену"],
    )
    db.commit()
    db.refresh(world.contact)
    assert world.contact.tags == ["спросил цену", "интересовался окрашиванием"]
    assert {
        "спросил цену",
        "хочет сегодня",
        "нет свободного слота",
        "ждёт администратора",
        "повторный клиент",
        "не пришёл",
    } <= set(crm.ALLOWED_AUTO_TAGS)


def test_opt_out(db, world):
    """AT-13: opt-out flag set, marketing consent withdrawn, action audited."""
    crm.set_opt_out(db, world.tenant.id, world.contact.id, True, "ai", None)
    db.commit()
    db.refresh(world.contact)
    assert world.contact.opted_out is True
    assert world.contact.consent_flags["marketing"] is False and world.contact.consent_flags["processing"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "contact.opt_out")) is not None


def test_merge_moves_everything(db, world):
    dup = _wa(db, world, "77019998877", name="")
    dup.tags = ["хочет сегодня"]
    conv = Conversation(tenant_id=world.tenant.id, contact_id=dup.id)
    db.add(conv)
    db.commit()
    b = booking_svc.create_booking(
        db,
        world.tenant.id,
        contact_id=dup.id,
        service_id=world.haircut.id,
        start_at=at(future_day(), 12),
        staff_id=world.barber1.id,
        idempotency_key=None,
        channel="whatsapp",
        source="ai_agent",
        actor_type="ai",
        actor_id=None,
    )
    db.commit()

    primary = crm.merge_contacts(
        db, world.tenant.id, world.contact.id, dup.id, actor_type="user", actor_id=str(world.admin.id)
    )
    db.commit()
    db.refresh(dup)
    assert dup.merged_into_id == primary.id and dup.deleted_at is not None and dup.phone_e164 is None
    assert db.get(Booking, b.id).contact_id == primary.id
    assert db.get(Conversation, conv.id).contact_id == primary.id
    assert db.scalars(select(Lead).where(Lead.contact_id == dup.id)).all() == []
    assert db.query(ContactChannel).filter_by(contact_id=primary.id, external_id="77019998877").count() == 1
    assert "хочет сегодня" in primary.tags
    # the merged card resolves to the survivor; channel identity follows too
    assert crm.resolve_contact(db, world.tenant.id, dup.id).id == primary.id
    assert _wa(db, world, "77019998877").id == primary.id
    assert db.scalar(select(AuditLog).where(AuditLog.action == "contact.merge")).payload["merged_id"] == str(
        dup.id
    )
    with pytest.raises(DomainError):
        crm.merge_contacts(db, world.tenant.id, primary.id, primary.id, actor_type="user", actor_id=None)


def test_merge_takes_phone_when_primary_has_none(db, world):
    nophone = Contact(tenant_id=world.tenant.id, name="Без телефона")
    db.add(nophone)
    db.commit()
    crm.merge_contacts(db, world.tenant.id, nophone.id, world.contact.id, actor_type="user", actor_id=None)
    db.commit()
    assert db.get(Contact, nophone.id).phone_e164 == "+77071234567"


def test_timeline(db, world):
    b = booking_svc.create_booking(
        db,
        world.tenant.id,
        contact_id=world.contact.id,
        service_id=world.haircut.id,
        start_at=at(future_day(), 12),
        staff_id=world.barber1.id,
        idempotency_key=None,
        channel="admin",
        source="admin",
        actor_type="user",
        actor_id=None,
    )
    crm.add_note(db, world.tenant.id, world.contact.id, "любит короткие стрижки", world.admin.id)
    db.commit()
    items = crm.contact_timeline(db, world.tenant.id, world.contact.id)
    kinds = [i["kind"] for i in items]
    assert {"booking", "note", "lead"} <= set(kinds)
    booking_item = next(i for i in items if i["kind"] == "booking")
    assert booking_item["ref_id"] == str(b.id) and Decimal(booking_item["amount"]) == 5000
    assert booking_item["title"].startswith("BK-000001")
    note = next(i for i in items if i["kind"] == "note")
    assert note["text"] == "любит короткие стрижки" and note["amount"] is None
    assert all(set(i) == {"kind", "at", "title", "text", "amount", "ref_id"} for i in items)
    assert items == sorted(items, key=lambda i: i["at"], reverse=True)
