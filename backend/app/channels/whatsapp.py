"""Meta WhatsApp Cloud API adapter: webhook parsing, signature check, sending, media download.

Per-tenant credentials live in Integration(kind="whatsapp", external_id=<phone_number_id>):
  config  = {"template_name": "...", "template_language": "ru", "display_phone": "..."}   (non-secret)
  secrets = {"access_token": "..."}  (encrypted with app.security.encrypt_secret, JSON-encoded)

Mock mode (settings.whatsapp_mode == "mock") never touches the network: sends are recorded in
memory (`MOCK_OUTBOX`) and logged, and get fake ids.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import NormalizedMessage, SendResult, StatusUpdate
from app.config import get_settings
from app.models import Integration
from app.security import decrypt_secret, verify_meta_signature

log = logging.getLogger("channels.whatsapp")

CHANNEL = "whatsapp"
SERVICE_WINDOW = timedelta(hours=24)
MEDIA_PREFIX = "wa-media:"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

MOCK_OUTBOX: list[dict[str, Any]] = []
_mock_lock = threading.Lock()


class WhatsAppNotConfigured(RuntimeError):
    pass


# ---------------------------------------------------------------- webhook parsing
@dataclass
class InboundWA:
    """Provider-level inbound message, before tenant resolution."""

    provider_message_id: str
    wa_id: str
    message_type: str
    text: str
    contact_name: str | None
    timestamp: datetime
    media_id: str | None = None
    media_mime: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_normalized(self, tenant_id: uuid.UUID, branch_id: uuid.UUID | None = None) -> NormalizedMessage:
        return NormalizedMessage(
            tenant_id=tenant_id,
            channel=CHANNEL,
            external_user_id=self.wa_id,
            provider_message_id=self.provider_message_id,
            message_type=self.message_type,
            text=self.text,
            media_url=f"{MEDIA_PREFIX}{self.media_id}" if self.media_id else None,
            media_mime=self.media_mime,
            contact_name=self.contact_name,
            phone=self.wa_id,
            branch_id=branch_id,
            received_at=self.timestamp,
            meta=self.meta,
        )


@dataclass
class WebhookEntry:
    phone_number_id: str
    messages: list[InboundWA] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)


def verify_signature(body: bytes, header: str | None) -> bool:
    return verify_meta_signature(get_settings().whatsapp_app_secret, body, header)


def _ts(raw: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _parse_message(m: dict[str, Any], names: dict[str, str]) -> InboundWA | None:
    mid, wa_id, mtype = m.get("id"), m.get("from"), m.get("type", "unsupported")
    if not mid or not wa_id:
        return None
    text, media_id, mime, meta = "", None, None, {}
    if mtype == "text":
        text = (m.get("text") or {}).get("body", "")
    elif mtype in ("audio", "image", "document", "video", "sticker"):
        media = m.get(mtype) or {}
        media_id, mime = media.get("id"), media.get("mime_type")
        text = media.get("caption", "") or ""
        if mtype == "audio":
            meta["voice"] = bool(media.get("voice", True))
        if mtype == "document" and media.get("filename"):
            meta["filename"] = media["filename"]
        if mtype in ("video", "sticker"):
            meta["original_type"] = mtype
            mtype = "image" if mtype == "sticker" else "document"
    elif mtype == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        text = reply.get("title", "")
        meta["reply_id"] = reply.get("id")
    elif mtype == "button":  # quick-reply button of a template
        btn = m.get("button") or {}
        text, meta["reply_id"], mtype = btn.get("text", ""), btn.get("payload"), "interactive"
    elif mtype == "location":
        loc = m.get("location") or {}
        meta["location"] = {k: loc.get(k) for k in ("latitude", "longitude", "name", "address")}
        text = loc.get("address") or loc.get("name") or ""
    else:
        meta["original_type"] = mtype
        mtype = "unsupported"
    if m.get("context", {}).get("id"):
        meta["reply_to"] = m["context"]["id"]
    return InboundWA(
        provider_message_id=mid,
        wa_id=str(wa_id),
        message_type=mtype,
        text=text,
        contact_name=names.get(str(wa_id)),
        timestamp=_ts(m.get("timestamp")),
        media_id=media_id,
        media_mime=mime,
        meta=meta,
    )


def _parse_status(s: dict[str, Any]) -> StatusUpdate | None:
    if not s.get("id") or not s.get("status"):
        return None
    err = None
    if s.get("errors"):
        e = s["errors"][0]
        err = f"{e.get('code', '')} {e.get('title') or e.get('message') or ''}".strip()
    return StatusUpdate(
        provider_message_id=s["id"],
        status=str(s["status"]),
        recipient_id=s.get("recipient_id"),
        timestamp=_ts(s.get("timestamp")),
        error=err,
    )


def parse_webhook(payload: dict[str, Any]) -> list[WebhookEntry]:
    """Flatten a Cloud API webhook into per-phone-number batches of messages and statuses."""
    out: dict[str, WebhookEntry] = {}
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            pnid = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if not pnid:
                continue
            batch = out.setdefault(pnid, WebhookEntry(phone_number_id=pnid))
            names = {
                str(c.get("wa_id")): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []
            }
            for m in value.get("messages") or []:
                if parsed := _parse_message(m, names):
                    batch.messages.append(parsed)
            for s in value.get("statuses") or []:
                if st := _parse_status(s):
                    batch.statuses.append(st)
    return list(out.values())


# ---------------------------------------------------------------- service window
def within_service_window(last_inbound_at: datetime | None, now: datetime | None = None) -> bool:
    """Free-form messages are allowed only within 24h after the customer's last message."""
    if last_inbound_at is None:
        return False
    now = now or datetime.now(UTC)
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
    return now - last_inbound_at < SERVICE_WINDOW


# ---------------------------------------------------------------- integration lookup
def find_integration_by_phone_number_id(db: Session, phone_number_id: str) -> Integration | None:
    return db.scalar(
        select(Integration)
        .where(
            Integration.kind == CHANNEL,
            Integration.external_id == phone_number_id,
            Integration.enabled.is_(True),
        )
        .limit(1)
    )


def _secrets(integration: Integration) -> dict[str, Any]:
    raw = decrypt_secret(integration.secrets_enc or "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"access_token": raw}
    return data if isinstance(data, dict) else {"access_token": str(data)}


# ---------------------------------------------------------------- adapter
class WhatsAppAdapter:
    channel = CHANNEL

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        mock: bool,
        template_name: str | None = None,
        template_language: str = "ru",
    ):
        self.phone_number_id = phone_number_id
        self._token = access_token
        self.mock = mock
        self.template_name = template_name
        self.template_language = template_language

    @classmethod
    def for_tenant(cls, db: Session, tenant_id: uuid.UUID) -> WhatsAppAdapter:
        settings = get_settings()
        integ = db.scalar(
            select(Integration)
            .where(
                Integration.tenant_id == tenant_id, Integration.kind == CHANNEL, Integration.enabled.is_(True)
            )
            .order_by(Integration.created_at)
            .limit(1)
        )
        config = (integ.config if integ else None) or {}
        mock = settings.whatsapp_mode == "mock"
        if not mock and (integ is None or not integ.external_id):
            raise WhatsAppNotConfigured("WhatsApp integration is not configured for this organization")
        token = "" if mock or integ is None else str(_secrets(integ).get("access_token") or "")
        if not mock and not token:
            raise WhatsAppNotConfigured("WhatsApp access token is missing")
        return cls(
            phone_number_id=integ.external_id if integ else "mock",
            access_token=token,
            mock=mock,
            template_name=config.get("template_name") or None,
            template_language=config.get("template_language") or "ru",
        )

    # -- sending
    def _post(self, payload: dict[str, Any]) -> SendResult:
        if self.mock:
            fake_id = f"wamid.mock.{uuid.uuid4().hex}"
            with _mock_lock:
                MOCK_OUTBOX.append({"id": fake_id, "phone_number_id": self.phone_number_id, **payload})
            log.info(
                "mock whatsapp send to=%s type=%s id=%s", payload.get("to"), payload.get("type"), fake_id
            )
            return SendResult(ok=True, provider_message_id=fake_id, status="sent")
        url = f"{get_settings().whatsapp_api_base}/{self.phone_number_id}/messages"
        try:
            resp = httpx.post(
                url, json=payload, timeout=_HTTP_TIMEOUT, headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            return SendResult(ok=False, status="failed", error=f"network: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                msg = f"{err.get('code', resp.status_code)} {err.get('message', '')}".strip()
            except ValueError:
                msg = f"HTTP {resp.status_code}"
            return SendResult(ok=False, status="failed", error=msg[:500])
        msgs = resp.json().get("messages") or [{}]
        return SendResult(ok=True, provider_message_id=msgs[0].get("id"), status="sent")

    def send_text(self, to: str, text: str) -> SendResult:
        return self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": text[:4096]},
            }
        )

    def send_template(self, to: str, template: str, language: str, params: list[str]) -> SendResult:
        components = [{"type": "body", "parameters": [{"type": "text", "text": p[:1024]} for p in params]}]
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": language},
                    "components": components if params else [],
                },
            }
        )

    def download_media(self, media_ref: str) -> tuple[bytes, str]:
        """Resolve a media id via Graph API and download the binary (bytes, mime)."""
        media_id = media_ref.removeprefix(MEDIA_PREFIX)
        if self.mock:
            return b"", "audio/ogg"
        headers = {"Authorization": f"Bearer {self._token}"}
        meta = httpx.get(
            f"{get_settings().whatsapp_api_base}/{media_id}", headers=headers, timeout=_HTTP_TIMEOUT
        )
        meta.raise_for_status()
        info = meta.json()
        data = httpx.get(info["url"], headers=headers, timeout=_HTTP_TIMEOUT)
        data.raise_for_status()
        return data.content, info.get("mime_type") or data.headers.get(
            "content-type", "application/octet-stream"
        )


def mock_outbox() -> list[dict[str, Any]]:
    with _mock_lock:
        return list(MOCK_OUTBOX)


def clear_mock_outbox() -> None:
    with _mock_lock:
        MOCK_OUTBOX.clear()
