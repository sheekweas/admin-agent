"""Celery tasks: thin wrappers that open a session and call services. Each ``run_*`` function is a plain
synchronous callable (tests / scripts); the ``@celery_app.task`` objects only add scheduling/retries."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.workers.celery_app import celery_app

log = logging.getLogger("worker")


def _with_session(fn: Callable[[Session], Any], db: Session | None = None) -> Any:
    if db is not None:
        return fn(db)
    session = SessionLocal()
    try:
        result = fn(session)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------- plain callables
def run_dispatch_outbox(db: Session | None = None, now: datetime | None = None) -> int:
    from app.services.outbox import dispatch_outbox

    return _with_session(lambda s: dispatch_outbox(s, now), db)


def run_send_due_notifications(db: Session | None = None, now: datetime | None = None) -> int:
    from app.services.notifications import send_due_notifications

    return _with_session(lambda s: send_due_notifications(s, now), db)


def run_process_escalations(db: Session | None = None, now: datetime | None = None) -> int:
    from app.services.escalation import process_due_escalations

    return _with_session(lambda s: process_due_escalations(s, now), db)


def run_release_expired_holds(db: Session | None = None, now: datetime | None = None) -> int:
    def _run(s: Session) -> int:
        try:
            from app.services.booking import release_expired_holds
        except ImportError:
            log.warning("booking.release_expired_holds not available")
            return 0
        n = release_expired_holds(s, now)
        s.commit()
        return n

    return _with_session(_run, db)


def run_deliver_stale_messages(db: Session | None = None) -> int:
    from app.services.messaging import deliver_stale_queued

    return _with_session(deliver_stale_queued, db)


def run_daily_summary(db: Session | None = None, now: datetime | None = None) -> int:
    from app.services.notifications import send_due_notifications
    from app.services.ops_jobs import daily_summary

    def _run(s: Session) -> int:
        n = daily_summary(s, now)
        send_due_notifications(s, now)
        return n

    return _with_session(_run, db)


def run_reconcile_integrations(db: Session | None = None, now: datetime | None = None) -> int:
    from app.services.ops_jobs import reconcile_integrations

    return _with_session(lambda s: reconcile_integrations(s, now), db)


def run_process_inbound_message(
    conversation_id: str | uuid.UUID,
    message_id: str | uuid.UUID,
    tenant_id: str | uuid.UUID | None = None,
    db: Session | None = None,
) -> Any:
    from app.models import Conversation
    from app.services.messaging import process_ai_turn

    def _run(s: Session) -> Any:
        conv_id = uuid.UUID(str(conversation_id))
        tid = uuid.UUID(str(tenant_id)) if tenant_id else None
        if tid is None:
            conv = s.get(Conversation, conv_id)
            if conv is None:
                log.warning("process_inbound_message: conversation %s not found", conv_id)
                return None
            tid = conv.tenant_id
        result = process_ai_turn(s, tid, conv_id, uuid.UUID(str(message_id)))
        s.commit()
        return result

    return _with_session(_run, db)


# ---------------------------------------------------------------- celery tasks
@celery_app.task(name="ops.dispatch_outbox")
def dispatch_outbox() -> int:
    return run_dispatch_outbox()


@celery_app.task(name="ops.send_due_notifications")
def send_due_notifications() -> int:
    return run_send_due_notifications()


@celery_app.task(name="ops.process_escalations")
def process_escalations() -> int:
    return run_process_escalations()


@celery_app.task(name="ops.release_expired_holds")
def release_expired_holds() -> int:
    return run_release_expired_holds()


@celery_app.task(name="ops.deliver_stale_messages")
def deliver_stale_messages() -> int:
    return run_deliver_stale_messages()


@celery_app.task(name="ops.daily_summary")
def daily_summary() -> int:
    return run_daily_summary()


@celery_app.task(name="ops.reconcile_integrations")
def reconcile_integrations() -> int:
    return run_reconcile_integrations()


@celery_app.task(name="ai.process_inbound_message", bind=True, max_retries=5, acks_late=True)
def process_inbound_message(
    self, conversation_id: str, message_id: str, tenant_id: str | None = None
) -> None:
    try:
        run_process_inbound_message(conversation_id, message_id, tenant_id)
    except Exception as exc:
        log.exception("process_inbound_message failed conv=%s msg=%s", conversation_id, message_id)
        raise self.retry(exc=exc, countdown=min(5 * 2**self.request.retries, 300)) from exc
