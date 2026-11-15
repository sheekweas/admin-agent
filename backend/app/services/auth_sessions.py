"""Server-side auth sessions (v1.1.0): every JWT carries ``sid``; a token is accepted only while its session row
is active. Revocation = setting ``revoked_at``; expired/revoked rows are purged by the worker after 30 days."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, delete, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.config import get_settings
from app.models import AuthSession
from app.security import create_access_token
from app.services.common import utcnow

LAST_SEEN_THROTTLE = timedelta(minutes=5)
PURGE_AFTER = timedelta(days=30)
USER_AGENT_MAX = 300


def create_session(
    db: Session,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str | None,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> tuple[str, AuthSession]:
    """Create a session row (flushed, not committed) and mint a JWT bound to it."""
    now = now or utcnow()
    s = AuthSession(
        id=uuid.uuid4(),
        user_id=user_id,
        tenant_id=tenant_id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=get_settings().jwt_ttl_minutes),
        ip=(ip or None) and ip[:64],
        user_agent=(user_agent or None) and user_agent[:USER_AGENT_MAX],
    )
    db.add(s)
    db.flush()
    token = create_access_token(
        str(user_id), str(tenant_id) if tenant_id else None, role, sid=str(s.id), now=now
    )
    return token, s


def active_filter(now: datetime) -> ColumnElement[bool]:
    return (AuthSession.revoked_at.is_(None)) & (AuthSession.expires_at > now)


def check_session(
    db: Session, sid: str | None, user_id: uuid.UUID, now: datetime | None = None
) -> AuthSession:
    """Return the active session for ``sid`` or raise ``LookupError``. Touches ``last_seen_at`` at most once
    per LAST_SEEN_THROTTLE (single-row UPDATE by primary key, committed immediately)."""
    if not sid:
        raise LookupError("token has no session")
    try:
        sid_uuid = uuid.UUID(str(sid))
    except ValueError as exc:
        raise LookupError("bad session id") from exc
    now = now or utcnow()
    s = db.get(AuthSession, sid_uuid)
    if s is None or s.user_id != user_id or s.revoked_at is not None or s.expires_at <= now:
        raise LookupError("session inactive")
    if s.last_seen_at is None or now - s.last_seen_at >= LAST_SEEN_THROTTLE:
        db.execute(
            update(AuthSession)
            .where(AuthSession.id == s.id)
            .values(last_seen_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        set_committed_value(s, "last_seen_at", now)  # no reload, and not marked dirty
    return s


def revoke(db: Session, *conds: ColumnElement[bool], now: datetime | None = None) -> int:
    """Revoke the ACTIVE sessions matching ``conds``; returns how many were revoked (caller commits)."""
    now = now or utcnow()
    res = db.execute(
        update(AuthSession)
        .where(active_filter(now), *conds)
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)


def revoke_user_sessions(
    db: Session,
    user_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    except_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> int:
    conds: list[ColumnElement[bool]] = [AuthSession.user_id == user_id]
    if tenant_id is not None:
        conds.append(AuthSession.tenant_id == tenant_id)
    if except_id is not None:
        conds.append(AuthSession.id != except_id)
    return revoke(db, *conds, now=now)


def list_active(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> list[AuthSession]:
    now = now or utcnow()
    return list(
        db.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user_id, active_filter(now))
            .order_by(AuthSession.last_seen_at.desc(), AuthSession.created_at.desc())
        )
    )


def purge_sessions(db: Session, now: datetime | None = None) -> int:
    """Delete sessions that expired or were revoked more than PURGE_AFTER ago (worker beat)."""
    cutoff = (now or utcnow()) - PURGE_AFTER
    res = db.execute(
        delete(AuthSession)
        .where(or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff))
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)
