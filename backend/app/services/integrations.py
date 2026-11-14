"""Per-tenant integration config/secrets helpers (owner: ops agent).

Secrets of an ``Integration`` row are stored as ONE encrypted JSON object in ``secrets_enc``
(``app.security.encrypt_secret(json.dumps({...}))``). Known keys:
  telegram: bot_token          voice: account_sid, auth_token, from_number
  whatsapp: access_token, app_secret (phone_number_id lives in config + external_id)
Secrets are never returned by the API — only masked previews.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Integration
from app.security import decrypt_secret, encrypt_secret

INTEGRATION_KINDS = ("whatsapp", "telegram", "voice", "zapier")


def get_integration(
    db: Session, tenant_id: uuid.UUID, kind: str, *, enabled_only: bool = False
) -> Integration | None:
    q = select(Integration).where(Integration.tenant_id == tenant_id, Integration.kind == kind)
    if enabled_only:
        q = q.where(Integration.enabled.is_(True))
    return db.scalars(q.order_by(Integration.created_at)).first()


def read_secrets(integration: Integration | None) -> dict[str, str]:
    if integration is None or not integration.secrets_enc:
        return {}
    raw = decrypt_secret(integration.secrets_enc)
    try:
        data = json.loads(raw)
    except ValueError:
        return {"token": raw}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {"token": raw}


def write_secrets(integration: Integration, secrets: dict[str, str]) -> None:
    clean = {k: v for k, v in secrets.items() if v}
    integration.secrets_enc = encrypt_secret(json.dumps(clean)) if clean else ""


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def tenant_secrets(db: Session, tenant_id: uuid.UUID, kind: str) -> dict[str, str]:
    return read_secrets(get_integration(db, tenant_id, kind, enabled_only=True))


def telegram_webhook_secret(tenant_id: uuid.UUID | str) -> str:
    """Per-tenant secret for setWebhook(secret_token=...) / X-Telegram-Bot-Api-Secret-Token.
    Derived from the platform secret so a leaked tenant secret cannot forge other tenants' updates."""
    key = get_settings().telegram_webhook_secret.encode()
    return hmac.new(key, f"tg:{tenant_id}".encode(), hashlib.sha256).hexdigest()[:48]


def public_config(integration: Integration | None, kind: str) -> dict[str, Any]:
    secrets = read_secrets(integration) if integration else {}
    return {
        "id": integration.id if integration else None,
        "kind": kind,
        "enabled": integration.enabled if integration else False,
        "configured": bool(integration and (integration.secrets_enc or integration.config)),
        "status": integration.status if integration else "unknown",
        "last_error": integration.last_error if integration else None,
        "config": dict(integration.config or {}) if integration else {},
        "secrets_set": sorted(secrets),
        "secrets": {k: mask_secret(v) for k, v in secrets.items()},  # masked previews only
        "updated_at": integration.updated_at if integration else None,
    }
