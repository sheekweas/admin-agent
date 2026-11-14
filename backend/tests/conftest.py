"""Test fixtures. Requires PostgreSQL (exclusion constraints): TEST_DATABASE_URL or default local docker DB.

docker compose up -d postgres redis
cd backend && pytest
"""

from __future__ import annotations

import os
import uuid
from datetime import time
from decimal import Decimal

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://crm:crm@localhost:5432/crm_test"),
)
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("WHATSAPP_MODE", "mock")
os.environ.setdefault("TELEGRAM_MODE", "mock")
os.environ.setdefault("VOICE_PROVIDER", "mock")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db import Base, SessionLocal, engine
from app.models import (
    Branch,
    Contact,
    Membership,
    Service,
    Shift,
    Staff,
    StaffService,
    Tenant,
    User,
)
from app.security import create_access_token, hash_password


def _ensure_database() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname=:n"), {"n": url.database}):
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    _ensure_database()
    from importlib.util import module_from_spec, spec_from_file_location

    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "0001_initial.py")
    spec = spec_from_file_location("mig0001", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        Base.metadata.create_all(conn)
        for sql in module.CONSTRAINTS_SQL:
            conn.execute(text(sql))
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


class World:
    """A ready-made barbershop: 1 tenant, 1 branch, owner/admin/staff users, 2 barbers, 2 services, shifts."""

    def __init__(self, db):
        self.db = db
        self.tenant = Tenant(name="Test Barbershop", niche="barbershop", default_language="ru")
        db.add(self.tenant)
        db.flush()
        self.branch = Branch(
            tenant_id=self.tenant.id, name="Центральный", address="Алматы, Абая 1", timezone="Asia/Almaty"
        )
        db.add(self.branch)
        db.flush()
        self.owner = self._user("owner@test.kz", "owner")
        self.admin = self._user(
            "admin@test.kz", "admin", escalation_primary=True, phone="+77010000001", tg="1001"
        )
        self.backup_admin = self._user(
            "backup@test.kz", "admin", escalation_backup=True, phone="+77010000002", tg="1002"
        )
        self.staff_user = self._user("barber@test.kz", "staff")
        self.analyst = self._user("analyst@test.kz", "analyst")
        self.barber1 = Staff(
            tenant_id=self.tenant.id, branch_id=self.branch.id, name="Арман", user_id=self.staff_user.id
        )
        self.barber2 = Staff(tenant_id=self.tenant.id, branch_id=self.branch.id, name="Ерлан")
        db.add_all([self.barber1, self.barber2])
        db.flush()
        self.haircut = Service(
            tenant_id=self.tenant.id,
            title_ru="Мужская стрижка",
            title_kk="Ер адамның шаш қиюы",
            duration_min=60,
            price=Decimal(5000),
            buffer_after_min=0,
        )
        self.beard = Service(
            tenant_id=self.tenant.id,
            title_ru="Борода",
            title_kk="Сақал",
            duration_min=30,
            price=Decimal(3000),
        )
        db.add_all([self.haircut, self.beard])
        db.flush()
        for st in (self.barber1, self.barber2):
            for sv in (self.haircut, self.beard):
                db.add(StaffService(staff_id=st.id, service_id=sv.id))
            for wd in range(7):
                db.add(
                    Shift(
                        tenant_id=self.tenant.id,
                        staff_id=st.id,
                        weekday=wd,
                        start_time=time(10, 0),
                        end_time=time(21, 0),
                        breaks=[{"start": "14:00", "end": "15:00"}],
                    )
                )
        self.contact = Contact(
            tenant_id=self.tenant.id,
            phone_e164="+77071234567",
            name="Нурлан",
            language="ru",
            consent_flags={"processing": True},
        )
        db.add(self.contact)
        db.commit()

    def _user(self, email, role, escalation_primary=False, escalation_backup=False, phone=None, tg=None):
        u = User(
            email=email,
            full_name=email.split("@")[0],
            password_hash=hash_password("password123"),
            phone_e164=phone,
            telegram_chat_id=tg,
        )
        self.db.add(u)
        self.db.flush()
        self.db.add(
            Membership(
                tenant_id=self.tenant.id,
                user_id=u.id,
                role=role,
                is_escalation_primary=escalation_primary,
                is_escalation_backup=escalation_backup,
            )
        )
        return u

    def headers(self, user=None) -> dict:
        user = user or self.owner
        m = self.db.query(Membership).filter_by(user_id=user.id, tenant_id=self.tenant.id).one()
        return {"Authorization": "Bearer " + create_access_token(str(user.id), str(self.tenant.id), m.role)}


@pytest.fixture
def world(db) -> World:
    return World(db)


@pytest.fixture
def other_world(db) -> World:
    """Second, isolated tenant (for tenant-isolation tests)."""
    w = World.__new__(World)
    w.db = db
    w.tenant = Tenant(name="Other Salon", niche="beauty")
    db.add(w.tenant)
    db.flush()
    w.branch = Branch(tenant_id=w.tenant.id, name="Other", timezone="Asia/Almaty")
    db.add(w.branch)
    db.flush()
    w.owner = w._user(f"owner-{uuid.uuid4().hex[:6]}@other.kz", "owner")
    db.commit()
    return w
