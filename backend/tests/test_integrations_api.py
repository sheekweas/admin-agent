"""Integrations API: secrets are write-only/masked, owner-only writes, webhook subscriptions, audit API."""

from __future__ import annotations

import json

from app.models import AuditLog, Integration, WebhookSubscription
from app.security import decrypt_secret

TOKEN = "123456:AAH-very-secret-bot-token-xyz"


def test_secrets_never_returned_and_owner_only_write(client, db, world):
    owner, admin = world.headers(world.owner), world.headers(world.admin)
    body = {"config": {"note": "main bot"}, "secrets": {"bot_token": TOKEN}, "enabled": True}
    assert client.put("/v1/integrations/telegram", headers=admin, json=body).status_code == 403
    r = client.put("/v1/integrations/telegram", headers=owner, json=body)
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text and r.json()["secrets_set"] == ["bot_token"]
    assert r.json()["secrets"]["bot_token"].endswith("-xyz"[-4:])
    integ = db.query(Integration).filter_by(kind="telegram").one()
    assert TOKEN not in integ.secrets_enc and json.loads(decrypt_secret(integ.secrets_enc)) == {
        "bot_token": TOKEN
    }
    assert integ.external_id == "123456"
    listing = client.get("/v1/integrations", headers=admin)
    assert listing.status_code == 200 and TOKEN not in listing.text
    kinds = {i["kind"]: i for i in listing.json()["items"]}
    assert set(kinds) == {"whatsapp", "telegram", "voice", "zapier"} and kinds["telegram"]["enabled"] is True
    assert client.get("/v1/integrations", headers=world.headers(world.staff_user)).status_code == 403
    # masked echo from the UI keeps the secret, empty string deletes it
    client.put(
        "/v1/integrations/telegram",
        headers=owner,
        json={"secrets": {"bot_token": r.json()["secrets"]["bot_token"]}},
    )
    db.expire_all()
    assert json.loads(decrypt_secret(db.query(Integration).filter_by(kind="telegram").one().secrets_enc))
    r = client.put("/v1/integrations/telegram", headers=owner, json={"secrets": {"bot_token": ""}})
    assert r.json()["secrets_set"] == []
    audit = db.query(AuditLog).filter_by(action="integration.update").all()
    assert audit and all(TOKEN not in json.dumps(a.payload) for a in audit)


def test_whatsapp_number_cannot_be_claimed_by_two_tenants(client, world, other_world):
    body = {"config": {"phone_number_id": "PN1"}, "secrets": {"access_token": "t"}}
    assert client.put("/v1/integrations/whatsapp", headers=world.headers(), json=body).status_code == 200
    r = client.put("/v1/integrations/whatsapp", headers=other_world.headers(other_world.owner), json=body)
    assert r.status_code == 409


def test_unknown_kind_404_and_test_endpoint(client, world):
    assert client.put("/v1/integrations/sms", headers=world.headers(), json={}).status_code == 404
    r = client.post("/v1/integrations/telegram/test", headers=world.headers())
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/v1/integrations/voice/test", headers=world.headers())
    assert r.json() == {"ok": True, "detail": "provider mock"}
    r = client.post("/v1/integrations/whatsapp/test", headers=world.headers())
    assert r.json()["ok"] is False


def test_webhook_subscriptions_crud(client, db, world):
    owner = world.headers()
    r = client.post(
        "/v1/integrations/webhooks",
        headers=owner,
        json={"url": "https://hooks.zapier.com/hooks/catch/1/abc/", "events": ["booking.created"]},
    )
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert len(secret) > 20
    sub = db.query(WebhookSubscription).one()
    assert sub.secret != secret and decrypt_secret(sub.secret) == secret  # encrypted at rest
    listing = client.get("/v1/integrations/webhooks", headers=world.headers(world.admin)).json()
    assert listing["total"] == 1 and "secret" not in listing["items"][0] and secret not in json.dumps(listing)
    assert set(listing["items"][0]) == {"id", "url", "events", "include_pii", "enabled", "created_at"}
    bad = client.post(
        "/v1/integrations/webhooks", headers=owner, json={"url": "https://x.io", "events": ["nope"]}
    )
    assert bad.status_code == 422
    assert (
        client.post(
            "/v1/integrations/webhooks",
            headers=world.headers(world.admin),
            json={"url": "https://x.io/h", "events": ["booking.created"]},
        ).status_code
        == 403
    )
    sid = r.json()["id"]
    assert (
        client.patch(f"/v1/integrations/webhooks/{sid}", headers=owner, json={"enabled": False}).json()[
            "enabled"
        ]
        is False
    )
    assert client.delete(f"/v1/integrations/webhooks/{sid}", headers=owner).status_code == 204
    assert client.get("/v1/integrations/webhooks", headers=owner).json()["total"] == 0


def test_audit_api_owner_only_with_filters(client, db, world):
    client.put("/v1/integrations/zapier", headers=world.headers(), json={"enabled": True})
    r = client.get("/v1/audit?action=integration.*", headers=world.headers())
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["items"][0]["action"] == "integration.update"
    assert client.get("/v1/audit", headers=world.headers(world.admin)).status_code == 403
