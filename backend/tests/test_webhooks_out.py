"""Outbound webhooks (ТЗ §16.3): signature + timestamp, PII stripping, backoff, DLQ, manual retry."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest

from app.models import Notification, OutboxEvent, WebhookSubscription
from app.security import encrypt_secret
from app.services import webhooks_out
from app.services.notifications import send_due_notifications
from tests.ops_helpers import T0


class FakePost:
    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.requests = status, exc, []

    def __call__(self, url, content=None, headers=None, **kw):
        self.requests.append({"url": url, "body": content, "headers": headers})
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, text="nope" if self.status >= 300 else "ok")


@pytest.fixture
def post(monkeypatch) -> FakePost:
    fake = FakePost()
    monkeypatch.setattr(webhooks_out.httpx, "post", fake)
    return fake


def _setup(
    db, world, include_pii=False, events=("booking.confirmed",)
) -> tuple[WebhookSubscription, OutboxEvent]:
    sub = WebhookSubscription(
        tenant_id=world.tenant.id,
        url="https://hooks.zapier.com/hooks/catch/1/abc/",
        events=list(events),
        secret=encrypt_secret("topsecret"),
        include_pii=include_pii,
    )
    ev = OutboxEvent(
        tenant_id=world.tenant.id,
        event_type="booking.confirmed",
        aggregate_type="booking",
        payload={
            "booking_id": "b1",
            "number": "BK-000001",
            "phone": "+77071234567",
            "contact": {"id": "c1", "name": "Нурлан"},
            "total_price": "5000.00",
        },
    )
    db.add_all([sub, ev])
    db.commit()
    webhooks_out.enqueue_for_event(db, ev)
    db.commit()
    return sub, ev


def test_signed_delivery_without_pii(db, world, post):
    _, ev = _setup(db, world)
    assert send_due_notifications(db, T0) == 1
    req = post.requests[0]
    h = req["headers"]
    ts = h["X-CRM-Timestamp"]
    assert ts == str(int(T0.timestamp())) and h["X-CRM-Event"] == "booking.confirmed"
    expected = (
        "sha256=" + hmac.new(b"topsecret", ts.encode() + b"." + req["body"], hashlib.sha256).hexdigest()
    )
    assert h["X-CRM-Signature"] == expected
    body = json.loads(req["body"])
    assert body["id"] == str(ev.id) and body["type"] == "booking.confirmed"
    assert body["data"] == {
        "booking_id": "b1",
        "number": "BK-000001",
        "contact": {"id": "c1"},
        "total_price": "5000.00",
    }
    n = db.query(Notification).one()
    assert n.status == "sent" and h["X-CRM-Delivery"] == str(n.id)
    webhooks_out.enqueue_for_event(db, ev)  # redelivery of the event -> no duplicate
    db.commit()
    assert db.query(Notification).count() == 1


def test_include_pii_keeps_personal_data(db, world, post):
    _setup(db, world, include_pii=True)
    send_due_notifications(db, T0)
    assert json.loads(post.requests[0]["body"])["data"]["phone"] == "+77071234567"


def test_unsubscribed_events_are_not_sent(db, world, post):
    _setup(db, world, events=("payment.confirmed",))
    assert db.query(Notification).count() == 0


def test_failures_backoff_then_dlq_and_manual_retry(client, db, world, post):
    post.status = 500
    _setup(db, world)
    send_due_notifications(db, T0)
    n = db.query(Notification).one()
    assert n.status == "pending" and n.attempts == 1 and n.next_attempt_at == T0 + timedelta(seconds=30)
    post.exc = httpx.ConnectError("down")
    now = T0
    for _ in range(10):
        now += timedelta(hours=7)
        send_due_notifications(db, now)
    db.refresh(n)
    assert n.status == "dead" and n.attempts == 8  # settings.webhook_max_attempts
    r = client.get("/v1/integrations/events?status=dead&channel=webhook", headers=world.headers())
    item = r.json()["items"][0]
    assert item["id"] == str(n.id) and item["recipient"] == "https://hooks.zapier.com/…"
    r = client.post(f"/v1/integrations/events/{n.id}/retry", headers=world.headers(world.admin))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    post.exc, post.status = None, 200
    db.expire_all()
    assert send_due_notifications(db) == 1


def test_410_gone_is_dead_immediately(db, world, post):
    post.status = 410
    _setup(db, world)
    send_due_notifications(db, T0)
    assert db.query(Notification).one().status == "dead"


def test_disabled_subscription_skips_delivery(db, world, post):
    sub, _ = _setup(db, world)
    sub.enabled = False
    db.commit()
    send_due_notifications(db, T0)
    assert db.query(Notification).one().status == "cancelled" and post.requests == []


def test_target_url_validation(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(webhooks_out, "get_settings", lambda: Settings(env="prod"))
    for bad in (
        "http://example.com/x",
        "https://127.0.0.1/x",
        "https://10.0.0.5/hook",
        "https://localhost/x",
        "ftp://example.com",
    ):
        with pytest.raises(ValueError):
            webhooks_out.validate_target_url(bad)
    webhooks_out.validate_target_url("https://hooks.zapier.com/hooks/catch/1/abc/")
