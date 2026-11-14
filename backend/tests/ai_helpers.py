"""Shared helpers for the WhatsApp / AI agent tests."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.config import get_settings
from app.models import Integration
from app.security import encrypt_secret, sign_payload

WA_PNID = "PNID-TEST-1"


def add_wa_integration(
    db, tenant, *, pnid: str = WA_PNID, config: dict | None = None, secrets: dict | None = None
) -> Integration:
    integ = Integration(
        tenant_id=tenant.id,
        kind="whatsapp",
        external_id=pnid,
        config=config or {},
        secrets_enc=encrypt_secret(json.dumps(secrets or {"access_token": "test-token"})),
        enabled=True,
    )
    db.add(integ)
    db.commit()
    return integ


def wa_payload(
    *,
    pnid: str = WA_PNID,
    wa_id: str = "77015550001",
    text: str | None = "Здравствуйте",
    mid: str | None = None,
    name: str = "Айдар",
    message: dict | None = None,
    statuses: list[dict] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "77000000000", "phone_number_id": pnid},
    }
    if message is not None or text is not None:
        msg = message or {"type": "text", "text": {"body": text}}
        value["contacts"] = [{"profile": {"name": name}, "wa_id": wa_id}]
        value["messages"] = [
            {
                "from": wa_id,
                "id": mid or f"wamid.{uuid.uuid4().hex}",
                "timestamp": str(int(time.time())),
                **msg,
            }
        ]
    if statuses:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}],
    }


def post_webhook(client, payload: dict, *, secret: str | None = None, signature: str | None = None):
    body = json.dumps(payload).encode()
    sig = signature or "sha256=" + sign_payload(secret or get_settings().whatsapp_app_secret, body)
    return client.post(
        "/v1/webhooks/whatsapp",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )


def say(client, world, text: str, *, phone: str = "+77015550001", name: str = "Айдар") -> dict[str, Any]:
    """Send a client message through the mock-mode simulator and return its response (incl. AI replies)."""
    r = client.post(
        "/v1/conversations/simulate",
        headers=world.headers(world.admin),
        json={"phone": phone, "name": name, "text": text},
    )
    assert r.status_code == 200, r.text
    return r.json()


def reply_text(resp: dict[str, Any]) -> str:
    return "\n".join(m["text"] for m in resp["replies"])
