"""Integrations (ТЗ §16, §20): per-tenant channel config, outbound webhook subscriptions, delivery log / DLQ.

Secrets are write-only: they are encrypted at rest and only ever returned masked ("••••abcd").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import AuthContext, require
from app.api.schemas import (
    DeliveryEventList,
    DeliveryEventOut,
    IntegrationList,
    IntegrationOut,
    IntegrationTestOut,
    WebhookSubscriptionCreated,
    WebhookSubscriptionList,
    WebhookSubscriptionOut,
    error_responses,
)
from app.models import Integration, Notification, OutboxEvent, WebhookSubscription
from app.security import encrypt_secret, random_token
from app.services import webhooks_out
from app.services.audit import audit
from app.services.common import DomainError, get_or_404, mask_phone, utcnow
from app.services.integrations import (
    INTEGRATION_KINDS,
    get_integration,
    public_config,
    read_secrets,
    telegram_webhook_secret,
    write_secrets,
)
from app.services.notifications import retry_notification
from app.services.outbox import retry_event

router = APIRouter()
MASK_PREFIX = "••••"


class IntegrationIn(BaseModel):
    config: dict[str, Any] | None = None
    secrets: dict[str, str | None] | None = None  # omitted key = keep, "" / null = delete
    enabled: bool | None = None


class SubscriptionIn(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    events: list[str] = Field(min_length=1)
    include_pii: bool = False
    enabled: bool = True


class SubscriptionPatch(BaseModel):
    events: list[str] | None = None
    include_pii: bool | None = None
    enabled: bool | None = None


def _kind(kind: str) -> str:
    if kind not in INTEGRATION_KINDS:
        raise DomainError(404, "NOT_FOUND", f"Unknown integration kind {kind}")
    return kind


def _external_id(kind: str, config: dict[str, Any], secrets: dict[str, str]) -> str:
    if kind == "whatsapp":
        return str(config.get("phone_number_id") or "")
    if kind == "telegram":
        return secrets.get("bot_token", "").split(":")[0]
    if kind == "voice":
        return str(config.get("from_number") or secrets.get("from_number") or "")
    return ""


def _telegram_webhook(db: Session, tenant_id: uuid.UUID, integ: Integration) -> None:
    from app.channels.telegram import TelegramClient, TelegramError

    token = read_secrets(integ).get("bot_token")
    if not token or not integ.enabled:
        return
    url = f"{get_settings().public_base_url.rstrip('/')}/v1/webhooks/telegram/{tenant_id}"
    try:
        TelegramClient(token).set_webhook(url, telegram_webhook_secret(tenant_id))
        integ.status, integ.last_error = "ok", None
    except TelegramError as exc:
        integ.status, integ.last_error = "error", f"setWebhook: {exc}"[:500]


@router.get("", response_model=IntegrationList)
def list_integrations(
    ctx: AuthContext = Depends(require("integrations.read")), db: Session = Depends(get_db)
):
    return {"items": [public_config(get_integration(db, ctx.tenant_id, k), k) for k in INTEGRATION_KINDS]}


@router.put(
    "/{kind}",
    response_model=IntegrationOut,
    responses=error_responses(
        404, 409, e409="INTEGRATION_IN_USE: the WhatsApp number belongs to another organization"
    ),
)
def put_integration(
    kind: str,
    body: IntegrationIn,
    ctx: AuthContext = Depends(require("integrations.write")),
    db: Session = Depends(get_db),
):
    kind = _kind(kind)
    integ = get_integration(db, ctx.tenant_id, kind)
    if integ is None:
        integ = Integration(tenant_id=ctx.tenant_id, kind=kind, config={}, enabled=True, status="unknown")
        db.add(integ)
    if body.config is not None:
        integ.config = dict(body.config)
    changed: list[str] = []
    if body.secrets is not None:
        secrets = read_secrets(integ)
        for k, v in body.secrets.items():
            if v and v.startswith(MASK_PREFIX):
                continue  # masked echo from the UI -> unchanged
            if v:
                secrets[k] = v
            else:
                secrets.pop(k, None)
            changed.append(k)
        write_secrets(integ, secrets)
    if body.enabled is not None:
        integ.enabled = body.enabled
    integ.external_id = _external_id(kind, integ.config or {}, read_secrets(integ))
    if kind == "whatsapp" and integ.external_id:
        clash = db.scalar(
            select(Integration.id).where(
                Integration.kind == "whatsapp",
                Integration.external_id == integ.external_id,
                Integration.tenant_id != ctx.tenant_id,
            )
        )
        if clash:
            raise DomainError(
                409, "INTEGRATION_IN_USE", "This WhatsApp number is connected to another organization"
            )
    db.flush()
    if kind == "telegram" and "bot_token" in changed and get_settings().telegram_mode != "mock":
        _telegram_webhook(db, ctx.tenant_id, integ)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "integration.update",
        "integration",
        str(integ.id),
        {
            "kind": kind,
            "config_keys": sorted((integ.config or {}).keys()),
            "secrets_changed": sorted(changed),
            "enabled": integ.enabled,
        },
        ip=ctx.ip,
    )
    db.commit()
    return public_config(integ, kind)


@router.post("/{kind}/test", response_model=IntegrationTestOut, responses=error_responses(404))
def test_integration(
    kind: str, ctx: AuthContext = Depends(require("integrations.write")), db: Session = Depends(get_db)
):
    kind = _kind(kind)
    integ = get_integration(db, ctx.tenant_id, kind)
    ok, detail = True, "ok"
    try:
        if kind == "telegram":
            from app.channels.telegram import TelegramClient

            client = TelegramClient(read_secrets(integ).get("bot_token"))
            me = client.get_me()
            detail = f"bot @{me.get('username')}"
            if ctx.user.telegram_chat_id:
                client.send_message(ctx.user.telegram_chat_id, "Тест: уведомления CRM работают.")
                detail += ", test message sent"
            if integ is not None and get_settings().telegram_mode != "mock":
                _telegram_webhook(db, ctx.tenant_id, integ)
        elif kind == "voice":
            from app.channels.voice import TwilioVoice, get_voice_adapter

            adapter = get_voice_adapter(db, ctx.tenant_id)
            if adapter is None:
                raise RuntimeError("voice provider is not configured")
            if isinstance(adapter, TwilioVoice):
                adapter.check_credentials()
            detail = f"provider {adapter.provider}"
        elif kind == "whatsapp":
            secrets, config = read_secrets(integ), (integ.config if integ else {}) or {}
            if not (secrets.get("access_token") and config.get("phone_number_id")):
                raise RuntimeError("access_token and phone_number_id are required")
            if get_settings().whatsapp_mode != "mock":
                r = httpx.get(
                    f"{get_settings().whatsapp_api_base}/{config['phone_number_id']}",
                    headers={"Authorization": f"Bearer {secrets['access_token']}"},
                    timeout=10,
                )
                if r.status_code != 200:
                    raise RuntimeError(f"Graph API HTTP {r.status_code}")
        else:
            subs = db.scalars(
                select(WebhookSubscription).where(
                    WebhookSubscription.tenant_id == ctx.tenant_id, WebhookSubscription.enabled.is_(True)
                )
            ).all()
            results = []
            for sub in subs:
                try:
                    r = webhooks_out.post_signed(
                        sub.url,
                        webhooks_out.subscription_secret(sub),
                        {"type": "ping", "tenant_id": str(ctx.tenant_id)},
                        event_type="ping",
                        delivery_id=str(uuid.uuid4()),
                    )
                    results.append(r.status_code < 300)
                except httpx.HTTPError:
                    results.append(False)
            ok = bool(results) and all(results)
            detail = f"{sum(results)}/{len(results)} subscriptions answered 2xx"
    except Exception as exc:  # noqa: BLE001 - report provider errors to the UI, never the secrets
        ok, detail = False, str(exc)[:300]
    if integ is not None:
        integ.status, integ.last_error = ("ok", None) if ok else ("error", detail)
    audit(db, ctx.tenant_id, "user", str(ctx.user.id), "integration.test", "integration", kind, {"ok": ok})
    db.commit()
    return {"ok": ok, "detail": detail}


# ---------------------------------------------------------------- webhook subscriptions (Zapier)
def _sub_out(sub: WebhookSubscription, secret: str | None = None) -> dict[str, Any]:
    out = {
        "id": sub.id,
        "url": sub.url,
        "events": sub.events,
        "include_pii": sub.include_pii,
        "enabled": sub.enabled,
        "created_at": sub.created_at,
    }
    if secret is not None:
        out["secret"] = secret  # shown exactly once, at creation
    return out


def _check_events(events: list[str]) -> list[str]:
    bad = [e for e in events if e != "*" and e not in webhooks_out.WEBHOOK_EVENTS]
    if bad:
        raise DomainError(
            422, "UNKNOWN_EVENTS", f"Unknown events: {bad}", allowed=webhooks_out.WEBHOOK_EVENTS
        )
    return sorted(set(events))


@router.get("/webhooks", response_model=WebhookSubscriptionList)
def list_webhooks(ctx: AuthContext = Depends(require("integrations.read")), db: Session = Depends(get_db)):
    subs = db.scalars(
        select(WebhookSubscription)
        .where(WebhookSubscription.tenant_id == ctx.tenant_id)
        .order_by(WebhookSubscription.created_at)
    ).all()
    return {
        "items": [_sub_out(s) for s in subs],
        "total": len(subs),
        "available_events": webhooks_out.WEBHOOK_EVENTS,
    }


@router.post(
    "/webhooks", status_code=201, response_model=WebhookSubscriptionCreated, responses=error_responses(422)
)
def create_webhook(
    body: SubscriptionIn,
    ctx: AuthContext = Depends(require("integrations.write")),
    db: Session = Depends(get_db),
):
    try:
        webhooks_out.validate_target_url(body.url)
    except ValueError as exc:
        raise DomainError(422, "BAD_WEBHOOK_URL", str(exc)) from exc
    secret = random_token(24)
    sub = WebhookSubscription(
        tenant_id=ctx.tenant_id,
        url=body.url,
        events=_check_events(body.events),
        secret=encrypt_secret(secret),
        include_pii=body.include_pii,
        enabled=body.enabled,
    )
    db.add(sub)
    db.flush()
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "webhook.create",
        "webhook_subscription",
        str(sub.id),
        {"host": urlparse(body.url).hostname, "events": sub.events, "include_pii": body.include_pii},
        ip=ctx.ip,
    )
    db.commit()
    return _sub_out(sub, secret)


@router.patch(
    "/webhooks/{sub_id}", response_model=WebhookSubscriptionOut, responses=error_responses(404, 422)
)
def patch_webhook(
    sub_id: uuid.UUID,
    body: SubscriptionPatch,
    ctx: AuthContext = Depends(require("integrations.write")),
    db: Session = Depends(get_db),
):
    sub = get_or_404(db, WebhookSubscription, sub_id, ctx.tenant_id)
    if body.events is not None:
        sub.events = _check_events(body.events)
    if body.include_pii is not None:
        sub.include_pii = body.include_pii
    if body.enabled is not None:
        sub.enabled = body.enabled
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "webhook.update",
        "webhook_subscription",
        str(sub.id),
        body.model_dump(exclude_none=True),
        ip=ctx.ip,
    )
    db.commit()
    return _sub_out(sub)


@router.delete("/webhooks/{sub_id}", status_code=204, responses=error_responses(404))
def delete_webhook(
    sub_id: uuid.UUID,
    ctx: AuthContext = Depends(require("integrations.write")),
    db: Session = Depends(get_db),
):
    sub = get_or_404(db, WebhookSubscription, sub_id, ctx.tenant_id)
    db.delete(sub)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "webhook.delete",
        "webhook_subscription",
        str(sub_id),
        {},
        ip=ctx.ip,
    )
    db.commit()


# ---------------------------------------------------------------- delivery log / DLQ
def _mask_recipient(n: Notification) -> str:
    if n.channel == "webhook":
        p = urlparse(n.recipient)
        return f"{p.scheme}://{p.hostname}/…"
    if n.recipient.startswith("+"):
        return mask_phone(n.recipient)
    return n.recipient


def _notification_out(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id,
        "source": "notification",
        "channel": n.channel,
        "kind": n.kind,
        "recipient": _mask_recipient(n),
        "status": n.status,
        "attempts": n.attempts,
        "next_attempt_at": n.next_attempt_at,
        "scheduled_for": n.scheduled_for,
        "last_error": n.last_error,
        "provider_ref": n.provider_ref,
        "event_id": n.event_id,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


def _outbox_out(e: OutboxEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "source": "outbox",
        "channel": "outbox",
        "kind": e.event_type,
        "status": e.status,
        "attempts": e.attempts,
        "next_attempt_at": e.next_attempt_at,
        "last_error": e.last_error,
        "aggregate_type": e.aggregate_type,
        "aggregate_id": e.aggregate_id,
        "created_at": e.created_at,
        "processed_at": e.processed_at,
    }


@router.get("/events", response_model=DeliveryEventList)
def list_events(
    status: str | None = None,
    channel: str | None = None,
    source: str = "notification",
    date_from: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(require("integrations.read")),
    db: Session = Depends(get_db),
):
    if source == "outbox":
        q = select(OutboxEvent).where(OutboxEvent.tenant_id == ctx.tenant_id)
        if status:
            q = q.where(OutboxEvent.status.in_(status.split(",")))
        if date_from:
            q = q.where(OutboxEvent.created_at >= date_from)
        total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
        rows = db.scalars(q.order_by(OutboxEvent.created_at.desc()).limit(limit).offset(offset)).all()
        return {"items": [_outbox_out(e) for e in rows], "total": total}
    q = select(Notification).where(Notification.tenant_id == ctx.tenant_id)
    if status:
        q = q.where(Notification.status.in_(status.split(",")))
    if channel:
        q = q.where(Notification.channel == channel)
    if date_from:
        q = q.where(Notification.created_at >= date_from)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Notification.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [_notification_out(n) for n in rows], "total": total}


@router.post(
    "/events/{event_id}/retry",
    response_model=DeliveryEventOut,
    responses=error_responses(404, 409, e409="ALREADY_SENT"),
)
def retry(
    event_id: uuid.UUID,
    ctx: AuthContext = Depends(require("integrations.read")),
    db: Session = Depends(get_db),
):
    """Manual redelivery of a dead/failed notification (or a failed outbox event)."""
    n = db.scalar(
        select(Notification).where(Notification.id == event_id, Notification.tenant_id == ctx.tenant_id)
    )
    if n is not None:
        if n.status == "sent":
            raise DomainError(409, "ALREADY_SENT", "Notification was already delivered")
        retry_notification(db, ctx.tenant_id, n.id, utcnow())
        out = _notification_out(n)
    else:
        e = retry_event(db, ctx.tenant_id, event_id)
        out = _outbox_out(e)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "integration.retry",
        out["source"],
        str(event_id),
        {},
        ip=ctx.ip,
    )
    db.commit()
    return out
