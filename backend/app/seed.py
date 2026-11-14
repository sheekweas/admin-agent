"""Demo data: ``python -m app.seed`` creates the tenant "Barber KZ" (idempotent: skipped if it exists).

Logins (password ``demo12345``): owner@demo.kz, admin@demo.kz, backup@demo.kz, barber@demo.kz, analyst@demo.kz.
Also creates payments for most completed visits (one partial refund) and published RU/KK knowledge articles.

Bookings are inserted directly (not via the booking engine) because most of them lie in the past; the
generator still guarantees they respect shifts, breaks and the DB exclusion constraints. No outbox events are
emitted, so seeding never triggers Telegram/WhatsApp deliveries.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Booking,
    BookingItem,
    Branch,
    Contact,
    ContactChannel,
    KnowledgeArticle,
    Lead,
    Membership,
    Payment,
    Resource,
    Service,
    Shift,
    Staff,
    StaffService,
    Tenant,
    User,
    Visit,
)
from app.security import hash_password
from app.services.audit import audit
from app.services.common import next_number, tz, utcnow

TENANT_NAME = "Barber KZ"
PASSWORD = "demo12345"
TIMEZONE = "Asia/Almaty"  # Almaty and Astana share UTC+5
BREAK = {"start": "14:00", "end": "15:00"}

# (title_ru, title_kk, category, duration_min, price KZT, buffer_after_min, resource_type)
SERVICES = [
    ("Мужская стрижка", "Ерлер шаш қиюы", "Стрижки", 60, 5000, 0, None),
    ("Стрижка бороды", "Сақал қию", "Борода", 30, 3000, 0, None),
    ("Стрижка + борода", "Шаш қию + сақал", "Комплексы", 90, 7500, 0, None),
    ("Детская стрижка", "Балалар шаш қиюы", "Стрижки", 40, 3500, 0, None),
    ("Королевское бритьё", "Корольдік қырыну", "Борода", 45, 4500, 0, None),
    ("Отец + сын", "Әке + бала", "Комплексы", 90, 8000, 0, None),
    ("Укладка", "Сән беру", "Стрижки", 20, 2000, 0, None),
    ("Камуфляж седины", "Ақ шашты бояу", "Окрашивание", 45, 6000, 15, "wash_chair"),
]

# (name, branch index, specialization, color, works on Sunday)
BARBERS = [
    ("Арман", 0, "Топ-барбер", "#2563eb", False),
    ("Ерлан", 0, "Барбер", "#16a34a", True),
    ("Данияр", 0, "Барбер-колорист", "#9333ea", False),
    ("Бауыржан", 1, "Топ-барбер", "#ea580c", True),
    ("Тимур", 1, "Барбер", "#0891b2", False),
]

FIRST_NAMES = [
    "Нурлан",
    "Айдос",
    "Асхат",
    "Ерболат",
    "Дамир",
    "Санжар",
    "Алихан",
    "Мадияр",
    "Руслан",
    "Азамат",
    "Женис",
    "Ильяс",
    "Олжас",
    "Темирлан",
    "Бекзат",
    "Канат",
    "Максим",
    "Дмитрий",
    "Артём",
    "Сергей",
    "Абылай",
    "Нурсултан",
    "Айбек",
    "Ержан",
    "Марат",
    "Талгат",
    "Арсен",
    "Даурен",
    "Ислам",
    "Жандос",
    "Алмас",
    "Медет",
    "Нуржан",
    "Рустем",
    "Виктор",
    "Тимофей",
    "Саят",
    "Батыр",
    "Ринат",
    "Ерасыл",
]
SOURCES = ["whatsapp"] * 5 + ["ads"] * 3 + ["qr"] * 2 + ["site", "repeat", "call", "manual"]


@dataclass
class DemoData:
    """Everything the seed created; handed to extension hooks."""

    tenant: Tenant
    branches: list[Branch]
    users: dict[str, User]
    staff: list[Staff]
    services: list[Service]
    contacts: list[Contact]
    bookings: list[Booking] = field(default_factory=list)


# extension point: other modules register ``fn(db, demo)`` here (e.g. payments, knowledge articles).
EXTENSIONS: list[Callable[[Session, DemoData], None]] = []


def seed_extensions(db: Session, demo: DemoData) -> None:
    """# extension point — runs registered hooks after the core demo data is in place."""
    for hook in EXTENSIONS:
        hook(db, demo)


# ---------------------------------------------------------------- builders
def _user(db: Session, tenant: Tenant, email: str, full_name: str, role: str, **flags) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            full_name=full_name,
            password_hash=hash_password(PASSWORD),
            phone_e164=flags.pop("phone", None),
            telegram_chat_id=flags.pop("tg", None),
        )
        db.add(user)
        db.flush()
    db.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role, **flags))
    return user


def _catalogue(
    db: Session, tenant: Tenant
) -> tuple[list[Branch], list[Service], list[Staff], dict[str, User]]:
    branches = [
        Branch(
            tenant_id=tenant.id,
            name="Barber KZ Алматы",
            address="Алматы, пр. Абая 52",
            timezone=TIMEZONE,
            contacts={"phone": "+77272000001", "instagram": "@barberkz.almaty"},
        ),
        Branch(
            tenant_id=tenant.id,
            name="Barber KZ Астана",
            address="Астана, ул. Кенесары 40",
            timezone=TIMEZONE,
            contacts={"phone": "+77172000002", "instagram": "@barberkz.astana"},
        ),
    ]
    db.add_all(branches)
    db.flush()
    users = {
        # Demo Telegram chat ids are placeholders: in TELEGRAM_MODE=mock they are only recorded.
        "owner": _user(
            db, tenant, "owner@demo.kz", "Асет Owner", "owner", phone="+77010000100", tg="100000100"
        ),
        "admin": _user(
            db,
            tenant,
            "admin@demo.kz",
            "Айгерим Admin",
            "admin",
            is_escalation_primary=True,
            phone="+77010000101",
            tg="100000101",
        ),
        "backup": _user(
            db,
            tenant,
            "backup@demo.kz",
            "Мадина Backup",
            "admin",
            is_escalation_backup=True,
            phone="+77010000102",
            tg="100000102",
        ),
        "barber": _user(db, tenant, "barber@demo.kz", "Арман Barber", "staff"),
        "analyst": _user(db, tenant, "analyst@demo.kz", "Дана Analyst", "analyst"),
    }
    db.add(Resource(tenant_id=tenant.id, branch_id=branches[0].id, type="wash_chair", name="Мойка-кресло №1"))

    services = [
        Service(
            tenant_id=tenant.id,
            title_ru=ru,
            title_kk=kk,
            category=cat,
            duration_min=dur,
            price=Decimal(price),
            buffer_after_min=buf,
            resource_type=res,
            description=f"{ru} в Barber KZ",
        )
        for ru, kk, cat, dur, price, buf, res in SERVICES
    ]
    db.add_all(services)
    staff = []
    for name, branch_idx, spec, color, sunday in BARBERS:
        member = Staff(
            tenant_id=tenant.id,
            branch_id=branches[branch_idx].id,
            name=name,
            specialization=spec,
            color=color,
            user_id=users["barber"].id if name == "Арман" else None,
        )
        db.add(member)
        db.flush()
        staff.append(member)
        for weekday in range(7 if sunday else 6):
            db.add(
                Shift(
                    tenant_id=tenant.id,
                    staff_id=member.id,
                    weekday=weekday,
                    start_time=time(10),
                    end_time=time(21),
                    breaks=[BREAK],
                )
            )
    db.flush()
    for member in staff:
        for service in services:
            if service.resource_type and member.branch_id != branches[0].id:
                continue  # only Almaty has the wash chair
            override = 75 if member.specialization == "Топ-барбер" and service.duration_min == 60 else None
            db.add(StaffService(staff_id=member.id, service_id=service.id, duration_min=override))
    db.flush()
    return branches, services, staff, users


def _contacts(db: Session, tenant: Tenant, branches: list[Branch], rng: random.Random) -> list[Contact]:
    contacts = []
    for i, name in enumerate(FIRST_NAMES):
        phone = f"+7701{7000000 + i * 7919:07d}"
        contact = Contact(
            tenant_id=tenant.id,
            branch_id=branches[i % 2].id,
            phone_e164=phone,
            name=name,
            language="kk" if i % 3 == 0 else "ru",
            source=rng.choice(SOURCES),
            consent_flags={"processing": True, "marketing": i % 4 != 0},
            opted_out=i % 13 == 12,
        )
        db.add(contact)
        db.flush()
        db.add(
            ContactChannel(
                tenant_id=tenant.id, contact_id=contact.id, channel="whatsapp", external_id=phone.lstrip("+")
            )
        )
        contacts.append(contact)
    return contacts


class _Calendar:
    """Tracks occupied intervals so generated bookings never violate the exclusion constraints."""

    def __init__(self) -> None:
        self.busy: dict[uuid.UUID, list[tuple[datetime, datetime]]] = defaultdict(list)

    def free(self, key: uuid.UUID, start: datetime, end: datetime) -> bool:
        return all(end <= s or start >= e for s, e in self.busy[key])

    def take(self, key: uuid.UUID, start: datetime, end: datetime) -> None:
        self.busy[key].append((start, end))


def _status(start: datetime, now: datetime, rng: random.Random) -> str:
    roll = rng.random()
    if start < now:
        return "completed" if roll < 0.72 else "no_show" if roll < 0.82 else "cancelled"
    return "confirmed" if roll < 0.75 else "pending" if roll < 0.87 else "cancelled"


LEAD_STAGE_FOR = {
    "completed": "visited",
    "no_show": "lost",
    "cancelled": "cancelled",
    "confirmed": "booked",
    "pending": "booking_pending",
}


def _bookings(db: Session, demo: DemoData, rng: random.Random, now: datetime) -> None:
    zone = tz(TIMEZONE)
    today = now.astimezone(zone).date()
    links = defaultdict(set)
    for link in db.scalars(select(StaffService).where(StaffService.staff_id.in_([s.id for s in demo.staff]))):
        links[link.staff_id].add(link.service_id)
    durations = {
        (link.staff_id, link.service_id): link.duration_min
        for link in db.scalars(select(StaffService).where(StaffService.duration_min.is_not(None)))
    }
    chair = db.scalar(select(Resource).where(Resource.tenant_id == demo.tenant.id))
    calendar = _Calendar()
    weights = [30, 14, 18, 8, 6, 4, 8, 6]

    for offset in range(-30, 15):
        day: date = today + timedelta(days=offset)
        for _ in range(rng.randint(2, 4)):
            for _attempt in range(20):
                member = rng.choice(demo.staff)
                service = rng.choices(demo.services, weights=weights)[0]
                if service.id not in links[member.id]:
                    continue
                works = day.weekday() < 6 or any(b[0] == member.name and b[4] for b in BARBERS)
                minutes = durations.get((member.id, service.id)) or service.duration_min
                local = datetime.combine(day, time(10), tzinfo=zone) + timedelta(
                    minutes=30 * rng.randint(0, 21)
                )
                end = local + timedelta(minutes=minutes)
                occupied_end = end + timedelta(minutes=service.buffer_after_min)
                lunch = (
                    datetime.combine(day, time(14), tzinfo=zone),
                    datetime.combine(day, time(15), tzinfo=zone),
                )
                closing = datetime.combine(day, time(21), tzinfo=zone)
                if not works or occupied_end > closing or (local < lunch[1] and occupied_end > lunch[0]):
                    continue
                start_utc, end_utc, occ_utc = (x.astimezone(UTC) for x in (local, end, occupied_end))
                if not calendar.free(member.id, start_utc, occ_utc):
                    continue
                use_chair = service.resource_type is not None
                if use_chair and not calendar.free(chair.id, start_utc, occ_utc):
                    continue
                _insert(
                    db,
                    demo,
                    rng,
                    now,
                    member,
                    service,
                    chair if use_chair else None,
                    start_utc,
                    end_utc,
                    occ_utc,
                    calendar,
                )
                break


def _insert(db, demo, rng, now, member, service, chair, start, end, occupied_end, calendar) -> None:
    status = _status(start, now, rng)
    contact = rng.choice([c for c in demo.contacts if c.branch_id == member.branch_id])
    created = min(start - timedelta(days=rng.randint(0, 5), hours=rng.randint(1, 8)), now)
    booking = Booking(
        tenant_id=demo.tenant.id,
        number=next_number(db, demo.tenant.id, "booking"),
        branch_id=member.branch_id,
        contact_id=contact.id,
        staff_id=member.id,
        resource_id=chair.id if chair else None,
        start_at=start,
        end_at=end,
        occupied_start=start,
        occupied_end=occupied_end,
        status=status,
        channel=rng.choice(["whatsapp", "whatsapp", "whatsapp", "admin", "phone"]),
        total_price=service.price,
        cancel_reason="клиент передумал" if status == "cancelled" else "",
        created_at=created,
    )
    booking.source = "ai_agent" if booking.channel == "whatsapp" else "admin"
    booking.items.append(
        BookingItem(
            service_id=service.id,
            price_snapshot=service.price,
            duration_snapshot=int((end - start).total_seconds() // 60),
        )
    )
    db.add(booking)
    db.flush()
    if status not in ("cancelled", "no_show"):
        calendar.take(member.id, start, occupied_end)
        if chair:
            calendar.take(chair.id, start, occupied_end)
    if status == "completed":
        db.add(
            Visit(
                tenant_id=demo.tenant.id,
                booking_id=booking.id,
                contact_id=contact.id,
                staff_id=member.id,
                branch_id=member.branch_id,
                visited_at=start,
                amount_due=booking.total_price,
            )
        )
    stage = LEAD_STAGE_FOR[status]
    db.add(
        Lead(
            tenant_id=demo.tenant.id,
            contact_id=contact.id,
            branch_id=member.branch_id,
            service_id=service.id,
            source=contact.source,
            stage=stage,
            booking_id=booking.id,
            created_at=created,
            lost_reason="не пришёл" if status == "no_show" else booking.cancel_reason,
            closed_at=start if stage in ("visited", "lost", "cancelled") else None,
        )
    )
    if status == "no_show" and "не пришёл" not in contact.tags:
        contact.tags = [*contact.tags, "не пришёл"]
    contact.last_contact_at = max(contact.last_contact_at or created, created)
    demo.bookings.append(booking)


def _open_leads(db: Session, demo: DemoData, rng: random.Random, now: datetime) -> None:
    """Leads without bookings so every funnel stage is represented."""
    plan = [
        ("new", ""),
        ("new", ""),
        ("info_requested", ""),
        ("info_requested", ""),
        ("qualified", ""),
        ("qualified", ""),
        ("lost", "дорого"),
        ("lost", "не ответил"),
        ("repeat", ""),
    ]
    tags = {
        "info_requested": ["спросил цену"],
        "qualified": ["хочет сегодня"],
        "lost": ["нет свободного слота"],
    }
    for stage, reason in plan:
        contact = rng.choice(demo.contacts)
        created = now - timedelta(days=rng.randint(0, 20), hours=rng.randint(0, 20))
        db.add(
            Lead(
                tenant_id=demo.tenant.id,
                contact_id=contact.id,
                branch_id=contact.branch_id,
                source=contact.source,
                stage=stage,
                lost_reason=reason,
                tags=tags.get(stage, []),
                created_at=created,
                closed_at=created if stage == "lost" else None,
            )
        )
        contact.tags = list(dict.fromkeys([*contact.tags, *tags.get(stage, [])]))
    for contact in demo.contacts:
        visits = sum(1 for b in demo.bookings if b.contact_id == contact.id and b.status == "completed")
        if visits > 1:
            contact.tags = list(dict.fromkeys([*contact.tags, "повторный клиент"]))


def _demo_payments(db: Session, demo: DemoData) -> None:
    """Completed visits are mostly paid (some unpaid, one refunded) so finance reports are meaningful."""
    rng = random.Random(7)
    methods = ["cash", "card", "card", "transfer", "online"]
    for b in demo.bookings:
        if b.status != "completed" or rng.random() < 0.1:  # ~10% stay unpaid -> "unpaid completed" KPI
            continue
        db.add(
            Payment(
                tenant_id=demo.tenant.id,
                booking_id=b.id,
                contact_id=b.contact_id,
                branch_id=b.branch_id,
                staff_id=b.staff_id,
                kind="payment",
                amount=b.total_price,
                method=rng.choice(methods),
                status="confirmed",
                confirmation_source="admin",
                paid_at=b.end_at,
                created_by=demo.users["admin"].id,
            )
        )
    db.flush()
    paid = db.scalars(select(Payment).where(Payment.tenant_id == demo.tenant.id).limit(1)).first()
    if paid is not None:
        db.add(
            Payment(
                tenant_id=demo.tenant.id,
                booking_id=paid.booking_id,
                contact_id=paid.contact_id,
                branch_id=paid.branch_id,
                staff_id=paid.staff_id,
                kind="refund",
                amount=Decimal("1000.00"),
                method=paid.method,
                status="confirmed",
                confirmation_source="admin",
                paid_at=paid.paid_at + timedelta(days=1),
                comment="Частичный возврат: демо",
                refund_of_id=paid.id,
                created_by=demo.users["owner"].id,
            )
        )


_KNOWLEDGE = [
    (
        "company",
        "ru",
        "О барбершопе",
        "Barber KZ — сеть барбершопов в Алматы и Астане. Мужские и детские стрижки, "
        "оформление бороды, бритьё. Работаем ежедневно с 10:00 до 21:00.",
    ),
    (
        "company",
        "kk",
        "Барбершоп туралы",
        "Barber KZ — Алматы мен Астанадағы барбершоптар желісі. Күн сайын "
        "10:00-ден 21:00-ге дейін жұмыс істейміз.",
    ),
    (
        "rules",
        "ru",
        "Перенос и отмена",
        "Перенести или отменить запись можно не позднее чем за 3 часа до визита. "
        "Опоздание более 15 минут может привести к переносу записи.",
    ),
    ("promo", "ru", "Акции", "По вторникам скидка 10% на мужскую стрижку. Других скидок сейчас нет."),
    ("faq", "ru", "Оплата", "Принимаем наличные, карты и Kaspi-переводы. Предоплата не требуется."),
    (
        "faq",
        "kk",
        "Төлем",
        "Қолма-қол ақша, карта және Kaspi аударымы қабылданады. Алдын ала төлем қажет емес.",
    ),
    ("faq", "ru", "Парковка", "У филиала в Алматы есть бесплатная парковка во дворе."),
]


def _demo_knowledge(db: Session, demo: DemoData) -> None:
    for section, lang, title, content in _KNOWLEDGE:
        db.add(
            KnowledgeArticle(
                tenant_id=demo.tenant.id,
                section=section,
                language=lang,
                title=title,
                content=content,
                status="published",
                author_id=demo.users["owner"].id,
            )
        )
    db.add(
        KnowledgeArticle(
            tenant_id=demo.tenant.id,
            section="services",
            language="ru",
            title="Окрашивание бороды (черновик)",
            content="Новая услуга, цена уточняется.",
            status="draft",
            author_id=demo.users["admin"].id,
        )
    )


EXTENSIONS.extend([_demo_payments, _demo_knowledge])


def seed(db: Session, *, now: datetime | None = None) -> DemoData | None:
    """Create the demo tenant in the caller's transaction. Returns None if it already exists."""
    if db.scalar(select(Tenant.id).where(Tenant.name == TENANT_NAME)):
        return None
    now = now or utcnow()
    rng = random.Random(20261114)
    tenant = Tenant(
        name=TENANT_NAME,
        niche="barbershop",
        default_language="ru",
        currency="KZT",
        settings={"slot_step_min": 15, "cancel_deadline_hours": 3},
    )
    db.add(tenant)
    db.flush()
    branches, services, staff, users = _catalogue(db, tenant)
    demo = DemoData(
        tenant=tenant,
        branches=branches,
        users=users,
        staff=staff,
        services=services,
        contacts=_contacts(db, tenant, branches, rng),
    )
    _bookings(db, demo, rng, now)
    _open_leads(db, demo, rng, now)
    seed_extensions(db, demo)
    audit(
        db,
        tenant.id,
        "system",
        None,
        "tenant.seed",
        "tenant",
        str(tenant.id),
        {"bookings": len(demo.bookings), "contacts": len(demo.contacts)},
    )
    db.flush()
    return demo


def main() -> None:
    with SessionLocal() as db:
        demo = seed(db)
        if demo is None:
            print(f"Tenant {TENANT_NAME!r} already exists - nothing to do.")
            return
        db.commit()
        print(
            f"Seeded {TENANT_NAME!r}: {len(demo.branches)} branches, {len(demo.staff)} barbers, "
            f"{len(demo.services)} services, {len(demo.contacts)} contacts, {len(demo.bookings)} bookings."
        )
        print(
            f"Logins (password {PASSWORD!r}): owner@demo.kz, admin@demo.kz, backup@demo.kz, barber@demo.kz, "
            "analyst@demo.kz"
        )


if __name__ == "__main__":
    main()
