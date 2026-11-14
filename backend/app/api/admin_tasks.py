"""Admin tasks (AI questions, complaints, schedule conflicts) — ТЗ §4.3, §12."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, require
from app.models import AdminTask, Contact, User
from app.services import escalation
from app.services.common import DomainError, get_or_404

router = APIRouter()


class TaskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kind: str = "question"
    priority: str = "normal"
    conversation_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class ResolveIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    relay_to_client: bool = False
    add_to_knowledge: bool = False


class PersonRef(BaseModel):
    id: uuid.UUID
    name: str
    phone_e164: str | None = None


class AssigneeRef(BaseModel):
    id: uuid.UUID
    full_name: str


class TaskOut(BaseModel):
    id: uuid.UUID
    number: int
    kind: str
    question: str
    priority: str
    status: str
    assignee: AssigneeRef | None
    contact: PersonRef | None
    conversation_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    escalation_level: int
    next_escalation_at: datetime | None
    sla_due_at: datetime | None
    created_at: datetime
    ack_at: datetime | None
    resolved_at: datetime | None
    answer: str
    add_to_knowledge: bool
    payload: dict


class TaskList(BaseModel):
    items: list[TaskOut]
    total: int


def task_out(db: Session, t: AdminTask) -> TaskOut:
    user = db.get(User, t.assignee_user_id) if t.assignee_user_id else None
    contact = db.get(Contact, t.contact_id) if t.contact_id else None
    payload = {k: v for k, v in (t.payload or {}).items() if k != "force_backup"}
    return TaskOut(
        id=t.id,
        number=t.number,
        kind=t.kind,
        question=t.question,
        priority=t.priority,
        status=t.status,
        assignee=AssigneeRef(id=user.id, full_name=user.full_name or user.email) if user else None,
        contact=PersonRef(id=contact.id, name=contact.name, phone_e164=contact.phone_e164)
        if contact
        else None,
        conversation_id=t.conversation_id,
        branch_id=t.branch_id,
        escalation_level=t.escalation_level,
        next_escalation_at=t.next_escalation_at,
        sla_due_at=t.sla_due_at,
        created_at=t.created_at,
        ack_at=t.ack_at,
        resolved_at=t.resolved_at,
        answer=t.answer,
        add_to_knowledge=t.add_to_knowledge,
        payload=payload,
    )


def _scoped(ctx: AuthContext, t: AdminTask) -> AdminTask:
    if not ctx.branch_allowed(t.branch_id):
        raise DomainError(404, "NOT_FOUND", "AdminTask not found")
    return t


@router.get("", response_model=TaskList)
def list_tasks(
    status: str | None = None,
    kind: str | None = None,
    conversation_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(require("tasks.read")),
    db: Session = Depends(get_db),
):
    q = select(AdminTask).where(AdminTask.tenant_id == ctx.tenant_id)
    if status:
        q = q.where(AdminTask.status.in_(status.split(",")))
    if kind:
        q = q.where(AdminTask.kind == kind)
    if conversation_id:
        q = q.where(AdminTask.conversation_id == conversation_id)
    if ctx.branch_scope:
        q = q.where(or_(AdminTask.branch_id.is_(None), AdminTask.branch_id.in_(ctx.branch_scope)))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(AdminTask.created_at.desc()).limit(limit).offset(offset)).all()
    return TaskList(items=[task_out(db, t) for t in rows], total=total or 0)


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: uuid.UUID, ctx: AuthContext = Depends(require("tasks.read")), db: Session = Depends(get_db)
):
    return task_out(db, _scoped(ctx, get_or_404(db, AdminTask, task_id, ctx.tenant_id)))


@router.post("", response_model=TaskOut, status_code=201)
def create_task(
    body: TaskIn, ctx: AuthContext = Depends(require("tasks.write")), db: Session = Depends(get_db)
):
    if body.branch_id and not ctx.branch_allowed(body.branch_id):
        raise DomainError(403, "BRANCH_FORBIDDEN", "No access to this branch")
    t = escalation.create_admin_task(
        db,
        ctx.tenant_id,
        question=body.question,
        kind=body.kind,
        priority=body.priority,
        conversation_id=body.conversation_id,
        contact_id=body.contact_id,
        branch_id=body.branch_id,
        payload={"created_by": str(ctx.user.id)},
    )
    db.commit()
    return task_out(db, t)


@router.post("/{task_id}/ack", response_model=TaskOut)
def ack(
    task_id: uuid.UUID, ctx: AuthContext = Depends(require("tasks.write")), db: Session = Depends(get_db)
):
    _scoped(ctx, get_or_404(db, AdminTask, task_id, ctx.tenant_id))
    t = escalation.ack_task(db, ctx.tenant_id, task_id, ctx.user.id, "crm")
    db.commit()
    return task_out(db, t)


@router.post("/{task_id}/resolve", response_model=TaskOut)
def resolve(
    task_id: uuid.UUID,
    body: ResolveIn,
    ctx: AuthContext = Depends(require("tasks.write")),
    db: Session = Depends(get_db),
):
    _scoped(ctx, get_or_404(db, AdminTask, task_id, ctx.tenant_id))
    t = escalation.resolve_task(
        db, ctx.tenant_id, task_id, ctx.user.id, body.answer, body.relay_to_client, body.add_to_knowledge
    )
    db.commit()
    return task_out(db, t)
