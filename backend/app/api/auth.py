from __future__ import annotations

import uuid

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth
from app.models import AuthSession, Membership, Tenant, User
from app.security import hash_password, verify_password
from app.services import auth_sessions
from app.services.audit import audit
from app.services.common import DomainError, utcnow

router = APIRouter()


class LoginIn(BaseModel):
    email: str
    password: str
    tenant_id: uuid.UUID | None = None


class TenantOption(BaseModel):
    tenant_id: uuid.UUID
    name: str
    role: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: uuid.UUID | None
    role: str | None
    tenants: list[TenantOption]


_FAILED: dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    import time

    now = time.time()
    hits = [t for t in _FAILED.get(key, []) if now - t < 300]
    _FAILED[key] = hits
    return len(hits) >= 10


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else ''}:{body.email.lower()}"
    if _rate_limited(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try later")
    user = db.scalar(select(User).where(User.email == body.email.lower().strip()))
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        import time

        _FAILED.setdefault(key, []).append(time.time())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    rows = db.execute(
        select(Membership, Tenant)
        .join(Tenant, Tenant.id == Membership.tenant_id)
        .where(Membership.user_id == user.id, Membership.active.is_(True))
    ).all()
    options = [TenantOption(tenant_id=t.id, name=t.name, role=m.role) for m, t in rows]
    chosen = None
    if body.tenant_id:
        chosen = next((o for o in options if o.tenant_id == body.tenant_id), None)
        if not chosen:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this organization")
    elif options:
        chosen = options[0]

    user.last_login = utcnow()
    ip = request.client.host if request.client else None
    token, session = auth_sessions.create_session(
        db,
        user.id,
        chosen.tenant_id if chosen else None,
        chosen.role if chosen else None,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    audit(
        db,
        chosen.tenant_id if chosen else None,
        "user",
        str(user.id),
        "auth.login",
        "user",
        str(user.id),
        {"session_id": str(session.id)},
        ip=ip,
    )
    db.commit()
    return LoginOut(
        access_token=token,
        tenant_id=chosen.tenant_id if chosen else None,
        role=chosen.role if chosen else None,
        tenants=options,
    )


class MeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    ui_language: str
    tenant_id: uuid.UUID
    tenant_name: str
    role: str
    staff_id: uuid.UUID | None
    branch_scope: list[str]
    permissions: list[str]


@router.get("/me", response_model=MeOut)
def me(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    from app.deps import PERMISSIONS

    tenant = db.get(Tenant, ctx.tenant_id)
    return MeOut(
        user_id=ctx.user.id,
        email=ctx.user.email,
        full_name=ctx.user.full_name,
        ui_language=ctx.user.ui_language,
        tenant_id=ctx.tenant_id,
        tenant_name=tenant.name if tenant else "",
        role=ctx.role,
        staff_id=ctx.staff_id,
        branch_scope=ctx.branch_scope,
        permissions=sorted(p for p, roles in PERMISSIONS.items() if ctx.role in roles),
    )


# ---------------------------------------------------------------- sessions & password (v1.1.0)
def _audit(db: Session, ctx: AuthContext, action: str, entity: str, entity_id: str, payload: dict) -> None:
    audit(db, ctx.tenant_id, "user", str(ctx.user.id), action, entity, entity_id, payload, ip=ctx.ip)


@router.post("/logout", status_code=204)
def logout(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    auth_sessions.revoke(db, AuthSession.id == ctx.session_id)
    _audit(db, ctx, "auth.logout", "auth_session", str(ctx.session_id), {})
    db.commit()
    return Response(status_code=204)


class RevokedOut(BaseModel):
    revoked: int


@router.post("/logout-all", response_model=RevokedOut)
def logout_all(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    n = auth_sessions.revoke_user_sessions(db, ctx.user.id)
    _audit(db, ctx, "auth.logout_all", "user", str(ctx.user.id), {"revoked": n})
    db.commit()
    return RevokedOut(revoked=n)


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


class SessionList(BaseModel):
    items: list[SessionOut]
    total: int


@router.get("/sessions", response_model=SessionList)
def list_sessions(ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)):
    """Active sessions of the current user (all organizations: a session is a login, not a tenant)."""
    rows = auth_sessions.list_active(db, ctx.user.id)
    items = [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            ip=s.ip,
            user_agent=s.user_agent,
            current=s.id == ctx.session_id,
        )
        for s in rows
    ]
    return SessionList(items=items, total=len(items))


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: uuid.UUID, ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
):
    # Only the caller's own active sessions; anyone else's id is indistinguishable from a missing one.
    n = auth_sessions.revoke(db, AuthSession.id == session_id, AuthSession.user_id == ctx.user.id)
    if not n:
        raise DomainError(404, "NOT_FOUND", "Session not found")
    _audit(db, ctx, "auth.session_revoked", "auth_session", str(session_id), {})
    db.commit()
    return Response(status_code=204)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordOut(BaseModel):
    revoked_other_sessions: int


MIN_PASSWORD_LEN = 10


@router.post("/change-password", response_model=ChangePasswordOut)
def change_password(
    body: ChangePasswordIn, ctx: AuthContext = Depends(get_auth), db: Session = Depends(get_db)
):
    key = f"pwd:{ctx.user.id}"
    if _rate_limited(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, try later")
    if not verify_password(body.current_password, ctx.user.password_hash):
        import time

        _FAILED.setdefault(key, []).append(time.time())
        raise DomainError(400, "INVALID_CURRENT_PASSWORD", "Current password is incorrect")
    new = body.new_password
    if len(new) < MIN_PASSWORD_LEN or new.isdigit():
        raise DomainError(
            400,
            "WEAK_PASSWORD",
            f"Password must be at least {MIN_PASSWORD_LEN} characters and not only digits",
        )
    ctx.user.password_hash = hash_password(new)
    n = auth_sessions.revoke_user_sessions(db, ctx.user.id, except_id=ctx.session_id)
    _audit(db, ctx, "auth.password_changed", "user", str(ctx.user.id), {"revoked_other_sessions": n})
    db.commit()
    return ChangePasswordOut(revoked_other_sessions=n)
