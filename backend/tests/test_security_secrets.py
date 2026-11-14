"""Secret / log hygiene (ТЗ §20.1, §22.1 "утечка логов"): passwords, JWTs and integration secrets never appear
in API responses (except the one-time webhook secret at creation), audit payloads or log records."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.logging_setup import redact
from app.models import AuditLog, Integration
from app.services.integrations import telegram_webhook_secret
from tests.ai_helpers import post_webhook, wa_payload

PASSWORD = "password123"


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _audit_text(db) -> str:
    return json.dumps(
        [[a.action, a.payload] for a in db.scalars(select(AuditLog))], ensure_ascii=False, default=str
    )


def _all_logs(caplog) -> str:
    return "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records) + caplog.text


def test_login_never_logs_or_audits_password_or_token(client, db, world, logs):
    r = client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": PASSWORD})
    assert r.status_code == 200
    token = r.json()["access_token"]
    bad = client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": "wrong-" + PASSWORD})
    assert bad.status_code == 401 and PASSWORD not in bad.text
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert token not in me.text and PASSWORD not in me.text
    for secret in (PASSWORD, token):
        assert secret not in _all_logs(logs)
        assert secret not in _audit_text(db)


def test_team_member_password_not_echoed(client, db, world, logs):
    pw = "Sup3r-Secret-Pa55"
    r = client.post(
        "/v1/team", headers=world.headers(), json={"email": "new@test.kz", "password": pw, "role": "admin"}
    )
    assert r.status_code == 201
    team = client.get("/v1/team", headers=world.headers()).text
    for text in (r.text, team, _audit_text(db), _all_logs(logs)):
        assert pw not in text
    assert "password" not in r.json()


def test_integration_secrets_are_write_only(client, db, world, logs):
    h = world.headers()
    secrets = {
        "bot_token": "7000000000:AAH-integration-secret-token-0123456789",
        "access_token": "EAAG-very-secret-wa-token-0123456789",
        "auth_token": "twilio-secret-auth-token-0123456789",
        "app_secret": "meta-app-secret-0123456789",
    }
    responses = [
        client.put(
            "/v1/integrations/telegram", headers=h, json={"secrets": {"bot_token": secrets["bot_token"]}}
        ),
        client.put(
            "/v1/integrations/whatsapp",
            headers=h,
            json={
                "config": {"phone_number_id": "PN1"},
                "secrets": {k: secrets[k] for k in ("access_token", "app_secret")},
            },
        ),
        client.put(
            "/v1/integrations/voice", headers=h, json={"secrets": {"auth_token": secrets["auth_token"]}}
        ),
        client.get("/v1/integrations", headers=h),
        client.post("/v1/integrations/telegram/test", headers=h),
        client.get("/v1/integrations/events", headers=h),
        client.get("/v1/audit", headers=h),
    ]
    sub = client.post(
        "/v1/integrations/webhooks", headers=h, json={"url": "https://hooks.example.com/x", "events": ["*"]}
    )
    sub_secret = sub.json()["secret"]  # shown exactly once
    responses += [client.get("/v1/integrations/webhooks", headers=h), client.get("/v1/audit", headers=h)]
    for r in responses:
        assert r.status_code < 400, r.text
        for value in [*secrets.values(), sub_secret]:
            assert value not in r.text, (r.request.url, value)
    listing = client.get("/v1/integrations", headers=h).json()["items"]
    tg = next(i for i in listing if i["kind"] == "telegram")
    assert tg["secrets"]["bot_token"] == "••••" + secrets["bot_token"][-4:]
    # at rest: encrypted, never plaintext
    for integ in db.scalars(select(Integration)):
        assert not any(v in integ.secrets_enc for v in secrets.values())
        assert not any(v in json.dumps(integ.config) for v in secrets.values())
    for value in [*secrets.values(), sub_secret]:
        assert value not in _audit_text(db)
        assert value not in _all_logs(logs)


def test_provider_errors_do_not_leak_bot_token(client, db, world, logs, monkeypatch):
    """Live Telegram failing with an error whose text contains the request URL (and so the token)."""
    token = "7000000000:AAH-live-token-should-never-leak-0123456"
    h = world.headers()
    client.put("/v1/integrations/telegram", headers=h, json={"secrets": {"bot_token": token}})
    monkeypatch.setattr(get_settings(), "telegram_mode", "live")

    def boom(url, **kw):
        logging.getLogger("httpx").warning("HTTP Request: POST %s failed", url)
        raise httpx.ConnectError(f"connection to {url} failed")

    monkeypatch.setattr(httpx, "post", boom)
    r = client.post("/v1/integrations/telegram/test", headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False
    db.expire_all()
    integ = db.scalar(select(Integration).where(Integration.kind == "telegram"))
    for text in (r.text, integ.last_error or "", _all_logs(logs), _audit_text(db)):
        assert token not in text
        assert token.split(":")[1] not in text


def test_webhook_handling_does_not_log_signatures_or_secrets(client, db, world, logs):
    tg_secret = telegram_webhook_secret(world.tenant.id)
    client.post(
        f"/v1/webhooks/telegram/{world.tenant.id}",
        json={"update_id": 1, "message": {"chat": {"id": 1}, "text": "/start"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret},
    )
    client.post(
        f"/v1/webhooks/telegram/{world.tenant.id}",
        json={"update_id": 2},
        headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret[:-4] + "XXXX"},
    )
    r = post_webhook(client, wa_payload(text="hello"))
    assert r.status_code == 200
    forged = post_webhook(client, wa_payload(text="x"), signature="sha256=" + "ab" * 32)
    assert forged.status_code == 401
    text = _all_logs(logs)
    for value in (
        tg_secret,
        get_settings().telegram_webhook_secret,
        get_settings().whatsapp_app_secret,
        r.request.headers["x-hub-signature-256"],
    ):
        assert value not in text


def test_redaction_filter_covers_common_secret_shapes(logs):
    log = logging.getLogger("app.test")
    jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
    log.info("POST https://api.telegram.org/bot7000000000:AAH-abcdefghijklmnopqrstuvwxyz012345/sendMessage")
    log.info("headers %s", {"Authorization": "Bearer " + jwt_like})
    log.info("token=%s password=%s", "plain-token-value", "hunter2-password")
    try:
        raise RuntimeError("bad token " + jwt_like)
    except RuntimeError:
        log.exception("failure")
    text = _all_logs(logs)
    for secret in ("AAH-abcdefghijklmnopqrstuvwxyz012345", jwt_like, "plain-token-value", "hunter2-password"):
        assert secret not in text
    assert "<redacted" in text
    assert redact("author=ai booking BK-000123 at 10:00") == "author=ai booking BK-000123 at 10:00"
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_unhandled_errors_return_generic_message(client, world, monkeypatch, logs):
    def explode(*a, **kw):
        raise RuntimeError("db password=hunter2-in-exception")

    monkeypatch.setattr("app.api.reports.svc.overview", explode)
    r = client.get("/v1/reports/overview", headers=world.headers())
    assert r.status_code == 500
    assert "hunter2" not in r.text and r.json()["detail"]["code"] == "INTERNAL"
    assert "hunter2-in-exception" not in _all_logs(logs)


def test_uvicorn_access_log_formatting_keeps_args_and_redacts_query():
    """uvicorn's AccessFormatter unpacks record.args itself; redaction must keep them a 5-tuple."""
    from uvicorn.logging import AccessFormatter

    fmt = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    logger = logging.getLogger("uvicorn.access")
    plain = logger.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/health", "1.1", 200),
        None,
    )
    assert isinstance(plain.args, tuple) and len(plain.args) == 5
    assert fmt.format(plain).startswith('1.2.3.4:5 - "GET /health HTTP/1.1" 200')
    secret_path = (
        "/v1/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=very-secret-verify&hub.challenge=1"
    )
    rec = logger.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", secret_path, "1.1", 200),
        None,
    )
    assert isinstance(rec.args, tuple) and len(rec.args) == 5
    line = fmt.format(rec)
    assert "very-secret-verify" not in line and " 200" in line and "hub.challenge=1" in line
