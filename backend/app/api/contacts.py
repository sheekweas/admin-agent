"""CRM contacts: search, card, timeline, notes, merge, CSV export (ТЗ §10.1, §20)."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, require
from app.models import Contact, Membership
from app.services import crm
from app.services.audit import audit
from app.services.common import DomainError, mask_phone, normalize_phone

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]

EXPORT_LIMIT = 50_000


class ContactOut(BaseModel):
    id: uuid.UUID
    name: str
    phone_e164: str | None
    language: str
    source: str
    branch_id: uuid.UUID | None
    tags: list[str]
    preferences: dict
    consent_flags: dict
    opted_out: bool
    owner_user_id: uuid.UUID | None
    last_contact_at: datetime | None
    created_at: datetime


class ContactList(BaseModel):
    items: list[ContactOut]
    total: int


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    language: str = Field(default="ru", pattern="^(ru|kk)$")
    source: str = Field(default="manual", pattern="^(ads|site|qr|repeat|call|whatsapp|manual)$")
    branch_id: uuid.UUID | None = None
    tags: list[str] = []


class ContactPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    language: str | None = Field(default=None, pattern="^(ru|kk)$")
    source: str | None = Field(default=None, pattern="^(ads|site|qr|repeat|call|whatsapp|manual)$")
    branch_id: uuid.UUID | None = None
    tags: list[str] | None = None
    preferences: dict | None = None
    consent_flags: dict | None = None
    opted_out: bool | None = None
    owner_user_id: uuid.UUID | None = None


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class NoteOut(BaseModel):
    id: uuid.UUID
    text: str
    author_id: uuid.UUID | None
    created_at: datetime


class MergeIn(BaseModel):
    other_id: uuid.UUID


class TimelineOut(BaseModel):
    items: list[dict]
    total: int


def _out(ctx: AuthContext, c: Contact) -> ContactOut:
    """Analysts get masked phone numbers (ТЗ §2: limited access to personal data)."""
    phone = mask_phone(c.phone_e164) if ctx.role == "analyst" else c.phone_e164
    return ContactOut(
        id=c.id,
        name=c.name,
        phone_e164=phone,
        language=c.language,
        source=c.source,
        branch_id=c.branch_id,
        tags=list(c.tags or []),
        preferences=c.preferences or {},
        consent_flags=c.consent_flags or {},
        opted_out=c.opted_out,
        owner_user_id=c.owner_user_id,
        last_contact_at=c.last_contact_at,
        created_at=c.created_at,
    )


def _visible(db: Session, ctx: AuthContext, contact_id: uuid.UUID) -> Contact:
    contact = crm.resolve_contact(db, ctx.tenant_id, contact_id)
    if contact.branch_id is not None and not ctx.branch_allowed(contact.branch_id):
        raise DomainError(404, "NOT_FOUND", "Contact not found")
    return contact


def _filtered(
    ctx: AuthContext,
    q: str | None,
    tag: str | None,
    source: str | None,
    branch_id: uuid.UUID | None,
    opted_out: bool | None,
):
    stmt = select(Contact).where(
        Contact.tenant_id == ctx.tenant_id, Contact.merged_into_id.is_(None), Contact.deleted_at.is_(None)
    )
    if q and q.strip():
        term = q.strip()
        digits = "".join(ch for ch in term if ch.isdigit())
        conditions = [Contact.name.ilike(f"%{term}%")]
        if len(digits) >= 3:
            conditions.append(Contact.phone_e164.contains(digits))
        stmt = stmt.where(or_(*conditions))
    if tag:
        stmt = stmt.where(type_coerce(Contact.tags, JSONB).contains([tag]))
    if source:
        stmt = stmt.where(Contact.source == source)
    if branch_id is not None:
        stmt = stmt.where(Contact.branch_id == branch_id)
    if ctx.branch_scope:
        scope = [uuid.UUID(b) for b in ctx.branch_scope]
        stmt = stmt.where(or_(Contact.branch_id.is_(None), Contact.branch_id.in_(scope)))
    if opted_out is not None:
        stmt = stmt.where(Contact.opted_out.is_(opted_out))
    return stmt


@router.get("", response_model=ContactList)
def list_contacts(
    ctx: Annotated[AuthContext, Depends(require("contacts.read"))],
    db: DbSession,
    q: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    branch_id: uuid.UUID | None = None,
    opted_out: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = _filtered(ctx, q, tag, source, branch_id, opted_out)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Contact.last_contact_at.desc().nulls_last(), Contact.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return ContactList(items=[_out(ctx, c) for c in rows], total=total)


@router.post("", response_model=ContactOut, status_code=201)
def create_contact(
    ctx: Annotated[AuthContext, Depends(require("contacts.write"))], db: DbSession, body: ContactIn
):
    contact = crm.create_contact(
        db,
        ctx.tenant_id,
        name=body.name,
        phone=body.phone,
        language=body.language,
        source=body.source,
        branch_id=body.branch_id,
        actor_id=str(ctx.user.id),
    )
    contact.tags = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
    db.commit()
    db.refresh(contact)
    return _out(ctx, contact)


def _csv_cell(value: object) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@router.get("/export.csv")
def export_contacts(
    ctx: Annotated[AuthContext, Depends(require("contacts.export"))],
    db: DbSession,
    q: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    branch_id: uuid.UUID | None = None,
    opted_out: bool | None = None,
):
    """Owner-only export; every export is audited (ТЗ §20.1)."""
    stmt = _filtered(ctx, q, tag, source, branch_id, opted_out).order_by(Contact.created_at)
    rows = db.scalars(stmt.limit(EXPORT_LIMIT)).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "name",
            "phone_e164",
            "language",
            "source",
            "tags",
            "opted_out",
            "created_at",
            "last_contact_at",
        ]
    )
    for c in rows:
        writer.writerow(
            [
                _csv_cell(v)
                for v in (
                    c.id,
                    c.name,
                    c.phone_e164,
                    c.language,
                    c.source,
                    ";".join(c.tags or []),
                    c.opted_out,
                    c.created_at.isoformat() if c.created_at else "",
                    c.last_contact_at.isoformat() if c.last_contact_at else "",
                )
            ]
        )
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "contact.export",
        "contact",
        None,
        {
            "count": len(rows),
            "filters": {
                "q": q,
                "tag": tag,
                "source": source,
                "branch_id": str(branch_id) if branch_id else None,
            },
        },
        ip=ctx.ip,
    )
    db.commit()
    return Response(
        content="﻿" + buf.getvalue(),  # BOM so Excel opens UTF-8 Cyrillic correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


@router.get("/{contact_id}", response_model=ContactOut)
def get_contact(
    ctx: Annotated[AuthContext, Depends(require("contacts.read"))], db: DbSession, contact_id: uuid.UUID
):
    return _out(ctx, _visible(db, ctx, contact_id))


@router.patch("/{contact_id}", response_model=ContactOut)
def patch_contact(
    ctx: Annotated[AuthContext, Depends(require("contacts.write"))],
    db: DbSession,
    contact_id: uuid.UUID,
    body: ContactPatch,
):
    contact = _visible(db, ctx, contact_id)
    changes = body.model_dump(exclude_unset=True)
    actor = str(ctx.user.id)
    if "opted_out" in changes:
        opted_out = changes.pop("opted_out")
        if opted_out is not None:
            crm.set_opt_out(db, ctx.tenant_id, contact.id, opted_out, "user", actor)
    if "phone" in changes:
        raw = changes.pop("phone")
        phone = normalize_phone(raw) if raw else None
        if raw and phone is None:
            raise DomainError(422, "BAD_PHONE", "Phone number is not valid")
        duplicate = crm.find_contact_by_phone(db, ctx.tenant_id, phone)
        if duplicate is not None and duplicate.id != contact.id:
            raise DomainError(
                409, "CONTACT_EXISTS", "Another contact has this phone", existing_id=str(duplicate.id)
            )
        contact.phone_e164 = phone
    if changes.get("owner_user_id") and not db.scalar(
        select(Membership.id).where(
            Membership.tenant_id == ctx.tenant_id, Membership.user_id == changes["owner_user_id"]
        )
    ):
        raise DomainError(422, "UNKNOWN_USER", "Owner is not a member of this organization")
    for key, value in changes.items():
        if key == "tags" and value is not None:
            value = list(dict.fromkeys(t.strip() for t in value if t.strip()))
        if value is not None or key in ("branch_id", "owner_user_id"):
            setattr(contact, key, value)
    audit(
        db,
        ctx.tenant_id,
        "user",
        actor,
        "contact.update",
        "contact",
        str(contact.id),
        body.model_dump(mode="json", exclude_unset=True),
        ip=ctx.ip,
    )
    db.commit()
    return _out(ctx, contact)


@router.get("/{contact_id}/timeline", response_model=TimelineOut)
def contact_timeline(
    ctx: Annotated[AuthContext, Depends(require("contacts.read"))],
    db: DbSession,
    contact_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    contact = _visible(db, ctx, contact_id)
    items = crm.contact_timeline(db, ctx.tenant_id, contact.id, limit=limit)
    return TimelineOut(items=items, total=len(items))


@router.post("/{contact_id}/notes", response_model=NoteOut, status_code=201)
def add_note(
    ctx: Annotated[AuthContext, Depends(require("contacts.write"))],
    db: DbSession,
    contact_id: uuid.UUID,
    body: NoteIn,
):
    contact = _visible(db, ctx, contact_id)
    note = crm.add_note(db, ctx.tenant_id, contact.id, body.text, ctx.user.id)
    db.commit()
    db.refresh(note)
    return NoteOut(id=note.id, text=note.text, author_id=note.author_id, created_at=note.created_at)


@router.post("/{contact_id}/merge", response_model=ContactOut)
def merge_contact(
    ctx: Annotated[AuthContext, Depends(require("contacts.write"))],
    db: DbSession,
    contact_id: uuid.UUID,
    body: MergeIn,
):
    """Merge ``other_id`` into this contact (ТЗ §10.1: controlled merge)."""
    primary = _visible(db, ctx, contact_id)
    other = _visible(db, ctx, body.other_id)
    merged = crm.merge_contacts(
        db, ctx.tenant_id, primary.id, other.id, actor_type="user", actor_id=str(ctx.user.id)
    )
    db.commit()
    return _out(ctx, merged)
