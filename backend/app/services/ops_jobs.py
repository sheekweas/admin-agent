"""Periodic ops jobs: daily branch summary and integration reconciliation (ТЗ §12.1, §19)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Branch, Integration, Notification, OutboxEvent, Tenant
from app.services import notifications as nt
from app.services.common import tz, utcnow
from app.services.reports import ReportFilter, overview
from app.services.tenant_settings import merged_settings

log = logging.getLogger("ops_jobs")


def daily_summary(db: Session, now: datetime | None = None) -> int:
    """Send each active branch its summary once per local date, at/after settings.daily_summary_hour.
    Idempotent via dedupe_key daily:<branch>:<local date>. Commits per branch."""
    now = now or utcnow()
    rows = db.execute(
        select(Branch, Tenant)
        .join(Tenant, Tenant.id == Branch.tenant_id)
        .where(Branch.active.is_(True), Tenant.status == "active")
    ).all()
    queued = 0
    for branch, tenant in rows:
        hour = int(merged_settings(tenant).get("daily_summary_hour", 21))
        local = now.astimezone(tz(branch.timezone))
        if local.hour < hour:
            continue
        key = f"daily:{branch.id}:{local.date().isoformat()}"
        if db.scalar(select(Notification.id).where(Notification.dedupe_key.like(key + ":%")).limit(1)):
            continue
        r = overview(db, tenant.id, ReportFilter(local.date(), local.date(), branch_ids=[branch.id]))["kpi"]
        lang = nt.lang_of(tenant.default_language)
        text = nt.render(
            "daily_summary",
            lang,
            date=local.strftime("%d.%m.%Y"),
            branch=branch.name,
            bookings=r["bookings"],
            visits=r["visits"],
            revenue=r["revenue"],
            refunds=r["refunds"],
            currency=tenant.currency,
            cancellations=r["cancellations"],
            no_shows=r["no_shows"],
            open_tasks=r["open_tasks"],
        )
        queued += len(
            nt.notify_admins(
                db, tenant.id, "daily_summary", text, branch_id=branch.id, dedupe_key=key, now=now
            )
        )
        db.commit()
    return queued


def reconcile_integrations(db: Session, now: datetime | None = None) -> int:
    """Find stuck/failed deliveries per tenant, alert owners/admins once per hour, flag integration status."""
    now = now or utcnow()
    stale_outbox = dict(
        db.execute(
            select(OutboxEvent.tenant_id, func.count())
            .where(
                or_(
                    OutboxEvent.status == "failed",
                    (OutboxEvent.status == "pending")
                    & (OutboxEvent.created_at < now - timedelta(minutes=15)),
                )
            )
            .group_by(OutboxEvent.tenant_id)
        ).all()
    )
    bad_notifications = dict(
        db.execute(
            select(Notification.tenant_id, func.count())
            .where(
                or_(
                    (Notification.status == "dead") & (Notification.updated_at >= now - timedelta(hours=1)),
                    (Notification.status == "pending")
                    & (Notification.next_attempt_at < now - timedelta(minutes=30)),
                )
            )
            .group_by(Notification.tenant_id)
        ).all()
    )
    dead_tg = set(
        db.scalars(
            select(Notification.tenant_id).where(
                Notification.channel == "telegram",
                Notification.status == "dead",
                Notification.updated_at >= now - timedelta(hours=1),
            )
        ).all()
    )
    for integ in (
        db.scalars(
            select(Integration).where(Integration.kind == "telegram", Integration.tenant_id.in_(dead_tg))
        ).all()
        if dead_tg
        else []
    ):
        integ.status, integ.last_error = "error", "undelivered Telegram notifications in the last hour"
    alerted = 0
    for tenant_id in set(stale_outbox) | set(bad_notifications):
        count = stale_outbox.get(tenant_id, 0) + bad_notifications.get(tenant_id, 0)
        tenant = db.get(Tenant, tenant_id)
        text = nt.render("integration_alert", nt.lang_of(tenant.default_language), count=count)
        alerted += bool(
            nt.notify_admins(
                db,
                tenant_id,
                "integration_alert",
                text,
                dedupe_key=f"reconcile:{tenant_id}:{now:%Y%m%d%H}",
                now=now,
            )
        )
        log.warning(
            "reconcile: tenant=%s stale_outbox=%s bad_notifications=%s",
            tenant_id,
            stale_outbox.get(tenant_id, 0),
            bad_notifications.get(tenant_id, 0),
        )
    db.commit()
    return alerted
