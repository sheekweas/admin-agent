"""Branches (ТЗ §2.1: organisation → branch). Branch-scoped users only see their branches."""

from __future__ import annotations

import uuid
from typing import Annotated
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, require
from app.models import Branch
from app.services.audit import audit
from app.services.common import DomainError, get_or_404

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


class BranchOut(BaseModel):
    id: uuid.UUID
    name: str
    address: str
    timezone: str
    contacts: dict
    active: bool


def _check_tz(value: str | None) -> str | None:
    if value is not None and value not in available_timezones():
        raise ValueError("unknown time zone")
    return value


TimeZoneName = Annotated[str, AfterValidator(_check_tz)]


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    timezone: TimeZoneName = "Asia/Almaty"
    contacts: dict = {}
    active: bool = True


class BranchPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    timezone: TimeZoneName | None = None
    contacts: dict | None = None
    active: bool | None = None


class BranchList(BaseModel):
    items: list[BranchOut]
    total: int


def _out(b: Branch) -> BranchOut:
    return BranchOut(
        id=b.id,
        name=b.name,
        address=b.address,
        timezone=b.timezone,
        contacts=b.contacts or {},
        active=b.active,
    )


def _scoped(ctx: AuthContext, branch_id: uuid.UUID) -> None:
    if not ctx.branch_allowed(branch_id):
        raise DomainError(404, "NOT_FOUND", "Branch not found")


@router.get("", response_model=BranchList)
def list_branches(
    ctx: Annotated[AuthContext, Depends(require("services.read"))],
    db: DbSession,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = select(Branch).where(Branch.tenant_id == ctx.tenant_id)
    if active is not None:
        stmt = stmt.where(Branch.active.is_(active))
    if ctx.branch_scope:
        stmt = stmt.where(Branch.id.in_([uuid.UUID(b) for b in ctx.branch_scope]))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Branch.name).limit(limit).offset(offset)).all()
    return BranchList(items=[_out(b) for b in rows], total=total or 0)


@router.post("", response_model=BranchOut, status_code=201)
def create_branch(
    body: BranchIn, ctx: Annotated[AuthContext, Depends(require("settings.write"))], db: DbSession
):
    b = Branch(tenant_id=ctx.tenant_id, **body.model_dump())
    db.add(b)
    db.flush()
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "branch.create",
        "branch",
        str(b.id),
        body.model_dump(),
        ip=ctx.ip,
    )
    db.commit()
    return _out(b)


@router.patch("/{branch_id}", response_model=BranchOut)
def patch_branch(
    branch_id: uuid.UUID,
    body: BranchPatch,
    ctx: Annotated[AuthContext, Depends(require("settings.write"))],
    db: DbSession,
):
    b = get_or_404(db, Branch, branch_id, ctx.tenant_id)
    _scoped(ctx, b.id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in changes.items():
        setattr(b, k, v)
    audit(
        db, ctx.tenant_id, "user", str(ctx.user.id), "branch.update", "branch", str(b.id), changes, ip=ctx.ip
    )
    db.commit()
    return _out(b)
