"""Calls (voice channel, ТЗ §13): list / detail / outbound AI call / mock-mode simulator."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import CallDetail, CallList, CallOut, error_responses
from app.db import get_db
from app.deps import AuthContext, require
from app.models import Call
from app.services import voice_calls
from app.services.common import get_or_404

router = APIRouter()

DB = Annotated[Session, Depends(get_db)]
CallsRead = Annotated[AuthContext, Depends(require("conversations.read"))]
CallsWrite = Annotated[AuthContext, Depends(require("conversations.write"))]


class OutboundIn(BaseModel):
    contact_id: uuid.UUID
    purpose: Literal["booking_confirmation", "reminder", "callback", "reschedule"]
    booking_id: uuid.UUID | None = Field(
        default=None, description="Defaults to the client's next upcoming booking (not needed for callback)."
    )
    admin_task_id: uuid.UUID | None = Field(
        default=None, description="Optional: the callback admin task this call answers."
    )


class SimulateCallIn(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    name: str = Field(default="", max_length=200)
    utterances: list[Annotated[str, Field(max_length=1000)]] = Field(min_length=1, max_length=30)
    language: Literal["ru", "kk"] | None = None


def _mask(ctx: AuthContext) -> bool:
    return ctx.role == "analyst"


@router.get("", response_model=CallList)
def list_calls(
    *,
    direction: Literal["in", "out"] | None = None,
    purpose: str | None = Query(default=None, description="Comma-separated purposes."),
    status: str | None = Query(default=None, description="Comma-separated statuses."),
    contact_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    ctx: CallsRead,
    db: DB,
) -> dict[str, Any]:
    stmt = select(Call).where(Call.tenant_id == ctx.tenant_id)
    if direction:
        stmt = stmt.where(Call.direction == direction)
    if purpose:
        stmt = stmt.where(Call.purpose.in_([p.strip() for p in purpose.split(",") if p.strip()]))
    if status:
        stmt = stmt.where(Call.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if contact_id:
        stmt = stmt.where(Call.contact_id == contact_id)
    start, end = voice_calls.local_range(db, ctx.tenant_id, date_from, date_to)
    if start is not None:
        stmt = stmt.where(Call.created_at >= start)
    if end is not None:
        stmt = stmt.where(Call.created_at < end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Call.created_at.desc(), Call.id).limit(limit).offset(offset)).all()
    return {"items": voice_calls.call_items(db, list(rows), mask=_mask(ctx)), "total": total}


@router.post(
    "/outbound",
    status_code=201,
    response_model=CallOut,
    responses=error_responses(
        404,
        409,
        422,
        e409="CONSENT_REQUIRED (no outbound_calls consent / opted out) | OUTSIDE_HOURS | DAILY_CAP_REACHED",
        e422="BOOKING_REQUIRED | BOOKING_CONTACT_MISMATCH | NO_PHONE | validation error",
    )
    | {503: {"description": "VOICE_NOT_CONFIGURED"}},
)
def outbound(body: OutboundIn, ctx: CallsWrite, db: DB) -> dict[str, Any]:
    """Outbound AI call to a client (ТЗ §13.3). A provider failure is returned as a call with status=failed."""
    call = voice_calls.place_outbound(
        db,
        ctx.tenant_id,
        contact_id=body.contact_id,
        purpose=body.purpose,
        booking_id=body.booking_id,
        admin_task_id=body.admin_task_id,
        user_id=ctx.user.id,
    )
    db.commit()
    return voice_calls.call_items(db, [call], mask=_mask(ctx))[0]


@router.post("/simulate", response_model=CallDetail, responses=error_responses(404, 422))
def simulate(body: SimulateCallIn, ctx: CallsWrite, db: DB) -> dict[str, Any]:
    """Dev/demo only (voice mock mode): a full inbound AI call from `phone` saying `utterances` in turn,
    through the same turn logic as the Twilio webhooks. An empty string simulates silence."""
    call = voice_calls.simulate_call(
        db,
        ctx.tenant_id,
        phone=body.phone,
        name=body.name,
        utterances=body.utterances,
        language=body.language,
    )
    return voice_calls.call_detail(db, call, mask=_mask(ctx))


@router.get("/{call_id}", response_model=CallDetail, responses=error_responses(404))
def get_call(call_id: uuid.UUID, ctx: CallsRead, db: DB) -> dict[str, Any]:
    call = get_or_404(db, Call, call_id, ctx.tenant_id)
    return voice_calls.call_detail(db, call, mask=_mask(ctx))
