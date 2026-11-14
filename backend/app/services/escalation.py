"""Admin tasks and the 5/10-minute escalation chain with explicit ACK (ТЗ §4.3, §12.2, §12.3, AT-04..06).

Timeline (per-tenant ``settings.escalation``; T0 = task.created_at):
  T0                    Telegram to primary admins with [Взять] [Ответить] [Открыть CRM] (via outbox)
  T0 + repeat_after_min repeat Telegram to primary + backup                      (level 1, AT-04)
  T0 + call_after_min   outbound call to primary, then backup every call_retry_min (level 2.., AT-05)
                        capped by max_call_attempts; skipped when calls are disabled, in quiet hours
                        (branch-local), for low priority or when voice is not configured
  ACK (button / CRM / IVR "1") stops timers and cancels queued/ringing calls          (AT-06)
  sla_due_at            client gets ONE honest "передано специалисту" message if still unresolved

Race safety: the worker locks each due task with SELECT ... FOR UPDATE SKIP LOCKED; ACK is a conditional
UPDATE ... WHERE status='open' (blocks on that lock, then re-checks), so a call placed concurrently with an
ACK is cancelled by the ACK and no further step can run after it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    AdminTask,
    Branch,
    Call,
    Contact,
    Conversation,
    KnowledgeArticle,
    Membership,
    Notification,
    Tenant,
    User,
)
from app.services import notifications as nt
from app.services.audit import audit
from app.services.common import DomainError, emit_event, get_or_404, mask_phone, next_number, tz, utcnow
from app.services.tenant_settings import merged_settings

log = logging.getLogger("escalation")

TASK_KINDS = ("question", "complaint", "schedule_conflict", "callback", "delivery_failed")
PRIORITIES = ("low", "normal", "high", "urgent")
ACTIVE_CALL_STATUSES = ("queued", "ringing")


# ====================================================================== helpers
def escalation_settings(db: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    s = merged_settings(db.get(Tenant, tenant_id))["escalation"]
    return {"call_retry_min": 3, **s}


def branch_zone(db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID | None) -> ZoneInfo:
    branch = db.get(Branch, branch_id) if branch_id else None
    if branch is None:
        branch = db.scalars(
            select(Branch).where(Branch.tenant_id == tenant_id).order_by(Branch.created_at)
        ).first()
    return tz(branch.timezone if branch else None)


def in_quiet_hours(local: datetime, quiet: list[int] | tuple[int, int] | None) -> bool:
    if not quiet or len(quiet) != 2 or quiet[0] == quiet[1]:
        return False
    start, end = int(quiet[0]), int(quiet[1])
    h = local.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def _tenant_lang(db: Session, tenant_id: uuid.UUID) -> str:
    return nt.lang_of(db.get(Tenant, tenant_id).default_language)


def task_text(db: Session, task: AdminTask, lang: str) -> str:
    contact = db.get(Contact, task.contact_id) if task.contact_id else None
    client = "—"
    if contact:
        client = (
            " ".join(x for x in (contact.name, nt.format_phone(contact.phone_e164)) if x and x != "—") or "—"
        )
    kind = task.kind if task.kind in ("question", "complaint", "schedule_conflict") else "task_generic"
    text = nt.render(kind, lang, number=task.number, client=client, question=task.question.strip()[:1500])
    link = nt.panel_url(f"/tasks/{task.id}")
    return text if link.startswith("https://") else f"{text}\n{link}"


def task_buttons(task: AdminTask, lang: str, *, taken: bool = False) -> list[list[dict[str, str]]]:
    row = [] if taken else [{"text": nt.label("take", lang), "callback_data": f"task:take:{task.id}"}]
    if task.conversation_id:
        row.append({"text": nt.label("answer", lang), "callback_data": f"task:answer:{task.id}"})
    rows = [row] if row else []
    crm = nt.url_button(nt.label("open_crm", lang), nt.panel_url(f"/tasks/{task.id}"))
    if crm:
        rows.append([crm])
    return rows


def _task_chat_ids(db: Session, task: AdminTask, which: tuple[str, ...]) -> list[str]:
    members: list[tuple[Membership, User]] = []
    for w in which:
        members += nt.escalation_members(db, task.tenant_id, task.branch_id, w)
    return nt.telegram_chat_ids(members)


# ====================================================================== create
def create_admin_task(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    question: str,
    conversation_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    kind: str = "question",
    priority: str = "normal",
    branch_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AdminTask:
    """Create a task and start the escalation clock. Telegram T0 goes out via outbox admin_task.created."""
    now = now or utcnow()
    if kind not in TASK_KINDS:
        raise DomainError(422, "BAD_TASK_KIND", f"kind must be one of {TASK_KINDS}")
    if priority not in PRIORITIES:
        raise DomainError(422, "BAD_PRIORITY", f"priority must be one of {PRIORITIES}")
    if not (question or "").strip():
        raise DomainError(422, "EMPTY_QUESTION", "Question is empty")
    conv = get_or_404(db, Conversation, conversation_id, tenant_id) if conversation_id else None
    if conv is not None:
        contact_id = contact_id or conv.contact_id
        branch_id = branch_id or conv.branch_id
    if contact_id:
        contact = get_or_404(db, Contact, contact_id, tenant_id)
        branch_id = branch_id or contact.branch_id
    if branch_id:
        get_or_404(db, Branch, branch_id, tenant_id)
    s = escalation_settings(db, tenant_id)
    task = AdminTask(
        tenant_id=tenant_id,
        number=next_number(db, tenant_id, "admin_task"),
        branch_id=branch_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        kind=kind,
        question=question.strip(),
        priority=priority,
        status="open",
        escalation_level=0,
        created_at=now,
        next_escalation_at=now + timedelta(minutes=float(s["repeat_after_min"])),
        sla_due_at=now + timedelta(minutes=float(s["sla_min"]))
        if conversation_id and s.get("sla_min")
        else None,
        payload=dict(payload or {}),
    )
    db.add(task)
    if conv is not None and conv.mode == "ai":
        conv.mode = "waiting_admin"
    db.flush()
    emit_event(
        db,
        tenant_id,
        "admin_task.created",
        "admin_task",
        task.id,
        {
            "task_id": str(task.id),
            "number": task.number,
            "kind": kind,
            "priority": priority,
            "question": task.question,
            "branch_id": str(branch_id) if branch_id else None,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "contact_id": str(contact_id) if contact_id else None,
        },
    )
    audit(
        db,
        tenant_id,
        "system",
        None,
        "task.create",
        "admin_task",
        str(task.id),
        {"kind": kind, "priority": priority},
    )
    return task


def send_initial_alert(
    db: Session, task: AdminTask, *, event_id: uuid.UUID | None = None, now: datetime | None = None
) -> list[Notification]:
    """T0 Telegram alert (called by the outbox dispatcher on admin_task.created). Idempotent."""
    if task.status == "resolved":
        return []
    lang = _tenant_lang(db, task.tenant_id)
    return nt.notify_admins(
        db,
        task.tenant_id,
        "admin_task",
        task_text(db, task, lang),
        buttons=task_buttons(task, lang),
        branch_id=task.branch_id,
        dedupe_key=f"task:{task.id}:L0",
        recipients=_task_chat_ids(db, task, ("primary",)),
        payload={"task_id": str(task.id)},
        event_id=event_id,
        now=now,
    )


# ====================================================================== timers
def process_due_escalations(db: Session, now: datetime | None = None) -> int:
    """Run every due escalation step / SLA timeout. Deterministic for an injected ``now``.
    COMMITS after each task (keeps row locks short). Returns the number of tasks advanced."""
    now = now or utcnow()
    due = db.scalars(
        select(AdminTask.id)
        .where(
            ((AdminTask.status == "open") & (AdminTask.next_escalation_at <= now))
            | (AdminTask.status.in_(("open", "acked")) & (AdminTask.sla_due_at <= now))
        )
        .order_by(AdminTask.next_escalation_at.nulls_last())
        .limit(500)
    ).all()
    db.commit()
    advanced = 0
    for task_id in due:
        try:
            task = db.scalars(
                select(AdminTask)
                .where(AdminTask.id == task_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).first()
            if task is None:
                db.rollback()
                continue
            did = False
            if task.status == "open" and task.next_escalation_at and task.next_escalation_at <= now:
                _escalate_step(db, task, now)
                did = True
            if task.status in ("open", "acked") and task.sla_due_at and task.sla_due_at <= now:
                did = _sla_timeout(db, task, now) or did
            db.commit()
            advanced += did
        except Exception:
            db.rollback()
            log.exception("escalation step failed task=%s", task_id)
    return advanced


def _escalate_step(db: Session, task: AdminTask, now: datetime) -> None:
    s = escalation_settings(db, task.tenant_id)
    p = dict(task.payload or {})
    force_backup = bool(p.pop("force_backup", False))
    started = task.created_at or now
    if task.escalation_level == 0 and not force_backup:
        lang = _tenant_lang(db, task.tenant_id)
        text = nt.render("escalation_repeat", lang, minutes=s["repeat_after_min"]) + task_text(db, task, lang)
        nt.notify_admins(
            db,
            task.tenant_id,
            "admin_task",
            text,
            buttons=task_buttons(task, lang),
            branch_id=task.branch_id,
            dedupe_key=f"task:{task.id}:L1",
            recipients=_task_chat_ids(db, task, ("primary", "backup")),
            payload={"task_id": str(task.id)},
            now=now,
        )
        task.escalation_level = 1
        task.next_escalation_at = max(started + timedelta(minutes=float(s["call_after_min"])), now)
        audit(db, task.tenant_id, "system", None, "escalation.repeat", "admin_task", str(task.id), {})
        task.payload = p
        return

    calls_made = (
        db.scalar(
            select(func.count(Call.id)).where(
                Call.admin_task_id == task.id, Call.purpose == "admin_escalation"
            )
        )
        or 0
    )
    max_calls = int(s.get("max_call_attempts") or 0)
    reason = _calls_blocked_reason(db, task, s, now)
    if reason:
        p["calls_skipped"] = reason
        task.next_escalation_at = None
        audit(
            db,
            task.tenant_id,
            "system",
            None,
            "escalation.call_skipped",
            "admin_task",
            str(task.id),
            {"reason": reason},
        )
    elif calls_made >= max_calls and not force_backup:
        task.next_escalation_at = None
    else:
        target = _call_target(db, task, calls_made, force_backup)
        if target is None:
            p["calls_skipped"] = "no_phone"
            task.next_escalation_at = None
            audit(
                db,
                task.tenant_id,
                "system",
                None,
                "escalation.call_skipped",
                "admin_task",
                str(task.id),
                {"reason": "no_phone"},
            )
        else:
            place_escalation_call(db, task, target, now)
            calls_made += 1
            task.escalation_level = 1 + calls_made
            task.next_escalation_at = (
                now + timedelta(minutes=float(s["call_retry_min"])) if calls_made < max_calls else None
            )
    task.payload = p


def _calls_blocked_reason(db: Session, task: AdminTask, s: dict[str, Any], now: datetime) -> str | None:
    from app.channels.voice import get_voice_adapter

    if not s.get("calls_enabled", True):
        return "calls_disabled"
    if task.priority == "low":
        return "low_priority"
    if in_quiet_hours(now.astimezone(branch_zone(db, task.tenant_id, task.branch_id)), s.get("quiet_hours")):
        return "quiet_hours"
    if get_voice_adapter(db, task.tenant_id) is None:
        return "voice_not_configured"
    return None


def _call_target(db: Session, task: AdminTask, calls_made: int, force_backup: bool) -> User | None:
    primary = [
        u for _, u in nt.escalation_members(db, task.tenant_id, task.branch_id, "primary") if u.phone_e164
    ]
    backup = [
        u for _, u in nt.escalation_members(db, task.tenant_id, task.branch_id, "backup") if u.phone_e164
    ]
    if force_backup and backup:
        return backup[0]
    if calls_made == 0 or not backup:
        pool = primary or backup
        return pool[calls_made % len(pool)] if pool else None
    return backup[(calls_made - 1) % len(backup)]


def place_escalation_call(db: Session, task: AdminTask, user: User, now: datetime) -> Call:
    from app.channels.voice import get_voice_adapter

    call = Call(
        tenant_id=task.tenant_id,
        direction="out",
        purpose="admin_escalation",
        to_number=user.phone_e164,
        admin_task_id=task.id,
        contact_id=None,
        status="queued",
        created_at=now,
    )
    db.add(call)
    db.flush()
    adapter = get_voice_adapter(db, task.tenant_id)
    try:
        if adapter is None:
            raise RuntimeError("voice not configured")
        placed = adapter.place_call(user.phone_e164, call_id=call.id)
        call.provider_ref, call.status = placed.provider_ref, placed.status
    except Exception as exc:  # noqa: BLE001 - failed call is recorded; next step tries the next person
        call.status = "failed"
        call.transcript = f"error: {exc}"[:500]
        log.warning("escalation call failed task=%s err=%s", task.id, exc)
    audit(
        db,
        task.tenant_id,
        "system",
        None,
        "escalation.call",
        "admin_task",
        str(task.id),
        {"call_id": str(call.id), "to": mask_phone(user.phone_e164), "status": call.status},
    )
    return call


def _sla_timeout(db: Session, task: AdminTask, now: datetime) -> bool:
    """Tell the client ONCE that the question is with a specialist (no false promises)."""
    conv = db.get(Conversation, task.conversation_id) if task.conversation_id else None
    sp = db.begin_nested()
    try:
        if conv is not None and conv.mode != "human":
            nt.send_to_client(
                db, task.tenant_id, conv.id, nt.render("client_sla", conv.language), author="system"
            )
        sp.commit()
    except Exception as exc:  # noqa: BLE001 - retried on the next tick
        sp.rollback()
        log.warning("SLA message not sent task=%s err=%s", task.id, exc)
        return False
    task.sla_due_at = None
    task.payload = {**(task.payload or {}), "sla_notified_at": now.isoformat()}
    audit(db, task.tenant_id, "system", None, "escalation.sla_timeout", "admin_task", str(task.id), {})
    return True


# ====================================================================== ACK / resolve
def ack_task(
    db: Session,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID | None,
    via: str,
    *,
    now: datetime | None = None,
    source_call_id: uuid.UUID | None = None,
) -> AdminTask:
    """Explicit ACK (via: telegram|crm|voice). Idempotent: a second ACK returns the task unchanged."""
    now = now or utcnow()
    db.flush()  # session has autoflush=False; pending changes must not be clobbered by the reloads below
    task = get_or_404(db, AdminTask, task_id, tenant_id)
    won = db.execute(
        update(AdminTask)
        .where(AdminTask.id == task.id, AdminTask.tenant_id == tenant_id, AdminTask.status == "open")
        .values(status="acked", ack_at=now, assignee_user_id=user_id, next_escalation_at=None)
        .returning(AdminTask.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    db.refresh(task)
    if won is None:
        return task
    _stop_escalation(db, task, exclude_call_id=source_call_id)
    audit(
        db,
        tenant_id,
        "user" if user_id else "system",
        str(user_id) if user_id else None,
        "task.ack",
        "admin_task",
        str(task.id),
        {"via": via},
    )
    emit_event(
        db,
        tenant_id,
        "admin_task.acked",
        "admin_task",
        task.id,
        {
            "task_id": str(task.id),
            "number": task.number,
            "user_id": str(user_id) if user_id else None,
            "via": via,
        },
    )
    return task


def _stop_escalation(db: Session, task: AdminTask, *, exclude_call_id: uuid.UUID | None = None) -> None:
    from app.channels.voice import get_voice_adapter

    calls = db.scalars(
        select(Call)
        .where(Call.admin_task_id == task.id, Call.status.in_(ACTIVE_CALL_STATUSES))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    adapter = get_voice_adapter(db, task.tenant_id) if calls else None
    for call in calls:
        if call.id == exclude_call_id:
            continue
        call.status = "cancelled"
        if adapter is not None and call.provider_ref:
            try:
                adapter.cancel_call(call.provider_ref)
            except Exception as exc:  # noqa: BLE001 - provider hiccup must not block ACK
                log.warning("cancel call failed call=%s err=%s", call.id, exc)
    db.execute(
        update(Notification)
        .where(
            Notification.tenant_id == task.tenant_id,
            Notification.channel == "telegram",
            Notification.status == "pending",
            Notification.dedupe_key.like(f"task:{task.id}:L%"),
        )
        .values(status="cancelled", last_error="task acknowledged", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )


def resolve_task(
    db: Session,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID | None,
    answer: str,
    relay_to_client: bool,
    add_to_knowledge: bool,
    *,
    now: datetime | None = None,
) -> AdminTask:
    now = now or utcnow()
    db.flush()
    get_or_404(db, AdminTask, task_id, tenant_id)
    task = db.scalars(
        select(AdminTask)
        .where(AdminTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if task.status == "resolved":
        raise DomainError(409, "TASK_ALREADY_RESOLVED", "Task is already resolved")
    answer = (answer or "").strip()
    if not answer:
        raise DomainError(422, "EMPTY_ANSWER", "Answer is empty")
    conv = db.get(Conversation, task.conversation_id) if task.conversation_id else None
    if relay_to_client and conv is None:
        raise DomainError(422, "NO_CONVERSATION", "Task has no client conversation to relay the answer to")
    if task.status == "open":
        task = ack_task(db, tenant_id, task.id, user_id, "crm", now=now)
    else:
        _stop_escalation(db, task)
    task.status, task.resolved_at, task.answer = "resolved", now, answer
    task.add_to_knowledge = bool(add_to_knowledge)
    task.next_escalation_at = None
    task.sla_due_at = None
    task.assignee_user_id = task.assignee_user_id or user_id
    if relay_to_client and conv is not None:
        try:
            nt.send_to_client(
                db,
                tenant_id,
                conv.id,
                nt.render("client_relay", conv.language, answer=answer),
                author="human",
                author_user_id=user_id,
            )
        except nt.DeliveryError as exc:
            raise DomainError(503, "MESSAGING_UNAVAILABLE", str(exc)) from exc
    if conv is not None and conv.mode == "waiting_admin":
        conv.mode = "ai"
    article_id = None
    if add_to_knowledge:
        lang = (conv.language if conv else None) or db.get(Tenant, tenant_id).default_language or "ru"
        article = KnowledgeArticle(
            tenant_id=tenant_id,
            section="faq",
            language=lang,
            title=task.question[:300],
            content=answer,
            status="draft",
            author_id=user_id,
            test_questions=[task.question[:500]],
        )
        db.add(article)
        db.flush()
        article_id = str(article.id)
        task.payload = {**(task.payload or {}), "knowledge_article_id": article_id}
    audit(
        db,
        tenant_id,
        "user" if user_id else "system",
        str(user_id) if user_id else None,
        "task.resolve",
        "admin_task",
        str(task.id),
        {"relay_to_client": relay_to_client, "knowledge_article_id": article_id},
    )
    emit_event(
        db,
        tenant_id,
        "admin_task.resolved",
        "admin_task",
        task.id,
        {
            "task_id": str(task.id),
            "number": task.number,
            "relay_to_client": relay_to_client,
            "answer": answer,
            "user_id": str(user_id) if user_id else None,
        },
    )
    return task


def request_backup(
    db: Session, task: AdminTask, now: datetime | None = None, *, explicit: bool = False
) -> None:
    """Move to the next person on the next worker tick.
    no-answer/failed call (explicit=False): only pulls the pending step forward, the call cap still applies.
    IVR digit 2 (explicit=True): calls the backup even beyond the cap — but only once per task."""
    if task.status != "open":
        return
    now = now or utcnow()
    p = dict(task.payload or {})
    if explicit and not p.get("backup_forced"):
        p.update(force_backup=True, backup_forced=True)
        task.payload = p
        task.escalation_level = max(task.escalation_level, 1)
        task.next_escalation_at = now
    elif task.next_escalation_at is not None and task.next_escalation_at > now:
        task.next_escalation_at = now


def on_task_acked(db: Session, task: AdminTask) -> int:
    """Best effort: edit already delivered Telegram alerts to show who took the task (no buttons except Answer)."""
    from app.channels.telegram import TelegramError, client_for_tenant

    sent = db.scalars(
        select(Notification).where(
            Notification.tenant_id == task.tenant_id,
            Notification.channel == "telegram",
            Notification.status == "sent",
            Notification.provider_ref.is_not(None),
            Notification.dedupe_key.like(f"task:{task.id}:L%"),
        )
    ).all()
    if not sent:
        return 0
    lang = _tenant_lang(db, task.tenant_id)
    user = db.get(User, task.assignee_user_id) if task.assignee_user_id else None
    suffix = nt.render("task_taken", lang, name=(user.full_name or user.email) if user else "—")
    client = client_for_tenant(db, task.tenant_id)
    edited = 0
    for n in sent:
        try:
            client.edit_message_text(
                n.recipient,
                int(n.provider_ref),
                (n.payload or {}).get("text", "") + suffix,
                task_buttons(task, lang, taken=True),
            )
            edited += 1
        except (TelegramError, ValueError) as exc:
            log.info("edit telegram message failed notification=%s err=%s", n.id, exc)
    return edited
