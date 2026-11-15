"""Organization settings and team (users/memberships)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth, require
from app.models import Branch, Membership, Tenant, User
from app.security import hash_password, verify_password
from app.services.audit import audit
from app.services.auth_sessions import revoke_user_sessions
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


def _branch_scope(db: Session, ctx: AuthContext, ids: list[uuid.UUID]) -> list[str]:
    """Branch scopes may only name this organization's branches."""
    if ids and len(
        set(db.scalars(select(Branch.id).where(Branch.tenant_id == ctx.tenant_id, Branch.id.in_(ids))))
    ) != len(set(ids)):
        raise DomainError(422, "UNKNOWN_BRANCH", "Some branch ids do not exist")
    return [str(b) for b in ids]


def _shared_with_other_tenants(db: Session, user_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(Membership.id)
            .where(Membership.user_id == user_id, Membership.tenant_id != tenant_id)
            .limit(1)
        )
        is not None
    )


@router.get("/team", response_model=list[MemberOut])
def list_team(ctx: AuthContext = Depends(require("staff.read")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Membership).where(Membership.tenant_id == ctx.tenant_id)).all()
    out = [_member_out(m) for m in rows]
    if not ctx.can("tasks.write"):
        # Staff/analysts see who is on the team, but not colleagues' phones / Telegram ids (spec §20.1).
        for item in out:
            if item.user_id != ctx.user.id:
                item.phone_e164 = None
                item.telegram_chat_id = None
    return out


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
    elif not verify_password(body.password, user.password_hash):
        # An existing account (possibly of another organization) is only linked when the caller knows its
        # password: otherwise anyone could enrol a stranger's account and read/rewrite their profile.
        raise DomainError(
            409, "USER_EXISTS", "An account with this e-mail exists; enter that account's password to add it"
        )
    m = Membership(
        tenant_id=ctx.tenant_id,
        user_id=user.id,
        role=body.role,
        branch_scope=_branch_scope(db, ctx, body.branch_scope),
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
    profile = {
        k: data.pop(k)
        for k in ("full_name", "phone_e164", "telegram_chat_id")
        if k in data and data[k] != getattr(m.user, k)
    }
    if profile and m.user_id != ctx.user.id and _shared_with_other_tenants(db, m.user_id, ctx.tenant_id):
        # The user profile (phone, Telegram chat id) is global: another organization's owner must not be able
        # to redirect this person's escalation calls/messages.
        raise DomainError(
            409,
            "SHARED_USER_PROFILE",
            "This user also belongs to another organization; they edit it themselves",
        )
    for k, v in profile.items():
        setattr(m.user, k, v)
    for k in ("full_name", "phone_e164", "telegram_chat_id"):
        data.pop(k, None)
    if "branch_scope" in data:
        data["branch_scope"] = _branch_scope(db, ctx, data["branch_scope"])
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


class SignOutOut(BaseModel):
    revoked: int


@router.post("/team/{membership_id}/sign-out", response_model=SignOutOut)
def sign_out_member(
    membership_id: uuid.UUID,
    ctx: AuthContext = Depends(require("team.manage")),
    db: Session = Depends(get_db),
):
    """Revoke every active session of this member. A session is a login (the UI switches organizations with
    X-Tenant-Id on the same token), so all of the user's sessions end, not only those started in this tenant.
    The caller's own current session is kept (signing yourself out ends your other devices)."""
    m = get_or_404(db, Membership, membership_id, ctx.tenant_id)
    if m.role == "owner" and ctx.role != "owner":
        raise DomainError(403, "OWNER_PROTECTED", "Only an owner can sign out an owner")
    n = revoke_user_sessions(db, m.user_id, except_id=ctx.session_id)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "auth.user_signed_out",
        "membership",
        str(m.id),
        {"user_id": str(m.user_id), "revoked": n},
        ip=ctx.ip,
    )
    db.commit()
    return SignOutOut(revoked=n)
