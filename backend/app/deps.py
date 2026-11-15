"""Auth context & RBAC. tenant_id ALWAYS comes from the verified token, never from request body."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Membership, Staff, User
from app.security import decode_access_token
from app.services.auth_sessions import check_session
from app.services.audit import audit

# Permission matrix (ТЗ §2). Keys are permission names used by routers.
PERMISSIONS: dict[str, set[str]] = {
    "bookings.read": {"owner", "admin", "staff", "analyst"},
    "bookings.write": {"owner", "admin"},
    "bookings.write_own": {"owner", "admin", "staff"},
    "contacts.read": {"owner", "admin", "analyst"},
    "contacts.write": {"owner", "admin"},
    "contacts.export": {"owner"},
    "conversations.read": {"owner", "admin"},
    "conversations.write": {"owner", "admin"},
    "staff.read": {"owner", "admin", "staff", "analyst"},
    "staff.write": {"owner", "admin"},
    "shift_requests.create": {"owner", "admin", "staff"},
    "shift_requests.decide": {"owner", "admin"},
    "services.read": {"owner", "admin", "staff", "analyst"},
    "services.write": {"owner", "admin"},
    "finance.read": {"owner", "analyst"},
    "finance.write": {"owner", "admin"},
    "reports.read": {"owner", "analyst", "admin"},
    "reports.export": {"owner", "analyst"},
    "knowledge.read": {"owner", "admin"},
    "knowledge.write": {"owner", "admin"},
    "knowledge.publish": {"owner"},
    "tasks.read": {"owner", "admin"},
    "tasks.write": {"owner", "admin"},
    "integrations.read": {"owner", "admin"},
    "integrations.write": {"owner"},
    "settings.write": {"owner"},
    "users.write": {"owner"},
    "team.manage": {"owner", "admin"},  # sign a team member out of all sessions
    "audit.read": {"owner"},
}


@dataclass
class AuthContext:
    user: User
    tenant_id: uuid.UUID
    role: str
    branch_scope: list[str] = field(default_factory=list)
    staff_id: uuid.UUID | None = None
    ip: str | None = None
    session_id: uuid.UUID | None = None

    def can(self, perm: str) -> bool:
        return self.role in PERMISSIONS.get(perm, set())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {perm}")

    def branch_allowed(self, branch_id: uuid.UUID | str | None) -> bool:
        return not self.branch_scope or branch_id is None or str(branch_id) in self.branch_scope


def get_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_access_token(authorization.split(" ", 1)[1])
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    try:
        user_uuid = uuid.UUID(str(claims["sub"]))
        session = check_session(db, claims.get("sid"), user_uuid)
    except (ValueError, LookupError) as exc:
        # revoked / expired / unknown session, or a v1.0 token without "sid" -> the client must log in again
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from exc
    user = db.get(User, user_uuid)
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")

    tenant_id = x_tenant_id or claims.get("tid")
    if not tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tenant not selected")
    try:
        tenant_uuid = uuid.UUID(str(tenant_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad tenant id") from exc

    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.tenant_id == tenant_uuid, Membership.active.is_(True)
        )
    )
    ip = request.client.host if request.client else None
    if membership is None:
        # AT-11: cross-tenant access attempt is denied and logged.
        audit(db, tenant_uuid, "user", str(user.id), "access.denied", "tenant", str(tenant_uuid), {}, ip=ip)
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this organization")

    staff_id = None
    if membership.role == "staff":
        staff_id = db.scalar(select(Staff.id).where(Staff.user_id == user.id, Staff.tenant_id == tenant_uuid))
    return AuthContext(
        user=user,
        tenant_id=tenant_uuid,
        role=membership.role,
        branch_scope=[str(b) for b in (membership.branch_scope or [])],
        staff_id=staff_id,
        ip=ip,
        session_id=session.id,
    )


def require(perm: str):
    def _dep(ctx: AuthContext = Depends(get_auth)) -> AuthContext:
        ctx.require(perm)
        return ctx

    return _dep
