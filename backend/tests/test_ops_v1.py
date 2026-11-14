"""v1.0 operations: /metrics, platform AI kill switch, `python -m app.cli create-tenant`."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.llm import LLMRequest, LLMResult, LLMUnavailable, set_llm
from app.cli import CliError, create_tenant, main
from app.config import Settings, get_settings, production_problems
from app.models import (
    AdminTask,
    AIRun,
    AuditLog,
    Branch,
    Conversation,
    Membership,
    Notification,
    Tenant,
    User,
)
from app.security import verify_password
from tests.ai_helpers import reply_text, say
from tests.voice_helpers import add_voice_integration, call_by_sid, inbound, no_quiet_hours


@pytest.fixture(autouse=True)
def _reset_llm():
    yield
    set_llm(None)


def _client(host: str) -> TestClient:
    from app.main import app

    return TestClient(app, client=(host, 50000))


def _samples(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            out[key] = float(value)
    return out


# ---------------------------------------------------------------- /metrics
@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "172.18.0.5", "192.168.1.10"])
def test_metrics_served_to_internal_network(host):
    r = _client(host).get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "crm_db_up 1.0" in r.text


@pytest.mark.parametrize("host", ["8.8.8.8", "testclient"])
def test_metrics_refused_from_public_clients(host):
    r = _client(host).get("/metrics")
    assert r.status_code == 403 and "crm_" not in r.text


def test_metrics_token_required_when_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", "t" * 32)
    internal = _client("10.0.0.2")
    assert internal.get("/metrics").status_code == 401  # a token disables the network-based allowance
    assert internal.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = _client("8.8.8.8").get("/metrics", headers={"Authorization": "Bearer " + "t" * 32})
    assert ok.status_code == 200 and "crm_http_requests_total" in ok.text


def test_metrics_can_be_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_enabled", False)
    assert _client("127.0.0.1").get("/metrics").status_code == 404


def test_metrics_not_in_openapi(client):
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_http_metrics_use_route_templates_not_raw_paths(world):
    c = _client("127.0.0.1")
    for _ in range(2):
        c.get(f"/v1/bookings/{world.contact.id}")  # 401, no token
    c.get("/v1/no-such-endpoint-123")
    s = _samples(c.get("/metrics").text)
    assert s['crm_http_requests_total{method="GET",route="/v1/bookings/{booking_id}",status="401"}'] >= 2
    assert s['crm_http_requests_total{method="GET",route="unmatched",status="404"}'] >= 1
    c.get("/openapi.json")
    s = _samples(c.get("/metrics").text)
    assert s['crm_http_requests_total{method="GET",route="/openapi.json",status="200"}'] >= 1
    assert not any(str(world.contact.id) in k or "no-such-endpoint" in k for k in s)
    assert s['crm_http_request_duration_seconds_count{method="GET",route="/v1/bookings/{booking_id}"}'] >= 2


class _Broken:
    provider, model = "test", "broken"

    def run(self, req: LLMRequest, execute) -> LLMResult:
        raise LLMUnavailable("connection refused")


def test_operational_gauges_reflect_backlog(client, world, db):
    set_llm(_Broken())
    say(client, world, "Хочу записаться")  # failed AI turn -> fallback + open admin task + outbox events
    db.add(
        Notification(tenant_id=world.tenant.id, recipient="1001", channel="telegram", kind="x", status="dead")
    )
    db.commit()
    s = _samples(_client("127.0.0.1").get("/metrics").text)
    assert s["crm_db_up"] == 1.0
    assert s["crm_ai_run_errors_last_hour"] == 1.0 and s["crm_ai_runs_last_hour"] == 1.0
    assert s['crm_admin_tasks{status="open"}'] == 1.0
    assert s['crm_notifications{status="dead"}'] == 1.0
    assert s['crm_outbox_events{status="pending"}'] >= 1.0
    assert s["crm_outbox_oldest_pending_age_seconds"] >= 0.0
    assert s["crm_ai_globally_disabled"] == 0.0


def test_metrics_survive_database_outage(monkeypatch):
    import app.db

    class _Down:
        def __call__(self):
            raise ConnectionError("db down")

    monkeypatch.setattr(app.db, "SessionLocal", _Down())
    r = _client("127.0.0.1").get("/metrics")
    assert r.status_code == 200 and "crm_db_up 0.0" in r.text


def test_short_metrics_token_is_a_production_problem():
    base = {"env": "production", "jwt_secret": "x" * 40, "telegram_webhook_secret": "y" * 32}
    assert any("METRICS_TOKEN" in p for p in production_problems(Settings(**base, metrics_token="short")))
    assert production_problems(Settings(**base, metrics_token="z" * 32)) == []
    assert production_problems(Settings(**base, metrics_token="")) == []


# ---------------------------------------------------------------- AI kill switch (ТЗ §23.1)
class _MustNotRun:
    provider, model = "test", "must-not-run"

    def run(self, req: LLMRequest, execute) -> LLMResult:  # pragma: no cover - the point of the test
        raise AssertionError("LLM called while AI_GLOBALLY_DISABLED=true")


def test_global_kill_switch_sends_fallback_and_routes_to_humans(client, world, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_globally_disabled", True)
    set_llm(_MustNotRun())
    r = say(client, world, "Хочу записаться на завтра")
    assert reply_text(r) == "Спасибо за сообщение! Администратор скоро ответит вам."
    assert r["replies"][0]["author"] == "system"
    task = db.scalar(select(AdminTask))
    assert task.priority == "high" and task.payload["source"] == "ai_fallback"
    assert "AI_GLOBALLY_DISABLED" in task.payload["reason"]
    run = db.scalar(select(AIRun))
    assert run.provider == "disabled" and "AI_GLOBALLY_DISABLED" in run.error
    conv = db.scalar(select(Conversation))
    db.refresh(conv)
    assert conv.mode == "waiting_admin"
    assert say(client, world, "Алло?")["replies"] == []  # no repeated fallback / tasks
    assert db.scalar(select(func.count(AdminTask.id))) == 1
    assert "crm_ai_globally_disabled 1.0" in _client("127.0.0.1").get("/metrics").text


def test_global_kill_switch_off_by_default_ai_answers(client, world):
    assert get_settings().ai_globally_disabled is False
    r = say(client, world, "Здравствуйте")
    assert r["replies"] and r["replies"][0]["author"] == "ai"


def test_global_kill_switch_blocks_knowledge_sandbox(client, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_globally_disabled", True)
    set_llm(_MustNotRun())
    r = client.post("/v1/knowledge/test", headers=world.headers(), json={"question": "Есть парковка?"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "AI_UNAVAILABLE"


def test_global_kill_switch_transfers_voice_calls_to_admin(client, world, db, monkeypatch):
    add_voice_integration(db, world.tenant)
    no_quiet_hours(db, world.tenant)
    monkeypatch.setattr(get_settings(), "ai_globally_disabled", True)
    set_llm(_MustNotRun())
    r, sid = inbound(client)
    assert r.status_code == 200 and "<Dial" in r.text and "+77010000001" in r.text
    call = call_by_sid(db, sid)
    assert call.metrics["transferred_to_human"] is True


# ---------------------------------------------------------------- create-tenant CLI
def test_create_tenant_creates_owner_branch_and_audit(db):
    res = create_tenant(
        db,
        name="Barber Pro",
        owner_email="Owner@BarberPro.kz",
        owner_name="Айдар",
        owner_password="s3cret-pass-123",
        branch_name="Barber Pro Алматы",
        branch_address="Алматы, Абая 10",
        language="kk",
    )
    db.commit()
    assert res.created and res.owner_password is None
    tenant = db.get(Tenant, res.tenant_id)
    assert tenant.name == "Barber Pro" and tenant.default_language == "kk" and tenant.currency == "KZT"
    branch = db.get(Branch, res.branch_id)
    assert branch.tenant_id == tenant.id and branch.timezone == "Asia/Almaty"
    user = db.scalar(select(User).where(User.email == "owner@barberpro.kz"))
    assert verify_password("s3cret-pass-123", user.password_hash)
    m = db.scalar(select(Membership).where(Membership.user_id == user.id))
    assert m.role == "owner" and m.tenant_id == tenant.id
    assert db.scalar(select(AuditLog).where(AuditLog.action == "tenant.create")).tenant_id == tenant.id


def test_create_tenant_is_idempotent(db):
    first = create_tenant(db, name="Salon A", owner_email="a@salon.kz")
    db.commit()
    assert first.created and first.owner_password  # generated, shown once
    again = create_tenant(db, name="Salon A", owner_email="a@salon.kz", owner_password="another-pass-1")
    db.commit()
    assert not again.created and again.owner_password is None
    assert (again.tenant_id, again.branch_id) == (first.tenant_id, first.branch_id)
    assert db.scalar(select(func.count(Tenant.id))) == 1
    user = db.scalar(select(User).where(User.email == "a@salon.kz"))
    assert verify_password(first.owner_password, user.password_hash)  # password not changed


def test_create_tenant_links_existing_user_without_changing_password(db, world):
    res = create_tenant(db, name="Second Org", owner_email="admin@test.kz", owner_password="ignored-pass-1")
    db.commit()
    assert res.created and res.owner_password is None
    db.refresh(world.admin)
    assert verify_password("password123", world.admin.password_hash)
    roles = set(db.scalars(select(Membership.role).where(Membership.user_id == world.admin.id)))
    assert roles == {"admin", "owner"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": " ", "owner_email": "x@y.kz"},
        {"name": "X", "owner_email": "not-an-email"},
        {"name": "X", "owner_email": "x@y.kz", "timezone": "Mars/Base"},
        {"name": "X", "owner_email": "x@y.kz", "owner_password": "short"},
        {"name": "X", "owner_email": "x@y.kz", "niche": "casino"},
    ],
)
def test_create_tenant_validates_input(db, kwargs):
    with pytest.raises(CliError):
        create_tenant(db, **kwargs)
    db.rollback()
    assert db.scalar(select(func.count(Tenant.id))) == 0


def test_cli_main_prints_json(capsys, db):
    code = main(["create-tenant", "--name", "CLI Org", "--owner-email", "cli@org.kz", "--branch", "Main"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created"] is True and out["owner_email"] == "cli@org.kz" and len(out["owner_password"]) >= 12
    assert main(["create-tenant", "--name", "CLI Org", "--owner-email", "cli@org.kz"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert main(["create-tenant", "--name", "Bad", "--owner-email", "bad"]) == 2
