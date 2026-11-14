"""Client messaging core (owner: AI/WhatsApp module): inbound persistence, AI turns, outbound delivery,
human takeover. Functions never commit; the webhook/API/worker owns the transaction.

Flow: webhook -> handle_inbound (fast, idempotent) -> commit -> enqueue_ai_reply -> worker ->
process_ai_turn -> orchestrator.run_turn -> send_client_message -> channel adapter.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels.base import NormalizedMessage, SendResult, StatusUpdate
from app.config import get_settings
from app.models import Contact, ContactChannel, Conversation, Message, Tenant
from app.services import crm
from app.services.audit import audit
from app.services.common import DomainError, emit_event, get_or_404, normalize_phone, utcnow
from app.services.tenant_settings import merged_settings

log = logging.getLogger("messaging")

# AT-13: explicit requests to stop messaging (RU / KK / EN). Matched on the normalised lower-case text.
_OPT_OUT_PATTERNS = [
    r"\bне\s+(?:пишите|пиши|присылайте|присылай|отправляйте|беспокойте|звоните)\b",
    r"\bотпиш(?:ите|и)\b",
    r"\bотписа(?:ться|ть)\b",
    r"\bхочу\s+отписаться\b",
    r"\bудалите\s+(?:мой\s+)?номер\b",
    r"\bбольше\s+не\s+(?:пишите|присылайте)\b",
    r"\bжазбаңыз(?:дар)?\b",
    r"\bмаған\s+жазба\b",
    r"\bхабарлама\s+жібермеңіз\b",
    r"\bжібермеңіз\b",
    r"\bмазаламаңыз\b",
    r"\bжазылымнан\s+шығ",
    r"\bunsubscribe\b",
    r"\bopt[\s-]?out\b",
    r"^\s*(?:stop|стоп|тоқта)\s*[.!]*\s*$",
]
_OPT_OUT_RE = re.compile("|".join(_OPT_OUT_PATTERNS), re.IGNORECASE | re.UNICODE)
_OPT_OUT_REPLY = {
    "ru": "Хорошо, мы больше не будем присылать вам рассылки. Если понадобится запись — просто напишите нам.",
    "kk": "Жарайды, енді сізге жарнамалық хабарламалар жібермейміз. Жазылу қажет болса — бізге жазыңыз.",
}
_STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}


class DeliveryFailed(DomainError):
    def __init__(self, message: Message):
        super().__init__(
            502, "DELIVERY_FAILED", message.error or "Delivery failed", message_id=str(message.id)
        )


def detect_opt_out(text: str | None) -> bool:
    return bool(text) and bool(_OPT_OUT_RE.search(text.lower().replace("ё", "е")))


# ---------------------------------------------------------------- inbound
def _open_conversation(
    db: Session,
    tenant_id: uuid.UUID,
    contact: Contact,
    channel: str,
    branch_id: uuid.UUID | None,
    default_lang: str,
) -> Conversation:
    conv = db.scalars(
        select(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.contact_id == contact.id,
            Conversation.channel == channel,
            Conversation.status == "open",
        )
        .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
        .limit(1)
    ).first()
    if conv is None:
        conv = Conversation(
            tenant_id=tenant_id,
            contact_id=contact.id,
            channel=channel,
            mode="ai",
            status="open",
            branch_id=branch_id or contact.branch_id,
            language=contact.language or default_lang,
            state={},
        )
        db.add(conv)
        db.flush()
    return conv


def handle_inbound(db: Session, nm: NormalizedMessage) -> Message | None:
    """Persist one inbound message idempotently (AT-02). Returns the new Message, or None for a duplicate.

    Creates/links contact, conversation and an open lead; records an opt-out request (AT-13).
    The caller commits and then calls `enqueue_ai_reply(conversation_id, message_id)`.
    """
    if nm.provider_message_id and db.scalar(
        select(Message.id).where(
            Message.tenant_id == nm.tenant_id, Message.provider_message_id == nm.provider_message_id
        )
    ):
        return None
    tenant = db.get(Tenant, nm.tenant_id)
    if tenant is None:
        raise DomainError(404, "NOT_FOUND", "Tenant not found")
    try:
        with db.begin_nested():
            contact = crm.get_or_create_contact_by_channel(
                db,
                nm.tenant_id,
                channel=nm.channel,
                external_id=nm.external_user_id,
                phone=normalize_phone(nm.phone) if nm.phone else None,
                name=nm.contact_name,
                language=None,
            )
            conv = _open_conversation(
                db, nm.tenant_id, contact, nm.channel, nm.branch_id, tenant.default_language
            )
            crm.open_or_get_lead(db, nm.tenant_id, contact.id, source=nm.channel)
            meta = dict(nm.meta)
            opt_out = detect_opt_out(nm.text)
            if opt_out:
                meta["opt_out"] = True
                crm.set_opt_out(db, nm.tenant_id, contact.id, True, actor_type="client", actor_id=None)
            msg = Message(
                tenant_id=nm.tenant_id,
                conversation_id=conv.id,
                provider_message_id=nm.provider_message_id,
                direction="in",
                author="client",
                message_type=nm.message_type,
                text=nm.text or "",
                media_url=nm.media_url,
                language=nm.language,
                provider_status="received",
                received_at=nm.received_at,
                meta={**meta, **({"mime": nm.media_mime} if nm.media_mime else {})},
            )
            db.add(msg)
            now = utcnow()
            conv.last_message_at = max(nm.received_at, conv.last_message_at or nm.received_at)
            conv.unread_count = (conv.unread_count or 0) + 1
            contact.last_contact_at = now
            db.flush()
    except IntegrityError:
        # A concurrent delivery of the same webhook won the race on uq_message_provider_id.
        log.info("duplicate inbound message %s ignored", nm.provider_message_id)
        return None
    nm.message_id, nm.conversation_id = msg.id, conv.id
    return msg


def apply_status_update(db: Session, tenant_id: uuid.UUID, upd: StatusUpdate) -> Message | None:
    """Delivery receipts: statuses only move forward; `failed` emits message.delivery_failed once."""
    msg = db.scalar(
        select(Message).where(
            Message.tenant_id == tenant_id, Message.provider_message_id == upd.provider_message_id
        )
    )
    if msg is None:
        return None
    if upd.status == "failed":
        if msg.provider_status != "failed":
            _mark_failed(db, msg, upd.error or "delivery failed")
    elif _STATUS_RANK.get(upd.status, -1) > _STATUS_RANK.get(msg.provider_status, -1):
        msg.provider_status = upd.status
    return msg


def _mark_failed(db: Session, msg: Message, error: str) -> None:
    msg.provider_status = "failed"
    msg.error = error[:2000]
    conv = db.get(Conversation, msg.conversation_id)
    emit_event(
        db,
        msg.tenant_id,
        "message.delivery_failed",
        "message",
        msg.id,
        {
            "message_id": str(msg.id),
            "conversation_id": str(msg.conversation_id),
            "contact_id": str(conv.contact_id) if conv else None,
            "error": msg.error,
        },
    )


# ---------------------------------------------------------------- queueing
def enqueue_ai_reply(conversation_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Hand the AI turn to Celery; run inline in tests / mock mode or when the broker is unavailable.
    Must be called AFTER the inbound message was committed."""
    s = get_settings()
    if s.env != "test" and s.whatsapp_mode != "mock":
        try:
            from app.workers.tasks import process_inbound_message

            process_inbound_message.delay(str(conversation_id), str(message_id))
            return
        except Exception:
            log.warning("celery unavailable, processing AI turn inline", exc_info=True)
    run_ai_turn_now(conversation_id, message_id)


def run_ai_turn_now(conversation_id: uuid.UUID | str, message_id: uuid.UUID | str) -> None:
    """Run one AI turn in its own session/transaction (inline path and worker helper)."""
    from app.db import SessionLocal

    conv_id, msg_id = uuid.UUID(str(conversation_id)), uuid.UUID(str(message_id))
    with SessionLocal() as db:
        tenant_id = db.scalar(select(Conversation.tenant_id).where(Conversation.id == conv_id))
        if tenant_id is None:
            return
        try:
            process_ai_turn(db, tenant_id, conv_id, msg_id)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("AI turn failed conversation=%s message=%s", conv_id, msg_id)


# ---------------------------------------------------------------- AI turn
def _lang(conv: Conversation) -> str:
    return conv.language if conv.language in ("ru", "kk") else "ru"


def _transcribe(db: Session, conv: Conversation, msg: Message) -> None:
    from app.ai.stt import get_stt
    from app.channels.whatsapp import WhatsAppAdapter

    audio, mime = b"", (msg.meta or {}).get("mime") or "audio/ogg"
    if msg.media_url and conv.channel == "whatsapp":
        audio, mime = WhatsAppAdapter.for_tenant(db, conv.tenant_id).download_media(msg.media_url)
    stt = get_stt()
    tr = stt.transcribe(audio, mime, conv.language)
    msg.text = tr.text
    msg.meta = {**(msg.meta or {}), "transcribed": True, "stt": stt.name}
    if tr.language in ("ru", "kk"):
        msg.language = tr.language


def _fallback(db: Session, conv: Conversation, msg: Message, reason: str) -> Message | None:
    """AT-10: AI unavailable -> pre-approved technical reply once, conversation handed to a human."""
    tenant = db.get(Tenant, conv.tenant_id)
    ai_cfg = merged_settings(tenant).get("ai", {}) if tenant else {}
    text = ai_cfg.get(f"fallback_message_{_lang(conv)}") or ai_cfg.get("fallback_message_ru") or ""
    reply = None
    if conv.mode == "ai" and text:
        reply = send_client_message(db, conv.tenant_id, conv.id, text, author="system")
    try:
        from app.services import escalation

        escalation.create_admin_task(
            db,
            conv.tenant_id,
            question=f"AI не смог ответить клиенту. Сообщение: {msg.text or '[' + msg.message_type + ']'}"[
                :2000
            ],
            conversation_id=conv.id,
            contact_id=conv.contact_id,
            kind="question",
            priority="high",
            payload={"source": "ai_fallback", "reason": reason[:500], "message_id": str(msg.id)},
        )
    except Exception:
        log.exception("could not create admin task for AI fallback")
    conv.mode = "waiting_admin"  # a human owns it now; AI stays silent until released
    return reply


def process_ai_turn(
    db: Session, tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, *, llm: Any = None
) -> Message | None:
    """Answer one inbound message with the AI agent. Returns the outbound reply (or None if AI stays silent).

    Idempotent per message (processed_at). Silent when the conversation is not in "ai" mode (AT-15) or AI
    is disabled for the tenant. Any AI failure -> fallback + admin task (AT-10); the inbound is never lost.
    """
    from app.ai import orchestrator

    conv = db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .with_for_update()
    )
    msg = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
        )
    )
    if conv is None or msg is None or msg.direction != "in" or msg.processed_at is not None:
        return None
    msg.processed_at = utcnow()

    # Several quick messages in a row: answer once, on the latest one (it sees the whole history).
    newer = db.scalar(
        select(func.count(Message.id)).where(
            Message.conversation_id == conv.id,
            Message.tenant_id == tenant_id,
            Message.direction == "in",
            Message.received_at > msg.received_at,
            Message.processed_at.is_(None),
        )
    )
    if newer:
        return None

    tenant = db.get(Tenant, tenant_id)
    ai_enabled = bool(merged_settings(tenant).get("ai", {}).get("enabled", True)) if tenant else False

    if (msg.meta or {}).get("opt_out"):
        if conv.mode == "ai" and ai_enabled:
            return send_client_message(db, tenant_id, conv.id, _OPT_OUT_REPLY[_lang(conv)], author="ai")
        return None

    if msg.message_type == "audio" and not msg.text:
        try:
            _transcribe(db, conv, msg)
        except Exception as exc:  # noqa: BLE001 - STT not configured / provider down
            log.warning("voice transcription failed: %s", exc)
            msg.meta = {**(msg.meta or {}), "stt_error": str(exc)[:300]}
            try:
                crm.add_contact_tags(db, tenant_id, conv.contact_id, ["голосовое сообщение"])
            except Exception:
                log.exception("tagging failed")
            if conv.mode == "ai" and ai_enabled:
                return _fallback(db, conv, msg, f"stt: {exc}")
            return None

    if conv.mode != "ai" or not ai_enabled:
        return None

    sp = db.begin_nested()
    try:
        outcome = orchestrator.run_turn(db, conv, msg, llm=llm)
        sp.commit()
    except Exception as exc:  # noqa: BLE001 - every AI failure degrades to the fallback path
        sp.rollback()
        log.warning("AI turn failed for conversation %s: %s", conv.id, exc)
        db.refresh(conv)
        db.refresh(msg)
        msg.processed_at = msg.processed_at or utcnow()
        orchestrator.record_failed_run(db, conv, msg, exc)
        return _fallback(db, conv, msg, str(exc))
    return send_client_message(db, tenant_id, conv.id, outcome.reply_text, author="ai")


# ---------------------------------------------------------------- outbound
def _recipient(db: Session, conv: Conversation) -> str | None:
    ext = db.scalar(
        select(ContactChannel.external_id).where(
            ContactChannel.tenant_id == conv.tenant_id,
            ContactChannel.contact_id == conv.contact_id,
            ContactChannel.channel == conv.channel,
        )
    )
    if ext:
        return ext
    phone = db.scalar(
        select(Contact.phone_e164).where(Contact.id == conv.contact_id, Contact.tenant_id == conv.tenant_id)
    )
    return phone.lstrip("+") if phone else None


def _deliver_whatsapp(db: Session, conv: Conversation, msg: Message, kind: str) -> SendResult:
    from app.channels.whatsapp import WhatsAppAdapter, WhatsAppNotConfigured, within_service_window

    contact = db.get(Contact, conv.contact_id)
    if kind == "marketing" and (contact is None or contact.opted_out):
        return SendResult(ok=False, status="failed", error="OPTED_OUT: client asked not to receive mailings")
    to = _recipient(db, conv)
    if not to:
        return SendResult(ok=False, status="failed", error="NO_RECIPIENT")
    try:
        adapter = WhatsAppAdapter.for_tenant(db, conv.tenant_id)
    except WhatsAppNotConfigured as exc:
        return SendResult(ok=False, status="failed", error=f"NOT_CONFIGURED: {exc}")
    last_in = db.scalar(
        select(func.max(Message.received_at)).where(
            Message.conversation_id == conv.id, Message.tenant_id == conv.tenant_id, Message.direction == "in"
        )
    )
    if within_service_window(last_in):
        return adapter.send_text(to, msg.text)
    if adapter.template_name:  # outside the 24h customer-service window only templates are allowed
        msg.meta = {**(msg.meta or {}), "template": adapter.template_name}
        return adapter.send_template(to, adapter.template_name, adapter.template_language, [msg.text])
    return SendResult(
        ok=False,
        status="failed",
        error="OUTSIDE_SERVICE_WINDOW: 24h window closed and no template configured",
    )


def send_client_message(
    db: Session,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    *,
    author: str = "ai",
    author_user_id: uuid.UUID | None = None,
    kind: str = "service",
    raise_on_failure: bool = False,
) -> Message:
    """Store an outbound message and deliver it through the conversation's channel.

    kind="service" (replies, confirmations, reminders) or "marketing" (blocked for opted-out clients, AT-13).
    Failures are recorded on the message (provider_status="failed", error) + message.delivery_failed event;
    with raise_on_failure=True a DeliveryFailed error is raised after recording (for retrying callers).
    """
    conv = get_or_404(db, Conversation, conversation_id, tenant_id)
    text = (text or "").strip()
    if not text:
        raise DomainError(422, "EMPTY_MESSAGE", "Message text is empty")
    msg = Message(
        tenant_id=tenant_id,
        conversation_id=conv.id,
        direction="out",
        author=author,
        author_user_id=author_user_id,
        message_type="text",
        text=text,
        language=conv.language,
        provider_status="queued",
        received_at=utcnow(),
        meta={},
    )
    db.add(msg)
    db.flush()
    conv.last_message_at = msg.received_at
    if conv.channel != "whatsapp":
        return msg
    if raise_on_failure:
        # Retrying callers (notification worker) deliver synchronously so they can reschedule on failure.
        _deliver_now(db, conv, msg, kind)
        if msg.provider_status == "failed":
            raise DeliveryFailed(msg)
        return msg
    # Default: never tell the client anything before the transaction that produced it is committed
    # (ТЗ §7.3 — "Вы записаны" only after a committed booking). Delivery runs right after COMMIT;
    # if the process dies in between, `deliver_stale_queued` (worker sweep) sends it later.
    msg.meta = {**(msg.meta or {}), "kind": kind}
    _after_commit(db, msg.id)
    return msg


def _deliver_now(db: Session, conv: Conversation, msg: Message, kind: str) -> None:
    res = _deliver_whatsapp(db, conv, msg, kind)
    if res.ok:
        msg.provider_message_id, msg.provider_status = res.provider_message_id, res.status or "sent"
    else:
        _mark_failed(db, msg, res.error or "delivery failed")
    db.flush()


def deliver_queued(db: Session, message_id: uuid.UUID) -> Message | None:
    """Deliver one committed outbound message that is still 'queued' (row-locked, so it is sent once)."""
    msg = db.scalar(
        select(Message)
        .where(Message.id == message_id, Message.direction == "out", Message.provider_status == "queued")
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if msg is None:
        return None
    conv = db.get(Conversation, msg.conversation_id)
    _deliver_now(db, conv, msg, (msg.meta or {}).get("kind", "service"))
    return msg


def deliver_stale_queued(db: Session, older_than_sec: int = 60, limit: int = 100) -> int:
    """Worker safety net: deliver outbound messages left 'queued' (e.g. crash between commit and send)."""
    cutoff = utcnow() - timedelta(seconds=older_than_sec)
    ids = db.scalars(
        select(Message.id)
        .where(Message.direction == "out", Message.provider_status == "queued", Message.received_at < cutoff)
        .order_by(Message.received_at)
        .limit(limit)
    ).all()
    sent = 0
    for mid in ids:
        if deliver_queued(db, mid) is not None:
            db.commit()
            sent += 1
    return sent


def _after_commit(db: Session, message_id: uuid.UUID) -> None:
    pending: list[uuid.UUID] = db.info.setdefault("deliver_after_commit", [])
    pending.append(message_id)


@event.listens_for(Session, "after_commit")
def _run_deliveries(session: Session) -> None:
    ids = session.info.pop("deliver_after_commit", None)
    if not ids:
        return
    from app.db import SessionLocal

    with SessionLocal() as s:
        for mid in ids:
            try:
                if deliver_queued(s, mid) is not None:
                    s.commit()
            except Exception:
                s.rollback()
                log.exception("post-commit delivery failed for message %s", mid)


@event.listens_for(Session, "after_rollback")
def _drop_deliveries(session: Session) -> None:
    # Outer transaction rolled back: the queued messages no longer exist, nothing to send.
    if not session.in_nested_transaction():
        session.info.pop("deliver_after_commit", None)


# ---------------------------------------------------------------- inbox actions (humans)
def takeover(
    db: Session, tenant_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    """AT-15: a human takes the dialogue; the AI stays silent until `release`."""
    conv = get_or_404(db, Conversation, conversation_id, tenant_id)
    if conv.mode != "human" or conv.owner_user_id != user_id:
        previous = conv.mode
        conv.mode, conv.owner_user_id = "human", user_id
        audit(
            db,
            tenant_id,
            "user",
            str(user_id),
            "conversation.takeover",
            "conversation",
            str(conv.id),
            {"from": previous},
        )
    return conv


def release(
    db: Session, tenant_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    conv = get_or_404(db, Conversation, conversation_id, tenant_id)
    if conv.mode != "ai":
        previous = conv.mode
        conv.mode = "ai"
        audit(
            db,
            tenant_id,
            "user",
            str(user_id),
            "conversation.release",
            "conversation",
            str(conv.id),
            {"from": previous},
        )
    return conv


def mark_read(db: Session, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    conv = get_or_404(db, Conversation, conversation_id, tenant_id)
    conv.unread_count = 0
    return conv


def human_reply(
    db: Session,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    *,
    user_id: uuid.UUID,
    internal_note: bool = False,
) -> Message:
    """Operator message from the inbox. A real reply switches the conversation to human mode automatically;
    an internal note is stored only (never delivered, never shown to the AI)."""
    conv = get_or_404(db, Conversation, conversation_id, tenant_id)
    text = (text or "").strip()
    if not text:
        raise DomainError(422, "EMPTY_MESSAGE", "Message text is empty")
    if internal_note:
        note = Message(
            tenant_id=tenant_id,
            conversation_id=conv.id,
            direction="out",
            author="human",
            author_user_id=user_id,
            message_type="note",
            text=text,
            is_internal_note=True,
            provider_status="internal",
            received_at=utcnow(),
            meta={},
        )
        db.add(note)
        db.flush()
        return note
    takeover(db, tenant_id, conv.id, user_id)
    conv.unread_count = 0
    msg = send_client_message(db, tenant_id, conv.id, text, author="human", author_user_id=user_id)
    audit(
        db,
        tenant_id,
        "user",
        str(user_id),
        "conversation.reply",
        "conversation",
        str(conv.id),
        {"message_id": str(msg.id), "status": msg.provider_status},
    )
    return msg


def message_to_dict(m: Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "direction": m.direction,
        "author": m.author,
        "message_type": m.message_type,
        "text": m.text,
        "media_url": m.media_url,
        "provider_status": m.provider_status,
        "is_internal_note": m.is_internal_note,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "author_user_id": str(m.author_user_id) if m.author_user_id else None,
        "error": m.error,
        "language": m.language,
    }
