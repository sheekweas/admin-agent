"""Channel-neutral message model (ТЗ §6.2) and the adapter protocol every messenger implements."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.services.common import utcnow


@dataclass
class NormalizedMessage:
    """One inbound/outbound message as seen by the core, independent of the provider payload."""

    tenant_id: uuid.UUID
    channel: str  # whatsapp|telegram|voice|...
    external_user_id: str  # WhatsApp wa_id, Telegram chat id ...
    provider_message_id: str | None  # provider id (wamid...) - the dedupe key
    direction: str = "in"  # in|out
    message_type: str = "text"  # text|audio|image|document|interactive|location|unsupported
    text: str = ""
    media_url: str | None = None  # provider media reference ("wa-media:<id>") or URL
    media_mime: str | None = None
    language: str | None = None
    contact_name: str | None = None
    phone: str | None = None
    branch_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    provider_status: str = "received"
    received_at: datetime = field(default_factory=utcnow)
    processed_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return f"{self.channel}:{self.provider_message_id or self.message_id}"


@dataclass(frozen=True)
class StatusUpdate:
    """Delivery receipt for a previously sent outbound message."""

    provider_message_id: str
    status: str  # sent|delivered|read|failed
    recipient_id: str | None = None
    timestamp: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    status: str = "sent"  # sent|failed
    error: str | None = None


class ChannelAdapter(Protocol):
    channel: str

    def send_text(self, to: str, text: str) -> SendResult: ...

    def send_template(self, to: str, template: str, language: str, params: list[str]) -> SendResult: ...

    def download_media(self, media_ref: str) -> tuple[bytes, str]: ...
