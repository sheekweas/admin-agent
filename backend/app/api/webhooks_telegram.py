"""Telegram bot webhook (ТЗ §12): buttons Взять / Ответить / Подтвердить / Отклонить, text replies = answers.

URL: POST /v1/webhooks/telegram/{tenant_id}; authenticity via X-Telegram-Bot-Api-Secret-Token (per-tenant
secret set with setWebhook). Every action requires that the Telegram user is an active owner/admin of the
tenant (users.telegram_chat_id) with access to the task's branch. Always answers 200 so Telegram does not
retry-storm; failures are logged.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import OkOut
from app.channels.telegram import TelegramClient, TelegramError, client_for_tenant
from app.db import get_db
from app.models import AdminTask, Membership, Notification, Tenant, User
from app.services import escalation
from app.services import notifications as nt
from app.services.audit import audit
from app.services.common import DomainError
from app.services.integrations import telegram_webhook_secret

router = APIRouter()
log = logging.getLogger("webhooks.telegram")


def admin_by_chat(db: Session, tenant_id: uuid.UUID, chat_id: Any) -> tuple[Membership, User] | None:
    row = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            Membership.active.is_(True),
            User.active.is_(True),
            Membership.role.in_(("owner", "admin")),
            User.telegram_chat_id == str(chat_id),
        )
    ).first()
    return (row[0], row[1]) if row else None


def _branch_ok(m: Membership, branch_id: uuid.UUID | None) -> bool:
    scope = [str(b) for b in (m.branch_scope or [])]
    return not scope or branch_id is None or str(branch_id) in scope


def _safe(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TelegramError as exc:
        log.info("telegram reply failed: %s", exc)
        return None


def _parse_uuid(v: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(v)
    except ValueError:
        return None


@router.post(
    "/{tenant_id}",
    response_model=OkOut,
    responses={401: {"description": "Bad X-Telegram-Bot-Api-Secret-Token"}},
)
def telegram_webhook(
    tenant_id: uuid.UUID,
    update: dict = Body(...),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, telegram_webhook_secret(tenant_id)
    ):
        raise HTTPException(401, "Bad secret token")
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(404, "Unknown tenant")
    client = client_for_tenant(db, tenant_id)
    try:
        if "callback_query" in update:
            _on_callback(db, client, tenant_id, update["callback_query"])
        elif "message" in update:
            _on_message(db, client, tenant_id, update["message"])
    except Exception:
        db.rollback()
        log.exception("telegram update failed tenant=%s update_id=%s", tenant_id, update.get("update_id"))
    return {"ok": True}


def _on_callback(db: Session, client: TelegramClient, tenant_id: uuid.UUID, cq: dict[str, Any]) -> None:
    cq_id = str(cq.get("id", ""))
    chat_id = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    parts = str(cq.get("data") or "").split(":")
    member = admin_by_chat(db, tenant_id, chat_id)
    if member is None:
        audit(
            db,
            tenant_id,
            "telegram",
            str(chat_id),
            "access.denied",
            "telegram",
            str(chat_id),
            {"data": cq.get("data")},
        )
        db.commit()
        _safe(client.answer_callback_query, cq_id, "Нет доступа", alert=True)
        return
    m, user = member
    if len(parts) != 3 or (obj_id := _parse_uuid(parts[2])) is None:
        _safe(client.answer_callback_query, cq_id, "Неизвестная команда")
        return
    scope, action = parts[0], parts[1]
    if scope == "task" and action in ("take", "answer"):
        task = db.scalar(select(AdminTask).where(AdminTask.id == obj_id, AdminTask.tenant_id == tenant_id))
        if task is None or not _branch_ok(m, task.branch_id):
            _safe(client.answer_callback_query, cq_id, "Задача не найдена", alert=True)
            return
        task = escalation.ack_task(db, tenant_id, task.id, user.id, "telegram")
        db.commit()
        if task.assignee_user_id and task.assignee_user_id != user.id:
            other = db.get(User, task.assignee_user_id)
            _safe(client.answer_callback_query, cq_id, f"Уже взята: {other.full_name if other else '—'}")
        else:
            _safe(client.answer_callback_query, cq_id, f"Задача №{task.number} ваша")
        if action == "answer" and task.status != "resolved":
            _send_answer_prompt(db, client, tenant_id, task, chat_id)
    elif scope == "shift" and action in ("approve", "reject"):
        from app.services.schedule import decide_shift_request

        try:
            decide_shift_request(
                db, tenant_id, obj_id, approve=action == "approve", comment="via Telegram", actor_id=user.id
            )
            db.commit()
        except DomainError as exc:
            db.rollback()
            _safe(
                client.answer_callback_query,
                cq_id,
                str(exc.detail.get("message", "Ошибка"))[:200],
                alert=True,
            )
            return
        verdict = "Подтверждено" if action == "approve" else "Отклонено"
        _safe(client.answer_callback_query, cq_id, verdict)
        if msg.get("message_id"):
            _safe(
                client.edit_message_text,
                chat_id,
                msg["message_id"],
                f"{msg.get('text', '')}\n\n{verdict}: {user.full_name or user.email}",
            )
    else:
        _safe(client.answer_callback_query, cq_id, "Неизвестная команда")


def _send_answer_prompt(
    db: Session, client: TelegramClient, tenant_id: uuid.UUID, task: AdminTask, chat_id: Any
) -> None:
    lang = nt.lang_of(db.get(Tenant, tenant_id).default_language)
    text = nt.render("answer_prompt", lang, number=task.number)
    try:
        mid = client.send_message(chat_id, text, force_reply=True)
    except TelegramError as exc:
        log.info("answer prompt failed: %s", exc)
        return
    db.add(
        Notification(
            tenant_id=tenant_id,
            channel="telegram",
            kind="answer_prompt",
            recipient=str(chat_id),
            payload={"text": text, "task_id": str(task.id)},
            status="sent",
            attempts=1,
            provider_ref=str(mid),
        )
    )
    db.commit()


def _on_message(db: Session, client: TelegramClient, tenant_id: uuid.UUID, msg: dict[str, Any]) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    text = str(msg.get("text") or "").strip()
    if text.startswith("/start"):
        _safe(
            client.send_message,
            chat_id,
            f"Ваш Telegram chat id: {chat_id}\n"
            "Укажите его в профиле сотрудника в CRM, чтобы получать уведомления.",
        )
        return
    reply = msg.get("reply_to_message") or {}
    if not text or not reply.get("message_id"):
        return
    n = db.scalar(
        select(Notification).where(
            Notification.tenant_id == tenant_id,
            Notification.channel == "telegram",
            Notification.recipient == str(chat_id),
            Notification.provider_ref == str(reply["message_id"]),
        )
    )
    task_id = _parse_uuid(str((n.payload or {}).get("task_id"))) if n is not None else None
    if task_id is None:
        return
    member = admin_by_chat(db, tenant_id, chat_id)
    task = db.scalar(select(AdminTask).where(AdminTask.id == task_id, AdminTask.tenant_id == tenant_id))
    if member is None or task is None or not _branch_ok(member[0], task.branch_id):
        audit(db, tenant_id, "telegram", str(chat_id), "access.denied", "admin_task", str(task_id), {})
        db.commit()
        _safe(client.send_message, chat_id, "Нет доступа")
        return
    try:
        task = escalation.resolve_task(
            db,
            tenant_id,
            task.id,
            member[1].id,
            text,
            relay_to_client=task.conversation_id is not None,
            add_to_knowledge=False,
        )
        db.commit()
    except DomainError as exc:
        db.rollback()
        _safe(
            client.send_message,
            chat_id,
            str(exc.detail.get("message", "Ошибка")),
            reply_to_message_id=msg.get("message_id"),
        )
        return
    done = "Ответ отправлен клиенту" if task.conversation_id else "Задача закрыта"
    _safe(
        client.send_message, chat_id, f"{done} (№{task.number}).", reply_to_message_id=msg.get("message_id")
    )
