"""Inbox: conversations, messages, human takeover / release (ТЗ §11, AT-15) and the mock-mode simulator."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.channels.base import NormalizedMessage
from app.api.schemas import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    MessageList,
    MessageOut,
    SimulateOut,
    error_responses,
)
from app.config import get_settings
from app.db import get_db
from app.deps import AuthContext, require
from app.models import AdminTask, Contact, Conversation, Message
from app.services import messaging
from app.services.common import DomainError, get_or_404, normalize_phone

router = APIRouter()

DB = Annotated[Session, Depends(get_db)]
ConvRead = Annotated[AuthContext, Depends(require("conversations.read"))]
ConvWrite = Annotated[AuthContext, Depends(require("conversations.write"))]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    internal_note: bool = False


class SimulateIn(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    name: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=4096)


def _scoped(ctx: AuthContext, conv: Conversation) -> Conversation:
    if not ctx.branch_allowed(conv.branch_id):
        raise DomainError(404, "NOT_FOUND", "Conversation not found")
    return conv


def _get(db: Session, ctx: AuthContext, conversation_id: uuid.UUID) -> Conversation:
    return _scoped(ctx, get_or_404(db, Conversation, conversation_id, ctx.tenant_id))


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _items(db: Session, convs: list[Conversation]) -> list[dict[str, Any]]:
    ids = [c.id for c in convs]
    if not ids:
        return []
    contacts = {
        c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_({c.contact_id for c in convs})))
    }
    last = {
        m.conversation_id: m
        for m in db.scalars(
            select(Message)
            .ext(distinct_on(Message.conversation_id))
            .where(Message.conversation_id.in_(ids), Message.is_internal_note.is_(False))
            .order_by(Message.conversation_id, Message.received_at.desc())
        )
    }
    tasks: dict[uuid.UUID, uuid.UUID] = {}
    for conv_id, task_id in db.execute(
        select(AdminTask.conversation_id, AdminTask.id)
        .where(AdminTask.conversation_id.in_(ids), AdminTask.status.in_(("open", "acked")))
        .order_by(AdminTask.created_at)
    ):
        tasks[conv_id] = task_id
    out = []
    for c in convs:
        contact, m = contacts.get(c.contact_id), last.get(c.id)
        out.append(
            {
                "id": str(c.id),
                "contact": {
                    "id": str(contact.id),
                    "name": contact.name,
                    "phone_e164": contact.phone_e164,
                    "opted_out": contact.opted_out,
                }
                if contact
                else None,
                "channel": c.channel,
                "mode": c.mode,
                "status": c.status,
                "language": c.language,
                "branch_id": str(c.branch_id) if c.branch_id else None,
                "owner_user_id": str(c.owner_user_id) if c.owner_user_id else None,
                "last_message": {"text": m.text, "author": m.author, "at": _iso(m.received_at)}
                if m
                else None,
                "last_message_at": _iso(c.last_message_at),
                "unread_count": c.unread_count,
                "open_task_id": str(tasks[c.id]) if c.id in tasks else None,
            }
        )
    return out


@router.get("", response_model=ConversationList)
def list_conversations(
    *,
    mode: str | None = None,
    unread: bool | None = None,
    status: str | None = None,
    q: str | None = None,
    channel: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
    ctx: ConvRead,
    db: DB,
) -> dict[str, Any]:
    stmt = select(Conversation).where(Conversation.tenant_id == ctx.tenant_id)
    if mode:
        stmt = stmt.where(Conversation.mode.in_(mode.split(",")))
    if status:
        stmt = stmt.where(Conversation.status.in_(status.split(",")))
    if channel:
        stmt = stmt.where(Conversation.channel == channel)
    if unread is not None:
        stmt = stmt.where(Conversation.unread_count > 0 if unread else Conversation.unread_count == 0)
    if ctx.branch_scope:
        stmt = stmt.where(or_(Conversation.branch_id.is_(None), Conversation.branch_id.in_(ctx.branch_scope)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Conversation.contact_id.in_(
                select(Contact.id).where(
                    Contact.tenant_id == ctx.tenant_id,
                    or_(Contact.name.ilike(like), Contact.phone_e164.ilike(like)),
                )
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return {"items": _items(db, list(rows)), "total": total}


@router.get("/{conversation_id}", response_model=ConversationDetail, responses=error_responses(404))
def get_conversation(conversation_id: uuid.UUID, ctx: ConvRead, db: DB) -> dict[str, Any]:
    conv = _get(db, ctx, conversation_id)
    return {**_items(db, [conv])[0], "state": conv.state or {}}


@router.get("/{conversation_id}/messages", response_model=MessageList, responses=error_responses(404))
def list_messages(
    conversation_id: uuid.UUID, ctx: ConvRead, db: DB, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> dict[str, Any]:
    """Latest `limit` messages, returned oldest -> newest."""
    conv = _get(db, ctx, conversation_id)
    base = select(Message).where(Message.tenant_id == ctx.tenant_id, Message.conversation_id == conv.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(Message.received_at.desc()).limit(limit)).all()
    return {"items": [messaging.message_to_dict(m) for m in reversed(rows)], "total": total}


@router.post(
    "/{conversation_id}/messages",
    status_code=201,
    response_model=MessageOut,
    responses=error_responses(404, 409),
)
def post_message(conversation_id: uuid.UUID, body: MessageIn, ctx: ConvWrite, db: DB) -> dict[str, Any]:
    conv = _get(db, ctx, conversation_id)
    msg = messaging.human_reply(
        db, ctx.tenant_id, conv.id, body.text, user_id=ctx.user.id, internal_note=body.internal_note
    )
    db.commit()  # delivery runs right after COMMIT; return the real server-side delivery status
    db.refresh(msg)
    return messaging.message_to_dict(msg)


def _action(conversation_id: uuid.UUID, ctx: AuthContext, db: Session, fn: Any) -> dict[str, Any]:
    conv = _get(db, ctx, conversation_id)
    fn(conv)
    db.commit()
    return _items(db, [conv])[0]


@router.post("/{conversation_id}/takeover", response_model=ConversationOut, responses=error_responses(404))
def takeover(conversation_id: uuid.UUID, ctx: ConvWrite, db: DB) -> dict[str, Any]:
    return _action(
        conversation_id, ctx, db, lambda c: messaging.takeover(db, ctx.tenant_id, c.id, ctx.user.id)
    )


@router.post("/{conversation_id}/release", response_model=ConversationOut, responses=error_responses(404))
def release(conversation_id: uuid.UUID, ctx: ConvWrite, db: DB) -> dict[str, Any]:
    return _action(
        conversation_id, ctx, db, lambda c: messaging.release(db, ctx.tenant_id, c.id, ctx.user.id)
    )


@router.post("/{conversation_id}/read", response_model=ConversationOut, responses=error_responses(404))
def mark_read(conversation_id: uuid.UUID, ctx: ConvRead, db: DB) -> dict[str, Any]:
    return _action(conversation_id, ctx, db, lambda c: messaging.mark_read(db, ctx.tenant_id, c.id))


@router.post("/simulate", response_model=SimulateOut, responses=error_responses(404, 422))
def simulate(body: SimulateIn, ctx: ConvWrite, db: DB) -> dict[str, Any]:
    """Dev/demo only: inject an inbound WhatsApp message as if it came from `phone` (mock mode only)."""
    if get_settings().whatsapp_mode != "mock":
        raise DomainError(404, "NOT_FOUND", "Simulator is available only in WhatsApp mock mode")
    phone = normalize_phone(body.phone)
    if phone is None:
        raise DomainError(422, "BAD_PHONE", "Invalid phone number")
    nm = NormalizedMessage(
        tenant_id=ctx.tenant_id,
        channel="whatsapp",
        external_user_id=phone.lstrip("+"),
        provider_message_id=f"sim.{uuid.uuid4().hex}",
        text=body.text,
        contact_name=body.name or None,
        phone=phone,
        meta={"simulated_by": str(ctx.user.id)},
    )
    msg = messaging.handle_inbound(db, nm)
    db.commit()
    assert msg is not None
    messaging.run_ai_turn_now(msg.conversation_id, msg.id)
    db.expire_all()
    replies = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == msg.conversation_id,
            Message.tenant_id == ctx.tenant_id,
            Message.direction == "out",
            Message.received_at >= msg.received_at,
            Message.is_internal_note.is_(False),
        )
        .order_by(Message.received_at)
    ).all()
    conv = db.get(Conversation, msg.conversation_id)
    return {
        "conversation_id": str(msg.conversation_id),
        "message_id": str(msg.id),
        "mode": conv.mode if conv else None,
        "replies": [messaging.message_to_dict(m) for m in replies],
    }
