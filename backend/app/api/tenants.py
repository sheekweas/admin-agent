"""Organization settings and team (users/memberships)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth, require
from app.models import Membership, Tenant, User
from app.security import hash_password
from app.services.audit import audit
from app.services.common import DomainError, get_or_404
from app.services.tenant_settings import DEFAULT_SETTINGS, merged_settings

router = APIRouter()


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    niche: str
    plan: str
    default_language: str
    currency: str
    settings: dict


class TenantPatch(BaseModel):
    name: str | None = None
    niche: str | None = None
    default_language: str | None = Field(default=None, pattern="^(ru|kk)$")
    settings: dict | None = None


@router.get("/tenant", response_model=TenantOut)
def get_tenant(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    t = db.get(Tenant, ctx.tenant_id)
    return TenantOut(
        id=t.id,
        name=t.name,
        niche=t.niche,
        plan=t.plan,
        default_language=t.default_language,
        currency=t.currency,
        settings=merged_settings(t),
    )


@router.patch("/tenant", response_model=TenantOut)
def patch_tenant(
    body: TenantPatch, ctx: AuthContext = Depends(require("settings.write")), db: Session = Depends(get_db)
):
    t = db.get(Tenant, ctx.tenant_id)
    changes = body.model_dump(exclude_none=True)
    if "settings" in changes:
        unknown = set(changes["settings"]) - set(DEFAULT_SETTINGS)
        if unknown:
            raise DomainError(422, "UNKNOWN_SETTINGS", f"Unknown settings: {sorted(unknown)}")
        t.settings = {**(t.settings or {}), **changes.pop("settings")}
    for k, v in changes.items():
        setattr(t, k, v)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "tenant.update",
        "tenant",
        str(t.id),
        body.model_dump(exclude_none=True),
        ip=ctx.ip,
    )
    db.commit()
    return get_tenant(ctx, db)


class MemberOut(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: str
    phone_e164: str | None
    telegram_chat_id: str | None
    role: str
    branch_scope: list
    is_escalation_primary: bool
    is_escalation_backup: bool
    active: bool


class MemberIn(BaseModel):
    email: str
    full_name: str = ""
    password: str = Field(min_length=8)
    phone_e164: str | None = None
    telegram_chat_id: str | None = None
    role: str = Field(pattern="^(owner|admin|staff|analyst)$")
    branch_scope: list[uuid.UUID] = []
    is_escalation_primary: bool = False
    is_escalation_backup: bool = False


class MemberPatch(BaseModel):
    full_name: str | None = None
    phone_e164: str | None = None
    telegram_chat_id: str | None = None
    role: str | None = Field(default=None, pattern="^(owner|admin|staff|analyst)$")
    branch_scope: list[uuid.UUID] | None = None
    is_escalation_primary: bool | None = None
    is_escalation_backup: bool | None = None
    active: bool | None = None


def _member_out(m: Membership) -> MemberOut:
    u = m.user
    return MemberOut(
        membership_id=m.id,
        user_id=u.id,
        email=u.email,
        full_name=u.full_name,
        phone_e164=u.phone_e164,
        telegram_chat_id=u.telegram_chat_id,
        role=m.role,
        branch_scope=m.branch_scope or [],
        is_escalation_primary=m.is_escalation_primary,
        is_escalation_backup=m.is_escalation_backup,
        active=m.active,
    )


@router.get("/team", response_model=list[MemberOut])
def list_team(ctx: AuthContext = Depends(require("staff.read")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Membership).where(Membership.tenant_id == ctx.tenant_id)).all()
    return [_member_out(m) for m in rows]


@router.post("/team", response_model=MemberOut, status_code=201)
def add_member(
    body: MemberIn, ctx: AuthContext = Depends(require("users.write")), db: Session = Depends(get_db)
):
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            full_name=body.full_name,
            password_hash=hash_password(body.password),
            phone_e164=body.phone_e164,
            telegram_chat_id=body.telegram_chat_id,
        )
        db.add(user)
        db.flush()
    elif db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.tenant_id == ctx.tenant_id)
    ):
        raise DomainError(409, "ALREADY_MEMBER", "User is already a member")
    m = Membership(
        tenant_id=ctx.tenant_id,
        user_id=user.id,
        role=body.role,
        branch_scope=[str(b) for b in body.branch_scope],
        is_escalation_primary=body.is_escalation_primary,
        is_escalation_backup=body.is_escalation_backup,
    )
    db.add(m)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "member.add",
        "user",
        str(user.id),
        {"role": body.role},
        ip=ctx.ip,
    )
    db.commit()
    db.refresh(m)
    return _member_out(m)


@router.patch("/team/{membership_id}", response_model=MemberOut)
def patch_member(
    membership_id: uuid.UUID,
    body: MemberPatch,
    ctx: AuthContext = Depends(require("users.write")),
    db: Session = Depends(get_db),
):
    m = get_or_404(db, Membership, membership_id, ctx.tenant_id)
    data = body.model_dump(exclude_none=True)
    for k in ("full_name", "phone_e164", "telegram_chat_id"):
        if k in data:
            setattr(m.user, k, data.pop(k))
    if "branch_scope" in data:
        data["branch_scope"] = [str(b) for b in data["branch_scope"]]
    for k, v in data.items():
        setattr(m, k, v)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "member.update",
        "membership",
        str(m.id),
        body.model_dump(exclude_none=True, mode="json"),
        ip=ctx.ip,
    )
    db.commit()
    return _member_out(m)
