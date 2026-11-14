"""Dashboard reports + CSV/XLSX export (ТЗ §14.3-14.4). Money is hidden for roles without finance.read."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.schemas import ReportOverview, error_responses
from app.db import get_db
from app.deps import AuthContext, require
from app.services import reports as svc
from app.services.audit import audit
from app.services.common import DomainError

router = APIRouter()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_response(media: str) -> dict:
    return {
        200: {"content": {media: {"schema": {"type": "string", "format": "binary"}}}, "description": "File"}
    }


def _filter(
    db: Session,
    ctx: AuthContext,
    date_from: date | None,
    date_to: date | None,
    branch_id: uuid.UUID | None,
    staff_id: uuid.UUID | None,
    service_id: uuid.UUID | None,
    source: str | None,
) -> svc.ReportFilter:
    d0, d1 = svc.default_range(db, ctx.tenant_id)
    date_from = date_from or (date_to - (d1 - d0) if date_to else d0)
    date_to = date_to or max(d1, date_from)
    if branch_id is not None:
        if not ctx.branch_allowed(branch_id):
            raise DomainError(403, "BRANCH_FORBIDDEN", "No access to this branch")
        branches: list[uuid.UUID] | None = [branch_id]
    else:
        branches = [uuid.UUID(b) for b in ctx.branch_scope] if ctx.branch_scope else None
    if ctx.role == "staff" and ctx.staff_id:
        staff_id = ctx.staff_id
    return svc.ReportFilter(
        date_from,
        date_to,
        branch_ids=branches,
        staff_id=staff_id,
        service_id=service_id,
        source=source or None,
    )


def _report(db: Session, ctx: AuthContext, f: svc.ReportFilter) -> dict:
    report = svc.overview(db, ctx.tenant_id, f)
    return report if ctx.can("finance.read") else svc.mask_finance(report)


@router.get("/overview", response_model=ReportOverview, responses=error_responses(403))
def overview(
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    source: str | None = None,
    ctx: AuthContext = Depends(require("reports.read")),
    db: Session = Depends(get_db),
):
    f = _filter(db, ctx, date_from, date_to, branch_id, staff_id, service_id, source)
    return {
        "date_from": f.date_from,
        "date_to": f.date_to,
        "currency": svc.tenant_currency(db, ctx.tenant_id),
        **_report(db, ctx, f),
    }


def _export(fmt: str, db: Session, ctx: AuthContext, f: svc.ReportFilter) -> Response:
    report = _report(db, ctx, f)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "report.export",
        "report",
        None,
        {"format": fmt, "date_from": f.date_from.isoformat(), "date_to": f.date_to.isoformat()},
        ip=ctx.ip,
    )
    db.commit()
    name = f"report_{f.date_from.isoformat()}_{f.date_to.isoformat()}.{fmt}"
    if fmt == "csv":
        body, media = svc.export_csv(report), "text/csv; charset=utf-8"
    else:
        body, media = svc.export_xlsx(report), XLSX
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export.csv", response_class=Response, responses=_file_response("text/csv"))
def export_csv(
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    source: str | None = None,
    ctx: AuthContext = Depends(require("reports.export")),
    db: Session = Depends(get_db),
):
    return _export(
        "csv", db, ctx, _filter(db, ctx, date_from, date_to, branch_id, staff_id, service_id, source)
    )


@router.get("/export.xlsx", response_class=Response, responses=_file_response(XLSX))
def export_xlsx(
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: uuid.UUID | None = None,
    staff_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    source: str | None = None,
    ctx: AuthContext = Depends(require("reports.export")),
    db: Session = Depends(get_db),
):
    return _export(
        "xlsx", db, ctx, _filter(db, ctx, date_from, date_to, branch_id, staff_id, service_id, source)
    )
