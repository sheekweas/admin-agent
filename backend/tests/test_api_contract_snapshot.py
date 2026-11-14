"""JSON contract snapshot of the endpoints whose OpenAPI models were made explicit (frontend depends on them).

The snapshot was recorded BEFORE the response models were introduced; responses must stay byte-for-byte
equivalent after normalisation (ids/timestamps/dates/secrets → placeholders, everything else literal:
keys, nulls, number types, decimal-string money, datetime offset style). Re-record deliberately with
UPDATE_API_SNAPSHOT=1 only when the contract is meant to change.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).parent / "snapshots" / "api_contract.json"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VOLATILE_KEYS = {"secret"}  # random per run


def _norm_str(s: str) -> str:
    if _UUID.match(s):
        return "<uuid>"
    if m := _DATETIME.match(s):
        return f"<datetime{m.group(3) or ''}>"  # keeps the offset style: "Z" vs "+00:00"
    if _DATE.match(s):
        return "<date>"
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", s)
    s = re.sub(r"\b\d{1,2}:\d{2}\b", "<time>", s)
    return s


def normalize(v: Any, key: str | None = None) -> Any:
    if key in _VOLATILE_KEYS and isinstance(v, str):
        return "<secret>"
    if isinstance(v, dict):
        return {k: normalize(x, k) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [normalize(x) for x in v]
    if isinstance(v, str):
        return _norm_str(v)
    if isinstance(v, bool) or v is None:
        return v
    return {"int": v} if isinstance(v, int) else {"float": v}


def _record(client, tenant) -> dict[str, Any]:
    w = tenant.world
    h = w.headers()
    out: dict[str, Any] = {}

    def call(name: str, method: str, url: str, headers=None, **kw) -> Any:
        r = client.request(method, url, headers=headers or h, **kw)
        assert r.status_code < 400, (name, r.status_code, r.text)
        body = r.json() if r.content else None
        out[name] = {"status": r.status_code, "body": normalize(body)}
        return body

    pay = tenant.one("payment")
    call("payments.list", "GET", "/v1/payments")
    call("payments.get", "GET", f"/v1/payments/{pay}")
    new = call(
        "payments.create",
        "POST",
        "/v1/payments",
        json={"booking_id": tenant.one("booking"), "amount": "500.00", "method": "card"},
    )
    call("payments.refund", "POST", f"/v1/payments/{pay}/refund", json={"amount": "100.00", "reason": "r"})
    call("payments.cancel", "POST", f"/v1/payments/{new['id']}/cancel", json={"reason": "typo"})

    call("integrations.list", "GET", "/v1/integrations")
    call(
        "integrations.put",
        "PUT",
        "/v1/integrations/voice",
        json={"config": {"from_number": "+77000000000"}, "enabled": False},
    )
    call("integrations.test", "POST", "/v1/integrations/telegram/test")
    call("webhooks.list", "GET", "/v1/integrations/webhooks")
    sub = call(
        "webhooks.create",
        "POST",
        "/v1/integrations/webhooks",
        json={"url": "https://hooks.example.com/s", "events": ["booking.created", "payment.confirmed"]},
    )
    call("webhooks.patch", "PATCH", f"/v1/integrations/webhooks/{sub['id']}", json={"include_pii": True})
    call("webhooks.delete", "DELETE", f"/v1/integrations/webhooks/{sub['id']}")
    call("events.notifications", "GET", "/v1/integrations/events")
    call("events.outbox", "GET", "/v1/integrations/events?source=outbox&limit=3")
    call("events.retry_notification", "POST", f"/v1/integrations/events/{tenant.one('notification')}/retry")
    call("events.retry_outbox", "POST", f"/v1/integrations/events/{tenant.one('outbox')}/retry")

    call("audit.list", "GET", "/v1/audit?action=knowledge.create")
    call("reports.overview.owner", "GET", "/v1/reports/overview")
    call("reports.overview.admin", "GET", "/v1/reports/overview", headers=w.headers(w.admin))

    conv = tenant.one("conversation")
    call("conversations.list", "GET", "/v1/conversations")
    call("conversations.get", "GET", f"/v1/conversations/{conv}")
    call("messages.list", "GET", f"/v1/conversations/{conv}/messages")
    call(
        "messages.post",
        "POST",
        f"/v1/conversations/{conv}/messages",
        json={"text": "Note", "internal_note": True},
    )
    call("conversations.takeover", "POST", f"/v1/conversations/{conv}/takeover")
    call("conversations.release", "POST", f"/v1/conversations/{conv}/release")
    call("conversations.read", "POST", f"/v1/conversations/{conv}/read")
    call(
        "conversations.simulate",
        "POST",
        "/v1/conversations/simulate",
        json={"phone": "+77015550001", "name": "Айдар", "text": "Сколько стоит стрижка?"},
    )

    call("knowledge.list", "GET", "/v1/knowledge")
    art = call(
        "knowledge.create",
        "POST",
        "/v1/knowledge",
        json={"title": "Wi-Fi", "content": "Пароль 12345", "test_questions": ["wifi?"]},
    )
    call("knowledge.get", "GET", f"/v1/knowledge/{art['id']}")
    call("knowledge.patch", "PATCH", f"/v1/knowledge/{art['id']}", json={"content": "Пароль 54321"})
    call("knowledge.publish", "POST", f"/v1/knowledge/{art['id']}/publish")
    call("knowledge.archive", "POST", f"/v1/knowledge/{art['id']}/archive")
    call("knowledge.test", "POST", "/v1/knowledge/test", json={"question": "Сколько стоит стрижка?"})

    call("tasks.list", "GET", "/v1/admin-tasks")
    task = call(
        "tasks.create",
        "POST",
        "/v1/admin-tasks",
        json={"question": "Парковка есть?", "contact_id": tenant.one("contact")},
    )
    call("tasks.get", "GET", f"/v1/admin-tasks/{task['id']}")
    call("tasks.ack", "POST", f"/v1/admin-tasks/{task['id']}/ack")
    call("tasks.resolve", "POST", f"/v1/admin-tasks/{task['id']}/resolve", json={"answer": "Да"})
    return out


def test_api_contract_unchanged(client, tenant_a):
    actual = _record(client, tenant_a)
    if os.environ.get("UPDATE_API_SNAPSHOT") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(actual) == set(expected)
    for name in expected:
        assert actual[name] == expected[name], name
