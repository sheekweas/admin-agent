"""Audit log (owner only, ТЗ §14.2, §20)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import AuditList
from app.db import get_db
from app.deps import AuthContext, require
from app.models import AuditLog
from app.services.common import tz
from app.services.reports import default_tz_name

router = APIRouter()


@router.get("", response_model=AuditList)
def list_audit(
    entity: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    actor_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(require("audit.read")),
    db: Session = Depends(get_db),
):
    q = select(AuditLog).where(AuditLog.tenant_id == ctx.tenant_id)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    if action:  # "finance.*" -> prefix match
        q = q.where(
            AuditLog.action.like(action.replace("%", r"\%").replace("*", "%"))
            if "*" in action
            else AuditLog.action == action
        )
    if actor_id:
        q = q.where(AuditLog.actor_id == actor_id)
    zone = tz(default_tz_name(db, ctx.tenant_id))
    if date_from:
        q = q.where(AuditLog.created_at >= datetime.combine(date_from, time.min, zone))
    if date_to:
        q = q.where(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, zone))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)).all()
    return {
        "items": [
            {
                "id": a.id,
                "actor_type": a.actor_type,
                "actor_id": a.actor_id,
                "action": a.action,
                "entity": a.entity,
                "entity_id": a.entity_id,
                "payload": a.payload,
                "ip": a.ip,
                "created_at": a.created_at,
            }
            for a in rows
        ],
        "total": total,
    }
