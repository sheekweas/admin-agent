"""GET /v1/availability — free slots from the booking engine (ТЗ §8.1, §18)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.bookings import ensure_staff_scope
from app.db import get_db
from app.deps import AuthContext, require
from app.services import booking as booking_svc
from app.services.common import DomainError

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]

MAX_RANGE_DAYS = 62


class SlotOut(BaseModel):
    slot_id: str
    staff_id: uuid.UUID
    staff_name: str
    branch_id: uuid.UUID
    start_at: datetime
    end_at: datetime


class SlotList(BaseModel):
    items: list[SlotOut]
    total: int


@router.get("", response_model=SlotList)
def get_availability(
    ctx: Annotated[AuthContext, Depends(require("bookings.read"))],
    db: DbSession,
    service_id: uuid.UUID,
    date_from: date,
    date_to: date | None = None,
    staff_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    date_to = date_to or date_from
    if date_to < date_from or (date_to - date_from).days > MAX_RANGE_DAYS:
        raise DomainError(422, "BAD_RANGE", f"date_to must be within {MAX_RANGE_DAYS} days after date_from")
    if ctx.role == "staff":
        staff_id = staff_id or ctx.staff_id
        ensure_staff_scope(ctx, staff_id)
    if branch_id is not None and not ctx.branch_allowed(branch_id):
        raise DomainError(403, "FORBIDDEN", "No access to this branch")
    slots = booking_svc.get_available_slots(
        db,
        ctx.tenant_id,
        service_id=service_id,
        date_from=date_from,
        date_to=date_to,
        staff_id=staff_id,
        branch_id=branch_id,
        limit=limit,
    )
    items = [
        SlotOut(
            slot_id=s.slot_id,
            staff_id=s.staff_id,
            staff_name=s.staff_name,
            branch_id=s.branch_id,
            start_at=s.start_at,
            end_at=s.end_at,
        )
        for s in slots
        if ctx.branch_allowed(s.branch_id)
    ]
    return SlotList(items=items, total=len(items))
