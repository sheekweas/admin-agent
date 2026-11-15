"""Celery app + beat schedule (ТЗ §19).

celery -A app.workers.celery_app worker --loglevel=INFO --concurrency=4
celery -A app.workers.celery_app beat --loglevel=INFO
"""

from __future__ import annotations

from celery import Celery

from app.config import get_settings, validate_settings
from app.logging_setup import install_redaction

validate_settings(get_settings())
install_redaction()

celery_app = Celery("ai_crm", broker=get_settings().redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    beat_schedule={
        # "expires": periodic ticks must not pile up while workers are down
        name: {"task": task, "schedule": every, "options": {"expires": max(every, 30.0)}}
        for name, task, every in (
            ("dispatch-outbox", "ops.dispatch_outbox", 5.0),
            ("send-due-notifications", "ops.send_due_notifications", 15.0),
            ("process-escalations", "ops.process_escalations", 30.0),
            ("release-expired-holds", "ops.release_expired_holds", 30.0),
            ("deliver-stale-messages", "ops.deliver_stale_messages", 30.0),
            ("daily-summary", "ops.daily_summary", 600.0),
            ("reconcile-integrations", "ops.reconcile_integrations", 3600.0),
            ("voice-retention", "ops.voice_retention", 3600.0),  # transcripts/recordings TTL, stale calls
            ("purge-auth-sessions", "ops.purge_auth_sessions", 3600.0),  # sessions expired/revoked > 30 days
        )
    },
)

celery = celery_app  # `celery -A app.workers.celery_app` discovers this name
