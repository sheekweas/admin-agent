"""Prometheus metrics (ТЗ §21 "Наблюдаемость"): HTTP RED metrics + operational gauges read from the DB.

GET /metrics (text exposition, not in OpenAPI). Access: with METRICS_TOKEN set the request must carry
"Authorization: Bearer <token>"; without a token only loopback / private-network clients are served
(Prometheus inside the docker network). The reverse proxy additionally blocks /metrics from the internet.

Gauges are platform-wide aggregates (no tenant ids, no PII) computed at scrape time with a statement
timeout, so a slow database cannot hang the scrape. One registry per process: run one uvicorn process per
container (scale with replicas) or sum the series across instances in PromQL.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from collections.abc import Iterator

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

log = logging.getLogger("api.metrics")

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "crm_http_requests_total",
    "HTTP requests by method, route template and status code",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "crm_http_request_duration_seconds",
    "HTTP request latency by method and route template",
    ["method", "route"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

UNMATCHED = "unmatched"  # 404s for unknown paths share one label (bounded cardinality)


_full_paths: dict[int, str] = {}  # id(route object) -> full path template incl. router prefixes
_static_paths: set[str] = set()  # app-level routes without scope["route"]: /docs, /metrics


def _index_routes(app) -> None:
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # older FastAPI: included routes already carry the full path
        return
    for rc in iter_route_contexts(app.routes):
        for r in (rc.original_route, rc.route):
            _full_paths[id(r)] = rc.path
        if "{" not in rc.path:
            _static_paths.add(rc.path)


def route_label(scope: dict) -> str:
    """Path template of the matched route ("/v1/bookings/{booking_id}"), never the raw path."""
    route = scope.get("route")
    if route is None:
        if not _static_paths and scope.get("app") is not None:
            _index_routes(scope["app"])
        path = scope.get("path")
        return path if path in _static_paths else UNMATCHED
    full = _full_paths.get(id(route))
    if full is None and scope.get("app") is not None:
        _index_routes(scope["app"])
        full = _full_paths.setdefault(id(route), getattr(route, "path", UNMATCHED))
    return full or UNMATCHED


def observe_request(scope: dict, method: str, status: int, seconds: float) -> None:
    route = route_label(scope)
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


# ---------------------------------------------------------------- DB-backed gauges
_QUERIES: dict[str, str] = {
    "outbox": "SELECT status, count(*) FROM outbox_events WHERE status IN ('pending', 'failed') GROUP BY status",
    "outbox_age": "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) "
    "FROM outbox_events WHERE status = 'pending'",
    "tasks": "SELECT status, count(*) FROM admin_tasks WHERE status IN ('open', 'acked') GROUP BY status",
    "tasks_overdue": "SELECT count(*) FROM admin_tasks WHERE status IN ('open', 'acked') "
    "AND sla_due_at IS NOT NULL AND sla_due_at < now()",
    "notifications": "SELECT status, count(*) FROM notifications "
    "WHERE status IN ('pending', 'failed', 'dead') GROUP BY status",
    "ai_runs": "SELECT count(*), "
    "count(*) FILTER (WHERE error IS NOT NULL AND error <> 'unverified_booking_claim'), "
    "count(*) FILTER (WHERE error = 'unverified_booking_claim') "
    "FROM ai_runs WHERE created_at > now() - interval '1 hour'",
    "esc_calls": "SELECT status, count(*) FROM calls WHERE purpose = 'admin_escalation' "
    "AND created_at > now() - interval '1 hour' GROUP BY status",
}


def _by_status(rows, statuses: tuple[str, ...]) -> dict[str, float]:
    out = dict.fromkeys(statuses, 0.0)
    for status, n in rows:
        out[str(status)] = float(n)
    return out


class OperationalCollector(Collector):
    def collect(self) -> Iterator[GaugeMetricFamily]:
        from app.db import SessionLocal

        s = get_settings()
        yield GaugeMetricFamily(
            "crm_ai_globally_disabled",
            "1 when the platform AI kill switch (AI_GLOBALLY_DISABLED) is on",
            value=1.0 if s.ai_globally_disabled else 0.0,
        )
        started = time.perf_counter()
        try:
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = 3000"))
                outbox = _by_status(db.execute(text(_QUERIES["outbox"])).all(), ("pending", "failed"))
                outbox_age = float(db.execute(text(_QUERIES["outbox_age"])).scalar() or 0)
                tasks = _by_status(db.execute(text(_QUERIES["tasks"])).all(), ("open", "acked"))
                overdue = float(db.execute(text(_QUERIES["tasks_overdue"])).scalar() or 0)
                notes = _by_status(
                    db.execute(text(_QUERIES["notifications"])).all(), ("pending", "failed", "dead")
                )
                ai_total, ai_errors, ai_guard = db.execute(text(_QUERIES["ai_runs"])).one()
                calls = _by_status(db.execute(text(_QUERIES["esc_calls"])).all(), ())
                db.rollback()
        except Exception as exc:  # noqa: BLE001 - the scrape must still answer (db_up=0 triggers the alert)
            log.warning("metrics: database query failed: %s", type(exc).__name__)
            yield GaugeMetricFamily("crm_db_up", "1 if the metrics queries reached PostgreSQL", value=0.0)
            return
        yield GaugeMetricFamily("crm_db_up", "1 if the metrics queries reached PostgreSQL", value=1.0)
        yield GaugeMetricFamily(
            "crm_metrics_db_query_seconds",
            "Time spent on the metrics queries",
            value=time.perf_counter() - started,
        )

        g = GaugeMetricFamily("crm_outbox_events", "Outbox events not yet processed", labels=["status"])
        for status, n in outbox.items():
            g.add_metric([status], n)
        yield g
        yield GaugeMetricFamily(
            "crm_outbox_oldest_pending_age_seconds",
            "Age of the oldest pending outbox event",
            value=outbox_age,
        )
        g = GaugeMetricFamily("crm_admin_tasks", "Admin tasks not yet resolved", labels=["status"])
        for status, n in tasks.items():
            g.add_metric([status], n)
        yield g
        yield GaugeMetricFamily(
            "crm_admin_tasks_sla_overdue", "Open/acked admin tasks past their SLA", value=overdue
        )
        g = GaugeMetricFamily(
            "crm_notifications", "Notifications waiting, retrying or dead (DLQ)", labels=["status"]
        )
        for status, n in notes.items():
            g.add_metric([status], n)
        yield g
        yield GaugeMetricFamily("crm_ai_runs_last_hour", "AI turns in the last hour", value=float(ai_total))
        yield GaugeMetricFamily(
            "crm_ai_run_errors_last_hour",
            "Failed AI turns in the last hour (provider/tool errors, kill switch; fallback was sent)",
            value=float(ai_errors),
        )
        yield GaugeMetricFamily(
            "crm_ai_guard_blocks_last_hour",
            "AI replies blocked by the booking-claim guard in the last hour",
            value=float(ai_guard),
        )
        g = GaugeMetricFamily(
            "crm_escalation_calls_last_hour", "Admin escalation calls in the last hour", labels=["status"]
        )
        for status, n in calls.items():
            g.add_metric([status], n)
        yield g


REGISTRY.register(OperationalCollector())


# ---------------------------------------------------------------- endpoint
def _internal(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def authorize(request: Request) -> int | None:
    """None if the scrape is allowed, otherwise the HTTP status to answer with."""
    s = get_settings()
    if not s.metrics_enabled:
        return 404
    token = (s.metrics_token or "").strip()
    if token:
        sent = request.headers.get("authorization", "")
        return None if hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()) else 401
    return None if _internal(request.client.host if request.client else None) else 403


async def metrics_endpoint(request: Request) -> Response:
    status = authorize(request)
    if status is not None:
        return JSONResponse({"detail": "Not available"}, status)
    body = await run_in_threadpool(generate_latest, REGISTRY)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
