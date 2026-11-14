"""Outbound webhooks (Zapier etc., ТЗ §16).

Each (subscription, outbox event) pair becomes a ``Notification(channel="webhook")`` row — the delivery log
/ DLQ. Requests are signed:

    X-CRM-Timestamp: <unix seconds>
    X-CRM-Signature: sha256=<hex HMAC-SHA256(secret, f"{timestamp}." + raw_body)>
    X-CRM-Event: booking.confirmed        X-CRM-Delivery: <notification id> (idempotency key for the receiver)

Receivers should reject timestamps older than 5 minutes (replay protection). Failures are retried with
exponential backoff up to settings.webhook_max_attempts, then the row is ``dead`` (DLQ) and can be retried
manually. Personal data is stripped unless the subscription has ``include_pii``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Notification, OutboxEvent, WebhookSubscription
from app.security import decrypt_secret
from app.services.common import EVENT_TYPES, utcnow

# ТЗ §16.2 + the remaining public events
WEBHOOK_EVENTS = sorted(EVENT_TYPES)
PII_KEYS = {
    "phone",
    "phone_e164",
    "contact_phone",
    "client_phone",
    "email",
    "name",
    "contact_name",
    "client_name",
    "full_name",
    "question",
    "answer",
    "text",
    "comment",
    "reason",
    "notes",
    "address",
    "transcript",
}


class WebhookError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def strip_pii(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_pii(v) for k, v in value.items() if k not in PII_KEYS}
    if isinstance(value, list):
        return [strip_pii(v) for v in value]
    return value


def validate_target_url(url: str) -> None:
    """https only (http allowed in dev); no private/loopback hosts outside dev (SSRF)."""
    p = urlparse(url)
    dev = get_settings().env == "dev"
    if p.scheme not in (("https", "http") if dev else ("https",)) or not p.hostname:
        raise ValueError("URL must be https://")
    if dev:
        return
    host = p.hostname
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("private host is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("private address is not allowed")


def subscription_secret(sub: WebhookSubscription) -> str:
    try:
        return decrypt_secret(sub.secret)
    except Exception:  # noqa: BLE001 - legacy plaintext secret
        return sub.secret


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return (
        "sha256="
        + hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    )


def build_body(event: OutboxEvent, include_pii: bool) -> dict[str, Any]:
    data = event.payload or {}
    return {
        "id": str(event.id),
        "type": event.event_type,
        "tenant_id": str(event.tenant_id),
        "created_at": (event.created_at or utcnow()).isoformat(),
        "data": data if include_pii else strip_pii(data),
    }


def enqueue_for_event(db: Session, event: OutboxEvent) -> list[Notification]:
    """Fan an outbox event out to matching enabled subscriptions (idempotent per subscription+event)."""
    from app.services.notifications import _insert_notifications

    subs = db.scalars(
        select(WebhookSubscription).where(
            WebhookSubscription.tenant_id == event.tenant_id, WebhookSubscription.enabled.is_(True)
        )
    ).all()
    now = utcnow()
    rows = [
        {
            "tenant_id": event.tenant_id,
            "event_id": event.id,
            "channel": "webhook",
            "kind": event.event_type,
            "recipient": sub.url[:200],
            "dedupe_key": f"wh:{sub.id}:{event.id}",
            "payload": {"subscription_id": str(sub.id), "body": build_body(event, sub.include_pii)},
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": now,
        }
        for sub in subs
        if event.event_type in (sub.events or []) or "*" in (sub.events or [])
    ]
    return _insert_notifications(db, rows) if rows else []


def post_signed(
    url: str,
    secret: str,
    body: dict[str, Any],
    *,
    event_type: str,
    delivery_id: str,
    now: datetime | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode()
    ts = int((now or utcnow()).timestamp())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ai-crm-webhooks/1.0",
        "X-CRM-Event": event_type,
        "X-CRM-Delivery": delivery_id,
        "X-CRM-Timestamp": str(ts),
        "X-CRM-Signature": sign(secret, ts, raw),
    }
    return httpx.post(url, content=raw, headers=headers, timeout=timeout, follow_redirects=False)


def deliver(db: Session, n: Notification, now: datetime) -> str:
    sub_id = (n.payload or {}).get("subscription_id")
    sub = db.get(WebhookSubscription, uuid.UUID(sub_id)) if sub_id else None
    if sub is None or sub.tenant_id != n.tenant_id or not sub.enabled:
        from app.services.notifications import SkipDelivery

        raise SkipDelivery("subscription removed or disabled")
    try:
        r = post_signed(
            sub.url,
            subscription_secret(sub),
            n.payload.get("body", {}),
            event_type=n.kind,
            delivery_id=str(n.id),
            now=now,
        )
    except httpx.HTTPError as exc:
        raise WebhookError(f"network error: {exc.__class__.__name__}") from exc
    if 200 <= r.status_code < 300:
        return f"HTTP {r.status_code}"
    if r.status_code == 410:  # receiver says "gone" (Zapier unsubscribed)
        raise WebhookError("HTTP 410 Gone", retryable=False)
    raise WebhookError(f"HTTP {r.status_code}: {r.text[:200]}")
