"""Dashboard / finance reports (ТЗ §14.3-14.4), computed in SQL.

Dates are branch-local: every timestamp is converted with its branch time zone (bookings, payments, visits,
leads) before taking the date, so "revenue for the 14th" means the 14th in Almaty, not in UTC.

Formulas
  revenue                = Σ received money (payments/prepayments/adjustments, status confirmed|refunded) by paid_at
  refunds                = Σ confirmed refunds by their own paid_at;  net_revenue = revenue − refunds
  bookings               = bookings with start in period and status confirmed|checked_in|completed|no_show
  visits                 = bookings checked_in|completed + walk-in visits (Visit without booking)
  conversion_to_booking  = unique contacts whose qualified lead (created in period) converted into a booking /
                           unique contacts with a qualified lead created in period × 100  (always 0–100; walk-ins
                           and admin bookings without a lead do not inflate it)
  conversion_to_visit    = visits (from bookings) / bookings × 100;   no_show_rate = no_shows / bookings × 100
  avg_check              = net revenue of paid bookings/visits / number of paid bookings/visits
  utilization            = booked client minutes / available working minutes (shifts − breaks ± exceptions)
  ai_resolution_rate     = AI conversations without human messages and without admin tasks / AI conversations
  service revenue        = paid amounts allocated to booking lines pro rata to the line price (not count × price)
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    Numeric,
    and_,
    case,
    cast,
    distinct,
    exists,
    extract,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.orm import Session

from app.models import (
    AdminTask,
    AIRun,
    Booking,
    BookingItem,
    Branch,
    Contact,
    Conversation,
    Lead,
    Message,
    Payment,
    ScheduleException,
    Service,
    Shift,
    Staff,
    Tenant,
    Visit,
)
from app.services.common import DomainError, tz, utcnow
from app.services.payments import REVENUE_STATUSES

CONFIRMED_SET = ("confirmed", "checked_in", "completed", "no_show")
VISIT_SET = ("checked_in", "completed")
OCCUPIED_SET = ("confirmed", "checked_in", "completed")
QUALIFIED_STAGES = ("qualified", "booking_pending", "booked", "visited", "repeat")
FUNNEL_ORDER = (
    "new",
    "info_requested",
    "qualified",
    "booking_pending",
    "booked",
    "visited",
    "repeat",
    "lost",
    "cancelled",
)
MAX_RANGE_DAYS = 400
MONEY_KPIS = ("revenue", "refunds", "net_revenue", "avg_check")
CENT = Decimal("0.01")


@dataclass
class ReportFilter:
    date_from: date
    date_to: date
    branch_ids: list[uuid.UUID] | None = None
    staff_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    source: str | None = None
    days: list[date] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.date_to < self.date_from:
            raise DomainError(422, "BAD_RANGE", "date_to is before date_from")
        n = (self.date_to - self.date_from).days + 1
        if n > MAX_RANGE_DAYS:
            raise DomainError(422, "RANGE_TOO_LARGE", f"Max {MAX_RANGE_DAYS} days")
        self.days = [self.date_from + timedelta(days=i) for i in range(n)]


def m(v: Any) -> str:
    return str(Decimal(v or 0).quantize(CENT))


def pct(n: float | Decimal, d: float | Decimal) -> float:
    return round(float(n) / float(d) * 100, 1) if d else 0.0


def default_tz_name(db: Session, tenant_id: uuid.UUID) -> str:
    b = db.scalars(select(Branch).where(Branch.tenant_id == tenant_id).order_by(Branch.created_at)).first()
    return b.timezone if b else "Asia/Almaty"


def default_range(db: Session, tenant_id: uuid.UUID, days: int = 30) -> tuple[date, date]:
    today = utcnow().astimezone(tz(default_tz_name(db, tenant_id))).date()
    return today - timedelta(days=days - 1), today


def _tz_sql(tz_col, default_tz: str):
    # Inlined literal (not a bind param) so the same expression can be used in SELECT and GROUP BY.
    return func.coalesce(tz_col, literal_column("'" + default_tz.replace("'", "''") + "'"))


def _local_date(ts_col, tz_col, default_tz: str):
    return cast(func.timezone(_tz_sql(tz_col, default_tz), ts_col), Date)


def _local_hour(ts_col, tz_col, default_tz: str):
    return extract("hour", func.timezone(_tz_sql(tz_col, default_tz), ts_col))


def _service_bookings(f: ReportFilter):
    return select(BookingItem.booking_id).where(BookingItem.service_id == f.service_id)


def _source_contacts(tenant_id: uuid.UUID, f: ReportFilter):
    return select(Contact.id).where(Contact.tenant_id == tenant_id, Contact.source == f.source)


# ====================================================================== sections
def _payment_conds(tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> tuple[list, Any]:
    d = _local_date(Payment.paid_at, Branch.timezone, dtz)
    conds = [Payment.tenant_id == tenant_id, d.between(f.date_from, f.date_to)]
    if f.branch_ids is not None:
        conds.append(Payment.branch_id.in_(f.branch_ids))
    if f.staff_id:
        conds.append(Payment.staff_id == f.staff_id)
    if f.service_id:
        conds.append(Payment.booking_id.in_(_service_bookings(f)))
    if f.source:
        conds.append(Payment.contact_id.in_(_source_contacts(tenant_id, f)))
    return conds, d


GROSS = case((and_(Payment.kind != "refund", Payment.status.in_(REVENUE_STATUSES)), Payment.amount), else_=0)
REFUND = case((and_(Payment.kind == "refund", Payment.status == "confirmed"), Payment.amount), else_=0)
SIGNED = GROSS - REFUND


def _payments(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> dict[str, Any]:
    conds, d = _payment_conds(tenant_id, f, dtz)
    base = select().select_from(Payment).outerjoin(Branch, Branch.id == Payment.branch_id).where(*conds)
    gross, refunds = db.execute(
        base.add_columns(func.coalesce(func.sum(GROSS), 0), func.coalesce(func.sum(REFUND), 0))
    ).one()
    by_day = dict(db.execute(base.add_columns(d, func.sum(SIGNED)).group_by(d)).all())
    entity = func.coalesce(Payment.booking_id, Payment.visit_id)
    per_entity = db.execute(
        base.add_columns(entity, func.sum(SIGNED)).where(entity.is_not(None)).group_by(entity)
    ).all()
    paid = [Decimal(v) for _, v in per_entity if v and Decimal(v) > 0]
    by_staff = dict(
        db.execute(base.add_columns(Payment.staff_id, func.sum(SIGNED)).group_by(Payment.staff_id)).all()
    )
    return {
        "gross": Decimal(gross),
        "refunds": Decimal(refunds),
        "by_day": by_day,
        "by_staff": by_staff,
        "avg_check": (sum(paid) / len(paid)) if paid else Decimal(0),
    }


def _booking_conds(tenant_id: uuid.UUID, f: ReportFilter, dtz: str, ts_col=None) -> tuple[list, Any]:
    d = _local_date(ts_col if ts_col is not None else Booking.start_at, Branch.timezone, dtz)
    conds = [Booking.tenant_id == tenant_id, d.between(f.date_from, f.date_to)]
    if f.branch_ids is not None:
        conds.append(Booking.branch_id.in_(f.branch_ids))
    if f.staff_id:
        conds.append(Booking.staff_id == f.staff_id)
    if f.service_id:
        conds.append(Booking.id.in_(_service_bookings(f)))
    if f.source:
        conds.append(Booking.contact_id.in_(_source_contacts(tenant_id, f)))
    return conds, d


def _bookings(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> dict[str, Any]:
    conds, d = _booking_conds(tenant_id, f, dtz)
    base = select().select_from(Booking).join(Branch, Branch.id == Booking.branch_id).where(*conds)
    st = Booking.status

    def cnt(statuses):
        return func.count().filter(st.in_(statuses))

    totals = db.execute(
        base.add_columns(cnt(CONFIRMED_SET), cnt(VISIT_SET), cnt(("no_show",)), cnt(("cancelled",)))
    ).one()
    by_day = {
        r[0]: r[1:]
        for r in db.execute(
            base.add_columns(
                d, cnt(CONFIRMED_SET), cnt(VISIT_SET), cnt(("cancelled",)), cnt(("no_show",))
            ).group_by(d)
        ).all()
    }
    hour = _local_hour(Booking.start_at, Branch.timezone, dtz)
    by_hour = {
        int(h): c
        for h, c in db.execute(
            base.add_columns(hour, func.count()).where(st.in_(CONFIRMED_SET)).group_by(hour)
        ).all()
    }
    staff_rows = db.execute(
        base.add_columns(
            Booking.staff_id,
            cnt(VISIT_SET),
            func.coalesce(
                func.sum(
                    case(
                        (st.in_(OCCUPIED_SET), extract("epoch", Booking.end_at - Booking.start_at) / 60),
                        else_=0,
                    )
                ),
                0,
            ),
        ).group_by(Booking.staff_id)
    ).all()

    paid_sub = (
        select(Payment.booking_id.label("bid"), func.sum(SIGNED).label("paid"))
        .where(Payment.tenant_id == tenant_id, Payment.booking_id.is_not(None))
        .group_by(Payment.booking_id)
        .subquery()
    )
    unpaid = (
        db.scalar(
            base.add_columns(func.count())
            .outerjoin(paid_sub, paid_sub.c.bid == Booking.id)
            .where(
                st == "completed",
                Booking.total_price > 0,
                func.coalesce(paid_sub.c.paid, 0) < Booking.total_price,
            )
        )
        or 0
    )

    created_conds, _ = _booking_conds(tenant_id, f, dtz, ts_col=Booking.created_at)
    created_confirmed = (
        db.scalar(
            select(func.count())
            .select_from(Booking)
            .join(Branch, Branch.id == Booking.branch_id)
            .where(*created_conds, st.in_(CONFIRMED_SET))
        )
        or 0
    )
    return {
        "bookings": totals[0],
        "visits": totals[1],
        "no_shows": totals[2],
        "cancellations": totals[3],
        "by_day": by_day,
        "by_hour": by_hour,
        "staff": {sid: {"visits": v, "booked_min": float(mins)} for sid, v, mins in staff_rows},
        "unpaid_completed": unpaid,
        "created_confirmed": created_confirmed,
    }


def _walk_ins(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> dict[str, Any]:
    if f.service_id:
        return {"total": 0, "by_day": {}, "by_staff": {}}
    d = _local_date(Visit.visited_at, Branch.timezone, dtz)
    conds = [Visit.tenant_id == tenant_id, Visit.booking_id.is_(None), d.between(f.date_from, f.date_to)]
    if f.branch_ids is not None:
        conds.append(Visit.branch_id.in_(f.branch_ids))
    if f.staff_id:
        conds.append(Visit.staff_id == f.staff_id)
    if f.source:
        conds.append(Visit.contact_id.in_(_source_contacts(tenant_id, f)))
    base = select().select_from(Visit).join(Branch, Branch.id == Visit.branch_id).where(*conds)
    by_day = dict(db.execute(base.add_columns(d, func.count()).group_by(d)).all())
    by_staff = dict(db.execute(base.add_columns(Visit.staff_id, func.count()).group_by(Visit.staff_id)).all())
    return {"total": sum(by_day.values()), "by_day": by_day, "by_staff": by_staff}


def _leads(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> dict[str, Any]:
    d = _local_date(Lead.created_at, Branch.timezone, dtz)
    conds = [Lead.tenant_id == tenant_id, d.between(f.date_from, f.date_to)]
    if f.branch_ids is not None:
        conds.append(Lead.branch_id.in_(f.branch_ids))
    if f.service_id:
        conds.append(Lead.service_id == f.service_id)
    if f.source:
        conds.append(Lead.source == f.source)
    base = select().select_from(Lead).outerjoin(Branch, Branch.id == Lead.branch_id).where(*conds)
    converted = or_(Lead.booking_id.is_not(None), Lead.stage.in_(("booked", "visited")))
    new_leads, qualified, booked = db.execute(
        base.add_columns(
            func.count(),
            func.count(distinct(Lead.contact_id)).filter(Lead.stage.in_(QUALIFIED_STAGES)),
            func.count(distinct(Lead.contact_id)).filter(Lead.stage.in_(QUALIFIED_STAGES), converted),
        )
    ).one()
    sources = db.execute(
        base.add_columns(Lead.source, func.count()).group_by(Lead.source).order_by(func.count().desc())
    ).all()
    stages = dict(db.execute(base.add_columns(Lead.stage, func.count()).group_by(Lead.stage)).all())
    return {
        "new_leads": new_leads,
        "qualified_contacts": qualified,
        "booked_contacts": booked,
        "sources": [{"source": s, "count": c} for s, c in sources],
        "funnel": [{"stage": s, "count": stages.get(s, 0)} for s in FUNNEL_ORDER]
        + [{"stage": s, "count": c} for s, c in stages.items() if s not in FUNNEL_ORDER],
    }


def _ai(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> float:
    d = _local_date(
        func.coalesce(Conversation.last_message_at, Conversation.created_at), Branch.timezone, dtz
    )
    conds = [
        Conversation.tenant_id == tenant_id,
        d.between(f.date_from, f.date_to),
        exists().where(AIRun.conversation_id == Conversation.id),
    ]
    if f.branch_ids is not None:
        conds.append(Conversation.branch_id.in_(f.branch_ids))
    if f.source:
        conds.append(Conversation.contact_id.in_(_source_contacts(tenant_id, f)))
    human = or_(
        exists().where(Message.conversation_id == Conversation.id, Message.author == "human"),
        exists().where(AdminTask.conversation_id == Conversation.id),
    )
    total, resolved = db.execute(
        select(func.count(), func.count().filter(~human))
        .select_from(Conversation)
        .outerjoin(Branch, Branch.id == Conversation.branch_id)
        .where(*conds)
    ).one()
    return pct(resolved, total)


def _top_services(db: Session, tenant_id: uuid.UUID, f: ReportFilter, dtz: str) -> list[dict[str, Any]]:
    pconds, _ = _payment_conds(tenant_id, f, dtz)
    paid = (
        select(Payment.booking_id.label("bid"), func.sum(SIGNED).label("net"))
        .select_from(Payment)
        .outerjoin(Branch, Branch.id == Payment.branch_id)
        .where(*pconds, Payment.booking_id.is_not(None))
        .group_by(Payment.booking_id)
        .subquery()
    )
    line = BookingItem.price_snapshot - BookingItem.discount
    items = select(
        BookingItem.booking_id.label("bid"),
        BookingItem.service_id.label("sid"),
        line.label("line"),
        func.sum(line).over(partition_by=BookingItem.booking_id).label("total"),
        func.count().over(partition_by=BookingItem.booking_id).label("cnt"),
    ).subquery()
    share = case((items.c.total > 0, items.c.line / items.c.total), else_=literal(1, Numeric) / items.c.cnt)
    rev_q = select(items.c.sid, func.sum(paid.c.net * share)).join(paid, paid.c.bid == items.c.bid)
    if f.service_id:
        rev_q = rev_q.where(items.c.sid == f.service_id)
    revenue = dict(db.execute(rev_q.group_by(items.c.sid)).all())

    bconds, _ = _booking_conds(tenant_id, f, dtz)
    cnt_q = (
        select(BookingItem.service_id, func.count())
        .select_from(Booking)
        .join(Branch, Branch.id == Booking.branch_id)
        .join(BookingItem, BookingItem.booking_id == Booking.id)
        .where(*bconds, Booking.status.in_(CONFIRMED_SET))
    )
    if f.service_id:
        cnt_q = cnt_q.where(BookingItem.service_id == f.service_id)
    counts = dict(db.execute(cnt_q.group_by(BookingItem.service_id)).all())
    ids = set(revenue) | set(counts)
    titles = (
        dict(db.execute(select(Service.id, Service.title_ru).where(Service.id.in_(ids))).all()) if ids else {}
    )
    rows = [
        {
            "service_id": sid,
            "title": titles.get(sid, "—"),
            "revenue": m(revenue.get(sid, 0)),
            "count": counts.get(sid, 0),
        }
        for sid in ids
    ]
    rows.sort(key=lambda r: (Decimal(r["revenue"]), r["count"]), reverse=True)
    return rows[:10]


# ====================================================================== utilization
def _parse_t(v: Any) -> time | None:
    if v is None or isinstance(v, time):
        return v
    return time.fromisoformat(str(v))


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


def available_minutes(shifts: list[Shift], exceptions: list[ScheduleException], days: list[date]) -> int:
    """Working minutes over ``days`` from weekly shifts minus breaks, adjusted by date exceptions."""
    exc_by_day: dict[date, list[ScheduleException]] = defaultdict(list)
    for e in exceptions:
        exc_by_day[e.date].append(e)
    total = 0
    for day in days:
        todays = [
            s
            for s in shifts
            if s.weekday == day.weekday()
            and (s.valid_from is None or s.valid_from <= day)
            and (s.valid_to is None or s.valid_to >= day)
        ]
        exc = exc_by_day.get(day, [])
        if any(e.kind in ("day_off", "vacation", "sick") for e in exc):
            continue
        intervals: list[list[int]] = [[_mins(s.start_time), _mins(s.end_time)] for s in todays]
        cut: list[tuple[int, int]] = []
        for s in todays:
            for br in s.breaks or []:
                b0, b1 = _parse_t(br.get("start")), _parse_t(br.get("end"))
                if b0 and b1:
                    cut.append((_mins(b0), _mins(b1)))
        for e in exc:
            st, en = _parse_t(e.start_time), _parse_t(e.end_time)
            if e.kind == "late_start" and st and intervals:
                intervals[0][0] = max(intervals[0][0], _mins(st))
            elif e.kind == "early_leave" and en and intervals:
                intervals[-1][1] = min(intervals[-1][1], _mins(en))
            elif e.kind == "overtime" and st and en:
                if intervals:
                    intervals[0][0] = min(intervals[0][0], _mins(st))
                    intervals[-1][1] = max(intervals[-1][1], _mins(en))
                else:
                    intervals.append([_mins(st), _mins(en)])
            elif e.kind == "block" and st and en:
                cut.append((_mins(st), _mins(en)))
        for a, b in intervals:
            if b <= a:
                continue
            busy = sum(max(0, min(b, c1) - max(a, c0)) for c0, c1 in cut)
            total += max(0, b - a - busy)
    return total


def _staff_rows(
    db: Session, tenant_id: uuid.UUID, f: ReportFilter, pay: dict, book: dict, walk: dict
) -> list[dict]:
    q = select(Staff).where(Staff.tenant_id == tenant_id)
    if f.branch_ids is not None:
        q = q.where(Staff.branch_id.in_(f.branch_ids))
    if f.staff_id:
        q = q.where(Staff.id == f.staff_id)
    staff = db.scalars(q).all()
    ids = [s.id for s in staff]
    shifts: dict[uuid.UUID, list[Shift]] = defaultdict(list)
    excs: dict[uuid.UUID, list[ScheduleException]] = defaultdict(list)
    if ids:
        for s in db.scalars(select(Shift).where(Shift.staff_id.in_(ids))).all():
            shifts[s.staff_id].append(s)
        for e in db.scalars(
            select(ScheduleException).where(
                ScheduleException.staff_id.in_(ids), ScheduleException.date.between(f.date_from, f.date_to)
            )
        ).all():
            excs[e.staff_id].append(e)
    rows = []
    for s in staff:
        b = book["staff"].get(s.id, {"visits": 0, "booked_min": 0.0})
        avail = available_minutes(shifts[s.id], excs[s.id], f.days)
        rev = Decimal(pay["by_staff"].get(s.id) or 0)
        visits = b["visits"] + walk["by_staff"].get(s.id, 0)
        if not (s.active or rev or visits or b["booked_min"]):
            continue
        rows.append(
            {
                "staff_id": s.id,
                "name": s.name,
                "revenue": m(rev),
                "visits": visits,
                "utilization": pct(b["booked_min"], avail),
            }
        )
    rows.sort(key=lambda r: (Decimal(r["revenue"]), r["visits"]), reverse=True)
    return rows


# ====================================================================== public API
def overview(db: Session, tenant_id: uuid.UUID, f: ReportFilter) -> dict[str, Any]:
    dtz = default_tz_name(db, tenant_id)
    pay = _payments(db, tenant_id, f, dtz)
    book = _bookings(db, tenant_id, f, dtz)
    walk = _walk_ins(db, tenant_id, f, dtz)
    leads = _leads(db, tenant_id, f, dtz)
    task_q = (
        select(func.count())
        .select_from(AdminTask)
        .where(AdminTask.tenant_id == tenant_id, AdminTask.status.in_(("open", "acked")))
    )
    if f.branch_ids is not None:
        task_q = task_q.where(or_(AdminTask.branch_id.in_(f.branch_ids), AdminTask.branch_id.is_(None)))
    net = pay["gross"] - pay["refunds"]
    visits_total = book["visits"] + walk["total"]
    kpi = {
        "revenue": m(pay["gross"]),
        "refunds": m(pay["refunds"]),
        "net_revenue": m(net),
        "bookings": book["bookings"],
        "visits": visits_total,
        "no_shows": book["no_shows"],
        "cancellations": book["cancellations"],
        "new_leads": leads["new_leads"],
        "conversion_to_booking": pct(leads["booked_contacts"], leads["qualified_contacts"]),
        "conversion_to_visit": pct(book["visits"], book["bookings"]),
        "no_show_rate": pct(book["no_shows"], book["bookings"]),
        "avg_check": m(pay["avg_check"]),
        "ai_resolution_rate": _ai(db, tenant_id, f, dtz),
        "open_tasks": db.scalar(task_q) or 0,
        "unpaid_completed": book["unpaid_completed"],
    }
    return {
        "kpi": kpi,
        "revenue_by_day": [{"date": d.isoformat(), "amount": m(pay["by_day"].get(d, 0))} for d in f.days],
        "bookings_by_day": [
            {
                "date": d.isoformat(),
                "bookings": book["by_day"].get(d, (0, 0, 0, 0))[0],
                "visits": book["by_day"].get(d, (0, 0, 0, 0))[1] + walk["by_day"].get(d, 0),
            }
            for d in f.days
        ],
        "top_services": _top_services(db, tenant_id, f, dtz),
        "top_staff": _staff_rows(db, tenant_id, f, pay, book, walk)[:10],
        "lead_sources": leads["sources"],
        "funnel": leads["funnel"],
        "load_by_hour": [{"hour": h, "bookings": book["by_hour"].get(h, 0)} for h in range(24)],
        "cancellations_by_day": [
            {
                "date": d.isoformat(),
                "cancelled": book["by_day"].get(d, (0, 0, 0, 0))[2],
                "no_show": book["by_day"].get(d, (0, 0, 0, 0))[3],
            }
            for d in f.days
        ],
    }


def mask_finance(report: dict[str, Any]) -> dict[str, Any]:
    """For roles without finance.read: money values are hidden (null)."""
    for k in MONEY_KPIS:
        report["kpi"][k] = None
    for row in report["revenue_by_day"]:
        row["amount"] = None
    for row in report["top_services"] + report["top_staff"]:
        row["revenue"] = None
    return report


# ====================================================================== export
KPI_LABELS = {
    "revenue": "Выручка",
    "refunds": "Возвраты",
    "net_revenue": "Чистая выручка",
    "bookings": "Записи",
    "visits": "Визиты",
    "no_shows": "Неявки",
    "cancellations": "Отмены",
    "new_leads": "Новые лиды",
    "conversion_to_booking": "Конверсия в запись, %",
    "conversion_to_visit": "Конверсия в визит, %",
    "no_show_rate": "No-show, %",
    "avg_check": "Средний чек",
    "ai_resolution_rate": "AI без человека, %",
    "open_tasks": "Открытые вопросы",
    "unpaid_completed": "Неоплаченные визиты",
}


def _tables(report: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]]]]:
    days = []
    for rv, bk, cn in zip(
        report["revenue_by_day"], report["bookings_by_day"], report["cancellations_by_day"], strict=True
    ):
        days.append([rv["date"], rv["amount"], bk["bookings"], bk["visits"], cn["cancelled"], cn["no_show"]])
    return [
        ("KPI", ["Показатель", "Значение"], [[KPI_LABELS.get(k, k), v] for k, v in report["kpi"].items()]),
        ("По дням", ["Дата", "Выручка (нетто)", "Записи", "Визиты", "Отмены", "Неявки"], days),
        (
            "Услуги",
            ["Услуга", "Выручка", "Кол-во"],
            [[r["title"], r["revenue"], r["count"]] for r in report["top_services"]],
        ),
        (
            "Мастера",
            ["Мастер", "Выручка", "Визиты", "Загрузка, %"],
            [[r["name"], r["revenue"], r["visits"], r["utilization"]] for r in report["top_staff"]],
        ),
        ("Источники", ["Источник", "Лиды"], [[r["source"], r["count"]] for r in report["lead_sources"]]),
        ("Воронка", ["Этап", "Лиды"], [[r["stage"], r["count"]] for r in report["funnel"]]),
    ]


def export_csv(report: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, (title, header, rows) in enumerate(_tables(report)):
        if i:
            w.writerow([])
        w.writerow([f"# {title}"])
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM: Excel opens Cyrillic correctly


def export_xlsx(report: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)
    for title, header, rows in _tables(report):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([_xl(v) for v in r])
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(
                12, min(40, max(len(str(c.value or "")) for c in col) + 2)
            )
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _xl(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return float(Decimal(v)) if v and v.replace(".", "", 1).lstrip("-").isdigit() else v
        except ArithmeticError:
            return v
    return v


def tenant_currency(db: Session, tenant_id: uuid.UUID) -> str:
    return db.get(Tenant, tenant_id).currency or "KZT"


def local_now(db: Session, tenant_id: uuid.UUID, now: datetime | None = None) -> datetime:
    return (now or utcnow()).astimezone(tz(default_tz_name(db, tenant_id)))
