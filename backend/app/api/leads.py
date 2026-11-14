"""Leads funnel (ТЗ §10.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.bookings import ContactRef, parse_bound, tenant_zone
from app.db import get_db
from app.deps import AuthContext, require
from app.models import Contact, Lead, LeadStage, Membership
from app.services import crm
from app.services.audit import audit
from app.services.common import DomainError, get_or_404, mask_phone

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]

STAGE_ORDER = [s.value for s in LeadStage]


class LeadOut(BaseModel):
    id: uuid.UUID
    contact: ContactRef
    branch_id: uuid.UUID | None
    service_id: uuid.UUID | None
    booking_id: uuid.UUID | None
    source: str
    stage: str
    tags: list[str]
    lost_reason: str
    owner_user_id: uuid.UUID | None
    created_at: datetime
    closed_at: datetime | None


class LeadList(BaseModel):
    items: list[LeadOut]
    total: int


class LeadPatch(BaseModel):
    stage: LeadStage | None = None
    lost_reason: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = None
    owner_user_id: uuid.UUID | None = None


class FunnelStage(BaseModel):
    stage: str
    count: int


class FunnelOut(BaseModel):
    items: list[FunnelStage]
    total: int


def _serialize(db: Session, ctx: AuthContext, leads: list[Lead]) -> list[LeadOut]:
    contacts = {
        c.id: c
        for c in db.scalars(select(Contact).where(Contact.id.in_({lead.contact_id for lead in leads})))
    }
    out = []
    for lead in leads:
        c = contacts[lead.contact_id]
        phone = mask_phone(c.phone_e164) if ctx.role == "analyst" else c.phone_e164
        out.append(
            LeadOut(
                id=lead.id,
                contact=ContactRef(id=c.id, name=c.name, phone_e164=phone),
                branch_id=lead.branch_id,
                service_id=lead.service_id,
                booking_id=lead.booking_id,
                source=lead.source,
                stage=lead.stage,
                tags=list(lead.tags or []),
                lost_reason=lead.lost_reason,
                owner_user_id=lead.owner_user_id,
                created_at=lead.created_at,
                closed_at=lead.closed_at,
            )
        )
    return out


def _scoped(ctx: AuthContext, stmt):
    if ctx.branch_scope:
        scope = [uuid.UUID(b) for b in ctx.branch_scope]
        stmt = stmt.where((Lead.branch_id.is_(None)) | (Lead.branch_id.in_(scope)))
    return stmt


@router.get("", response_model=LeadList)
def list_leads(
    ctx: Annotated[AuthContext, Depends(require("contacts.read"))],
    db: DbSession,
    stage: Annotated[str | None, Query(description="Comma-separated stages")] = None,
    source: str | None = None,
    contact_id: uuid.UUID | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    zone = tenant_zone(db, ctx.tenant_id)
    stmt = _scoped(ctx, select(Lead).where(Lead.tenant_id == ctx.tenant_id))
    if stage:
        stmt = stmt.where(Lead.stage.in_([s.strip() for s in stage.split(",") if s.strip()]))
    if source:
        stmt = stmt.where(Lead.source == source)
    if contact_id is not None:
        stmt = stmt.where(Lead.contact_id == contact_id)
    if (start := parse_bound(date_from, zone, end=False)) is not None:
        stmt = stmt.where(Lead.created_at >= start)
    if (end := parse_bound(date_to, zone, end=True)) is not None:
        stmt = stmt.where(Lead.created_at < end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Lead.created_at.desc()).limit(limit).offset(offset)))
    return LeadList(items=_serialize(db, ctx, rows), total=total)


@router.get("/funnel", response_model=FunnelOut)
def funnel(
    ctx: Annotated[AuthContext, Depends(require("contacts.read"))],
    db: DbSession,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Leads created in the period grouped by their current stage, in funnel order."""
    zone = tenant_zone(db, ctx.tenant_id)
    stmt = _scoped(ctx, select(Lead.stage, func.count()).where(Lead.tenant_id == ctx.tenant_id))
    if (start := parse_bound(date_from, zone, end=False)) is not None:
        stmt = stmt.where(Lead.created_at >= start)
    if (end := parse_bound(date_to, zone, end=True)) is not None:
        stmt = stmt.where(Lead.created_at < end)
    counts = dict(db.execute(stmt.group_by(Lead.stage)).all())
    items = [FunnelStage(stage=s, count=int(counts.get(s, 0))) for s in STAGE_ORDER]
    return FunnelOut(items=items, total=sum(i.count for i in items))


@router.patch("/{lead_id}", response_model=LeadOut)
def patch_lead(
    ctx: Annotated[AuthContext, Depends(require("contacts.write"))],
    db: DbSession,
    lead_id: uuid.UUID,
    body: LeadPatch,
):
    lead = get_or_404(db, Lead, lead_id, ctx.tenant_id)
    if lead.branch_id is not None and not ctx.branch_allowed(lead.branch_id):
        raise DomainError(404, "NOT_FOUND", "Lead not found")
    actor = str(ctx.user.id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("owner_user_id") and not db.scalar(
        select(Membership.id).where(
            Membership.tenant_id == ctx.tenant_id, Membership.user_id == changes["owner_user_id"]
        )
    ):
        raise DomainError(422, "UNKNOWN_USER", "Owner is not a member of this organization")
    if "owner_user_id" in changes:
        lead.owner_user_id = changes["owner_user_id"]
    if changes.get("tags") is not None:
        lead.tags = list(dict.fromkeys(t.strip() for t in changes["tags"] if t.strip()))
    if changes.get("lost_reason") is not None:
        lead.lost_reason = changes["lost_reason"]
    if changes.get("stage") is not None:
        crm.set_lead_stage(
            db,
            ctx.tenant_id,
            lead.id,
            changes["stage"].value,
            reason=changes.get("lost_reason") or "",
            actor_type="user",
            actor_id=actor,
        )
    audit(
        db,
        ctx.tenant_id,
        "user",
        actor,
        "lead.update",
        "lead",
        str(lead.id),
        body.model_dump(mode="json", exclude_unset=True),
        ip=ctx.ip,
    )
    db.commit()
    db.refresh(lead)
    return _serialize(db, ctx, [lead])[0]
