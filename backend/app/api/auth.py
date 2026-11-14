from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AuthContext, get_auth
from app.models import Membership, Tenant, User
from app.security import create_access_token, verify_password
from app.services.audit import audit
from app.services.common import utcnow

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
    audit(
        db,
        chosen.tenant_id if chosen else None,
        "user",
        str(user.id),
        "auth.login",
        "user",
        str(user.id),
        ip=request.client.host if request.client else None,
    )
    db.commit()
    token = create_access_token(
        str(user.id), str(chosen.tenant_id) if chosen else None, chosen.role if chosen else None
    )
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
