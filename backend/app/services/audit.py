from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(
    db: Session,
    tenant_id: uuid.UUID | None,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity: str,
    entity_id: str | None,
    payload: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Append an audit record in the caller's transaction. Never put secrets in payload."""
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            payload=payload or {},
            ip=ip,
        )
    )
