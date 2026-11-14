"""HTTP hardening: security headers, body limits, rate limits, production config validation, key rotation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app import middleware
from app.config import DEFAULT_JWT_SECRET, Settings, get_settings, production_problems, validate_settings
from app.models import Integration, WebhookSubscription
from app.security import decrypt_secret, encrypt_secret, needs_reencryption
from tests.ai_helpers import post_webhook, wa_payload


@pytest.fixture
def limits(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_backend", "memory")
    middleware.reset_rate_limits()
    yield s
    middleware.reset_rate_limits()


# ---------------------------------------------------------------- headers
def test_security_headers_on_api_and_errors(client, world):
    for resp in (
        client.get("/health"),
        client.get("/v1/auth/me"),  # 401
        client.get("/v1/bookings", headers=world.headers()),
    ):
        h = resp.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "strict-transport-security" not in h  # dev
    assert client.get("/v1/bookings", headers=world.headers()).headers["cache-control"] == "no-store"


def test_docs_page_keeps_working_without_strict_csp(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers and r.headers["x-frame-options"] == "DENY"


def test_hsts_in_production(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "env", "production")
    assert client.get("/health").headers["strict-transport-security"].startswith("max-age=")


def test_cors_preflight_still_allowed(client):
    r = client.options(
        "/v1/auth/login",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:3000"


# ---------------------------------------------------------------- body limits
def test_webhook_body_limit(client, db, world):
    big = b'{"object":"x","pad":"' + b"a" * 1_100_000 + b'"}'
    for path in (
        "/v1/webhooks/whatsapp",
        f"/v1/webhooks/telegram/{world.tenant.id}",
        "/v1/webhooks/voice/mock/status",
    ):
        r = client.post(path, content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (path, r.status_code)
        assert r.headers["x-content-type-options"] == "nosniff"


def test_webhook_body_limit_for_chunked_upload(client):
    def chunks():
        for _ in range(20):
            yield b"a" * 100_000

    r = client.post("/v1/webhooks/whatsapp", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_api_body_limit_is_configurable(client, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_body_bytes", 1000)
    r = client.post("/v1/knowledge", headers=world.headers(), json={"title": "t", "content": "x" * 2000})
    assert r.status_code == 413
    ok = client.post("/v1/knowledge", headers=world.headers(), json={"title": "t", "content": "x" * 10})
    assert ok.status_code == 201


# ---------------------------------------------------------------- rate limits
def test_login_rate_limited_per_ip_and_path(client, world, limits, monkeypatch):
    monkeypatch.setattr(limits, "rate_limit_login_per_minute", 3)
    codes = [
        client.post("/v1/auth/login", json={"email": f"nobody{i}@x.kz", "password": "x"}).status_code
        for i in range(5)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
    r = client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": "password123"})
    assert r.status_code == 429 and "retry-after" in r.headers  # even valid credentials wait
    # other endpoints are not affected
    assert client.get("/v1/auth/me", headers=world.headers()).status_code == 200


def test_webhook_rate_limited(client, world, limits, monkeypatch):
    monkeypatch.setattr(limits, "rate_limit_webhook_per_minute", 2)
    codes = [post_webhook(client, wa_payload(text="hi")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limit_disabled_by_setting(client, world, limits, monkeypatch):
    monkeypatch.setattr(limits, "rate_limit_login_per_minute", 1)
    monkeypatch.setattr(limits, "rate_limit_enabled", False)
    codes = {
        client.post("/v1/auth/login", json={"email": "a@b.kz", "password": "x"}).status_code for _ in range(3)
    }
    assert codes == {401}


def test_redis_counter_when_available():
    try:
        counter = middleware.RedisCounter(get_settings().redis_url)
    except Exception:  # noqa: BLE001
        pytest.skip("Redis not reachable")
    key = f"test:{uuid.uuid4().hex}"
    assert [counter.hit(key, 60) for _ in range(3)] == [1, 2, 3]


def test_auto_backend_falls_back_to_memory(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_backend", "auto")
    monkeypatch.setattr(s, "redis_url", "redis://127.0.0.1:1/0")  # nothing listens there
    middleware.reset_rate_limits()
    try:
        assert isinstance(middleware.counter_backend(), middleware.MemoryCounter)
    finally:
        middleware.reset_rate_limits()


# ---------------------------------------------------------------- production config validation
def _prod(**kw) -> Settings:
    good = {
        "env": "production",
        "jwt_secret": "x" * 48,
        "telegram_webhook_secret": "t" * 32,
        "whatsapp_mode": "live",
        "whatsapp_app_secret": "meta-app-secret-value",
        "whatsapp_verify_token": "verify-token-value",
    }
    return Settings(_env_file=None, **{**good, **kw})


def test_production_refuses_default_secrets():
    assert production_problems(_prod()) == []
    assert production_problems(Settings(_env_file=None, env="dev")) == []
    for bad in (
        {"jwt_secret": DEFAULT_JWT_SECRET},
        {"jwt_secret": "change-me-to-a-long-random-string"},
        {"jwt_secret": "short"},
        {"telegram_webhook_secret": "dev-telegram-secret"},
        {"whatsapp_app_secret": "dev-app-secret"},
        {"whatsapp_verify_token": "dev-verify-token"},
        {"env": "prod", "jwt_secret": DEFAULT_JWT_SECRET},
    ):
        s = _prod(**bad)
        assert production_problems(s), bad
        with pytest.raises(RuntimeError, match="insecure production settings"):
            validate_settings(s)


def test_api_process_refuses_to_start_with_default_jwt_secret():
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "ENV": "production", "JWT_SECRET": DEFAULT_JWT_SECRET}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "JWT_SECRET" in proc.stderr and DEFAULT_JWT_SECRET not in proc.stderr


# ---------------------------------------------------------------- secret encryption key rotation
def test_secrets_key_rotation(db, world, monkeypatch):
    from app.rotate_secrets import rotate

    s = get_settings()
    integ = Integration(
        tenant_id=world.tenant.id,
        kind="telegram",
        config={},
        secrets_enc=encrypt_secret(json.dumps({"bot_token": "tok"})),
    )
    sub = WebhookSubscription(
        tenant_id=world.tenant.id, url="https://h.example.com", events=["*"], secret=encrypt_secret("whsec")
    )
    legacy = WebhookSubscription(
        tenant_id=world.tenant.id, url="https://h2.example.com", events=["*"], secret="legacy-plaintext"
    )
    db.add_all([integ, sub, legacy])
    db.commit()
    # new dedicated key; the old (JWT-derived) key stays readable automatically
    monkeypatch.setattr(s, "secrets_encryption_key", "k" * 40)
    assert decrypt_secret(sub.secret) == "whsec" and needs_reencryption(sub.secret)
    assert rotate(db) == 3
    db.commit()
    assert rotate(db) == 0  # idempotent
    # after rotation the JWT secret can change without losing integration secrets
    monkeypatch.setattr(s, "jwt_secret", "a-brand-new-jwt-secret-" + "z" * 30)
    db.expire_all()
    assert json.loads(decrypt_secret(db.get(Integration, integ.id).secrets_enc)) == {"bot_token": "tok"}
    assert decrypt_secret(db.get(WebhookSubscription, legacy.id).secret) == "legacy-plaintext"
    with pytest.raises(ValueError):
        monkeypatch.setattr(s, "secrets_encryption_key", "q" * 40)
        decrypt_secret(db.get(WebhookSubscription, sub.id).secret)
