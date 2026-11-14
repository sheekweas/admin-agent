"""Service catalogue (ТЗ §8.1, §18: GET /v1/services)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.api.bookings import Money
from app.db import get_db
from app.deps import AuthContext, require
from app.models import Service, Staff, StaffService
from app.services.audit import audit
from app.services.common import DomainError, get_or_404

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


class ServiceOut(BaseModel):
    id: uuid.UUID
    category: str
    title_ru: str
    title_kk: str
    description: str
    duration_min: int
    price: Money
    buffer_before_min: int
    buffer_after_min: int
    resource_type: str | None
    published: bool
    active: bool
    staff_ids: list[uuid.UUID]


class ServiceIn(BaseModel):
    category: str = Field(default="", max_length=100)
    title_ru: str = Field(min_length=1, max_length=200)
    title_kk: str = Field(default="", max_length=200)
    description: str = ""
    duration_min: int = Field(gt=0, le=24 * 60)
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    buffer_before_min: int = Field(default=0, ge=0, le=240)
    buffer_after_min: int = Field(default=0, ge=0, le=240)
    resource_type: str | None = Field(default=None, max_length=50)
    published: bool = True
    active: bool = True
    staff_ids: list[uuid.UUID] = []


class ServicePatch(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    title_ru: str | None = Field(default=None, min_length=1, max_length=200)
    title_kk: str | None = Field(default=None, max_length=200)
    description: str | None = None
    duration_min: int | None = Field(default=None, gt=0, le=24 * 60)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    buffer_before_min: int | None = Field(default=None, ge=0, le=240)
    buffer_after_min: int | None = Field(default=None, ge=0, le=240)
    resource_type: str | None = Field(default=None, max_length=50)
    published: bool | None = None
    active: bool | None = None
    staff_ids: list[uuid.UUID] | None = None


class ServiceList(BaseModel):
    items: list[ServiceOut]
    total: int


def _staff_map(db: Session, service_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if service_ids:
        for service_id, staff_id in db.execute(
            select(StaffService.service_id, StaffService.staff_id).where(
                StaffService.service_id.in_(service_ids)
            )
        ):
            out[service_id].append(staff_id)
    return out


def _out(s: Service, staff_ids: list[uuid.UUID]) -> ServiceOut:
    return ServiceOut(
        id=s.id,
        category=s.category,
        title_ru=s.title_ru,
        title_kk=s.title_kk,
        description=s.description,
        duration_min=s.duration_min,
        price=s.price,
        buffer_before_min=s.buffer_before_min,
        buffer_after_min=s.buffer_after_min,
        resource_type=s.resource_type,
        published=s.published,
        active=s.active,
        staff_ids=staff_ids,
    )


def _set_staff(db: Session, tenant_id: uuid.UUID, service: Service, staff_ids: list[uuid.UUID]) -> None:
    """Replace assignments, keeping per-staff duration/price overrides of staff that stay assigned."""
    wanted = set(staff_ids)
    if wanted:
        found = set(db.scalars(select(Staff.id).where(Staff.tenant_id == tenant_id, Staff.id.in_(wanted))))
        if found != wanted:
            raise DomainError(422, "UNKNOWN_STAFF", "Some staff ids do not exist")
    current = set(db.scalars(select(StaffService.staff_id).where(StaffService.service_id == service.id)))
    if current - wanted:
        db.execute(
            delete(StaffService).where(
                StaffService.service_id == service.id, StaffService.staff_id.in_(current - wanted)
            )
        )
    for staff_id in wanted - current:
        db.add(StaffService(staff_id=staff_id, service_id=service.id))


@router.get("", response_model=ServiceList)
def list_services(
    ctx: Annotated[AuthContext, Depends(require("services.read"))],
    db: DbSession,
    branch_id: uuid.UUID | None = None,
    published: bool | None = None,
    q: str | None = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    stmt = select(Service).where(Service.tenant_id == ctx.tenant_id)
    if not include_inactive:
        stmt = stmt.where(Service.active.is_(True))
    if published is not None:
        stmt = stmt.where(Service.published.is_(published))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Service.title_ru.ilike(like), Service.title_kk.ilike(like), Service.category.ilike(like))
        )
    if branch_id is not None:
        stmt = stmt.where(
            Service.id.in_(
                select(StaffService.service_id)
                .join(Staff, Staff.id == StaffService.staff_id)
                .where(Staff.branch_id == branch_id, Staff.tenant_id == ctx.tenant_id, Staff.active.is_(True))
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Service.category, Service.title_ru).limit(limit).offset(offset)).all()
    staff = _staff_map(db, [s.id for s in rows])
    return ServiceList(items=[_out(s, staff[s.id]) for s in rows], total=total)


@router.get("/{service_id}", response_model=ServiceOut)
def get_service(
    service_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("services.read"))], db: DbSession
):
    s = get_or_404(db, Service, service_id, ctx.tenant_id)
    return _out(s, _staff_map(db, [s.id])[s.id])


@router.post("", response_model=ServiceOut, status_code=201)
def create_service(
    body: ServiceIn, ctx: Annotated[AuthContext, Depends(require("services.write"))], db: DbSession
):
    s = Service(tenant_id=ctx.tenant_id, **body.model_dump(exclude={"staff_ids"}))
    db.add(s)
    db.flush()
    _set_staff(db, ctx.tenant_id, s, body.staff_ids)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "service.create",
        "service",
        str(s.id),
        body.model_dump(mode="json"),
        ip=ctx.ip,
    )
    db.commit()
    db.refresh(s)
    return _out(s, _staff_map(db, [s.id])[s.id])


@router.patch("/{service_id}", response_model=ServiceOut)
def patch_service(
    service_id: uuid.UUID,
    body: ServicePatch,
    ctx: Annotated[AuthContext, Depends(require("services.write"))],
    db: DbSession,
):
    s = get_or_404(db, Service, service_id, ctx.tenant_id)
    changes = body.model_dump(exclude_unset=True)
    staff_ids = changes.pop("staff_ids", None)
    for k, v in changes.items():
        if v is None and k != "resource_type":
            continue
        setattr(s, k, v)
    if staff_ids is not None:
        _set_staff(db, ctx.tenant_id, s, staff_ids)
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "service.update",
        "service",
        str(s.id),
        body.model_dump(mode="json", exclude_unset=True),
        ip=ctx.ip,
    )
    db.commit()
    db.refresh(s)
    return _out(s, _staff_map(db, [s.id])[s.id])


@router.delete("/{service_id}", status_code=204)
def delete_service(
    service_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(require("services.write"))], db: DbSession
):
    """Soft delete: history (bookings, reports) keeps referencing the service."""
    s = get_or_404(db, Service, service_id, ctx.tenant_id)
    s.active = False
    s.published = False
    audit(db, ctx.tenant_id, "user", str(ctx.user.id), "service.delete", "service", str(s.id), {}, ip=ctx.ip)
    db.commit()
    return Response(status_code=204)
