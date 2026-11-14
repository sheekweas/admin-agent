"""Helpers for the AI voice-channel tests (mock voice provider, Twilio-shaped webhooks)."""

from __future__ import annotations

import html
import json
import re
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.models import Call, Integration, Tenant
from app.security import encrypt_secret

VOICE_NUMBER = "+77270000001"


def add_voice_integration(
    db, tenant, *, number: str = VOICE_NUMBER, secrets: dict | None = None
) -> Integration:
    integ = Integration(
        tenant_id=tenant.id,
        kind="voice",
        external_id=number,
        config={"phone_number": number},
        secrets_enc=encrypt_secret(json.dumps(secrets)) if secrets else "",
        enabled=True,
    )
    db.add(integ)
    db.commit()
    return integ


def set_settings(db, tenant, **sections) -> None:
    """Shallow-merge settings sections, e.g. voice={...}, escalation={...}."""
    t = db.get(Tenant, tenant.id)
    current = dict(t.settings or {})
    for key, value in sections.items():
        current[key] = {**(current.get(key) or {}), **value} if isinstance(value, dict) else value
    t.settings = current
    db.commit()


def no_quiet_hours(db, tenant) -> None:
    set_settings(db, tenant, escalation={"quiet_hours": [0, 0]})


def action_path(xml: str, verb: str = "Gather") -> str:
    """Relative path + query of the action URL of the first <Gather>/<Dial> in a TwiML document."""
    m = re.search(rf"<{verb}[^>]*\baction=\"([^\"]+)\"", xml)
    assert m, xml
    return html.unescape(m.group(1)).removeprefix(get_settings().public_base_url.rstrip("/"))


def inbound(
    client, *, phone: str = "+77015550001", sid: str | None = None, to: str = VOICE_NUMBER, headers=None
):
    sid = sid or f"CA{uuid.uuid4().hex}"
    r = client.post(
        "/v1/webhooks/voice/mock/inbound",
        data={"CallSid": sid, "From": phone, "To": to},
        headers=headers or {},
    )
    return r, sid


def turn(client, xml: str, speech: str, confidence: float | None = 0.93):
    data = {"SpeechResult": speech}
    if confidence is not None:
        data["Confidence"] = str(confidence)
    r = client.post(action_path(xml), data=data)
    assert r.status_code == 200, r.text
    return r.text


def call_by_sid(db, sid: str) -> Call:
    db.expire_all()
    return db.scalar(select(Call).where(Call.provider_ref == sid))


def said(xml: str) -> str:
    return " ".join(html.unescape(t) for t in re.findall(r"<Say[^>]*>(.*?)</Say>", xml, re.DOTALL))
