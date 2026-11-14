"""Helpers for the security suites: fully-populated tenants, route introspection, minimal request bodies."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi.routing import APIRoute
from sqlalchemy import select, text

from app.db import Base
from app.models import Call, Notification, OutboxEvent
from tests.booking_helpers import future_day
from tests.conftest import World
from tests.ops_helpers import make_lead, make_visit

TG_TOKEN_PREFIX = "7000000000:AAH-secret-bot-token-0123456789-"
WA_ACCESS_PREFIX = "EAAG-secret-wa-access-token-"


class NamedWorld(World):
    """World with prefixed e-mails / chat ids, so two full tenants can coexist."""

    def __init__(self, db, prefix: str):
        self._prefix = prefix
        super().__init__(db)

    def _user(self, email, role, escalation_primary=False, escalation_backup=False, phone=None, tg=None):
        return super()._user(
            f"{self._prefix}{email}",
            role,
            escalation_primary=escalation_primary,
            escalation_backup=escalation_backup,
            phone=phone,
            tg=f"{self._prefix.strip('-')}{tg}" if tg else None,
        )


@dataclass
class Populated:
    world: World
    ids: dict[str, list[str]] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)

    def add(self, kind: str, value: Any) -> None:
        self.ids.setdefault(kind, []).append(str(value))

    def one(self, kind: str) -> str:
        return self.ids[kind][0]

    def all_ids(self) -> set[str]:
        return {v for values in self.ids.values() for v in values}


def _ok(r, *codes: int):
    assert r.status_code in (codes or (200, 201)), (r.request.method, r.request.url, r.status_code, r.text)
    return r.json() if r.content else None


def populate(client, db, w: World, tag: str) -> Populated:
    """Create one of everything through the public API (plus a few rows the API only creates indirectly)."""
    p = Populated(w)
    h = w.headers()
    for kind, value in [
        ("tenant", w.tenant.id),
        ("branch", w.branch.id),
        ("staff", w.barber1.id),
        ("staff", w.barber2.id),
        ("service", w.haircut.id),
        ("service", w.beard.id),
        ("contact", w.contact.id),
    ]:
        p.add(kind, value)
    for user in (w.owner, w.admin, w.backup_admin, w.staff_user, w.analyst):
        p.add("user", user.id)
    for m in _ok(client.get("/v1/team", headers=h)):
        p.add("membership", m["membership_id"])

    contact2 = _ok(
        client.post("/v1/contacts", headers=h, json={"name": f"Second {tag}", "phone": "+77075550011"}), 201
    )
    p.add("contact", contact2["id"])
    contact3 = _ok(
        client.post("/v1/contacts", headers=h, json={"name": f"Third {tag}", "phone": "+77075550012"}), 201
    )
    p.add("contact", contact3["id"])
    note = _ok(
        client.post(f"/v1/contacts/{w.contact.id}/notes", headers=h, json={"text": f"note {tag}"}), 201
    )
    p.add("note", note["id"])

    day = future_day(3)
    slots = _ok(
        client.get(
            "/v1/availability",
            headers=h,
            params={"service_id": str(w.haircut.id), "date_from": day.isoformat()},
        )
    )["items"]
    booking = _ok(
        client.post(
            "/v1/bookings",
            headers=h,
            json={
                "contact_id": str(w.contact.id),
                "service_id": str(w.haircut.id),
                "slot_id": slots[0]["slot_id"],
            },
        ),
        201,
    )
    p.add("booking", booking["id"])
    booking2 = _ok(
        client.post(
            "/v1/bookings",
            headers=h,
            json={
                "contact_id": contact2["id"],
                "service_id": str(w.haircut.id),
                "slot_id": slots[3]["slot_id"],
            },
        ),
        201,
    )
    p.add("booking", booking2["id"])
    payment = _ok(
        client.post("/v1/payments", headers=h, json={"booking_id": booking["id"], "amount": "1000.00"}), 201
    )
    p.add("payment", payment["id"])
    from app.models import Booking

    visit = make_visit(db, w, db.get(Booking, uuid.UUID(booking2["id"])))
    p.add("visit", visit.id)

    sim = _ok(
        client.post(
            "/v1/conversations/simulate",
            headers=w.headers(w.admin),
            json={"phone": "+77015550099", "name": f"Client {tag}", "text": "Здравствуйте"},
        )
    )
    p.add("conversation", sim["conversation_id"])
    p.add("message", sim["message_id"])
    for m in sim["replies"]:
        p.add("message", m["id"])
    conv = _ok(client.get(f"/v1/conversations/{sim['conversation_id']}", headers=h))
    p.add("contact", conv["contact"]["id"])

    lead = make_lead(db, w, contact=w.contact)
    p.add("lead", lead.id)
    task = _ok(
        client.post(
            "/v1/admin-tasks",
            headers=h,
            json={"question": f"question {tag}", "conversation_id": sim["conversation_id"]},
        ),
        201,
    )
    p.add("task", task["id"])
    # a call row directly (a simulated call would add a voice conversation to the recorded API contract snapshot)
    call = Call(
        tenant_id=w.tenant.id,
        direction="in",
        purpose="client_ai",
        from_number=w.contact.phone_e164,
        to_number="+77270000001",
        contact_id=w.contact.id,
        status="completed",
        transcript_items=[{"speaker": "client", "text": f"call {tag}", "at": None, "confidence": 0.9}],
        metrics={"turns": 1},
    )
    db.add(call)
    db.commit()
    p.add("call", call.id)
    article = _ok(
        client.post("/v1/knowledge", headers=h, json={"title": f"Parking {tag}", "content": "Free parking"}),
        201,
    )
    p.add("article", article["id"])
    exc = _ok(
        client.post(
            f"/v1/staff/{w.barber2.id}/exceptions",
            headers=h,
            json={"date": future_day(20).isoformat(), "kind": "day_off", "reason": "x"},
        ),
        201,
    )
    p.add("exception", exc["id"])
    req = _ok(
        client.post(
            "/v1/staff/shift-requests",
            headers=h,
            json={"staff_id": str(w.barber2.id), "kind": "day_off", "date": future_day(21).isoformat()},
        ),
        201,
    )
    p.add("shift_request", req["id"])
    sub = _ok(
        client.post(
            "/v1/integrations/webhooks",
            headers=h,
            json={"url": f"https://hooks.example.com/{tag}", "events": ["booking.created"]},
        ),
        201,
    )
    p.add("webhook", sub["id"])
    p.secrets.append(sub["secret"])
    tg_token = TG_TOKEN_PREFIX + tag
    wa_token = WA_ACCESS_PREFIX + tag
    p.secrets += [tg_token, wa_token]
    _ok(client.put("/v1/integrations/telegram", headers=h, json={"secrets": {"bot_token": tg_token}}))
    _ok(
        client.put(
            "/v1/integrations/whatsapp",
            headers=h,
            json={"config": {"phone_number_id": f"PNID-{tag}"}, "secrets": {"access_token": wa_token}},
        )
    )
    n = Notification(
        tenant_id=w.tenant.id,
        channel="telegram",
        kind="admin_task",
        recipient="1001",
        payload={"text": f"dead {tag}"},
        status="dead",
        attempts=8,
    )
    db.add(n)
    db.commit()
    p.add("notification", n.id)
    for ev in db.scalars(select(OutboxEvent.id).where(OutboxEvent.tenant_id == w.tenant.id)):
        p.add("outbox", ev)
    return p


# ---------------------------------------------------------------- tenant fingerprint
_SKIP_TABLES = {"audit_logs", "counters"}


def tenant_fingerprint(db, tenant_id: uuid.UUID) -> dict[str, str]:
    """All rows of every tenant-owned table, serialized — to prove a tenant's data did not change."""
    out: dict[str, str] = {}
    db.expire_all()
    for table in Base.metadata.sorted_tables:
        if table.name in _SKIP_TABLES:
            continue
        col = "tenant_id" if "tenant_id" in table.c else ("id" if table.name == "tenants" else None)
        if col is None:
            continue
        rows = db.execute(
            text(f'SELECT row_to_json(t)::text FROM "{table.name}" t WHERE {col} = :tid ORDER BY 1'),
            {"tid": tenant_id},
        ).scalars()
        out[table.name] = json.dumps(list(rows))
    return out


# ---------------------------------------------------------------- route introspection
@dataclass
class RouteInfo:
    method: str
    path: str
    route: APIRoute

    @property
    def path_params(self) -> list[str]:
        return [p.name for p in self.route.dependant.path_params]

    @property
    def query_params(self) -> list[str]:
        return [p.name for p in self.route.dependant.query_params]

    @property
    def permission(self) -> str | None:
        """Permission name captured by the app.deps.require(...) dependency, if any."""
        stack = list(self.route.dependant.dependencies)
        while stack:
            dep = stack.pop()
            for cell in getattr(dep.call, "__closure__", None) or ():
                if isinstance(cell.cell_contents, str) and getattr(dep.call, "__qualname__", "").startswith(
                    "require."
                ):
                    return cell.cell_contents
            stack.extend(dep.dependencies)
        return None


def api_routes(app) -> list[RouteInfo]:
    try:
        from fastapi.routing import iter_route_contexts

        contexts = [(rc.original_route, rc.path, rc.methods) for rc in iter_route_contexts(app.routes)]
    except ImportError:  # older FastAPI: routes are flat
        contexts = [(r, r.path, r.methods) for r in app.routes]
    out = []
    for route, path, methods in contexts:
        if isinstance(route, APIRoute):
            out.extend(RouteInfo(m, path, route) for m in sorted(methods or ()))
    return out


PUBLIC_PREFIXES = ("/v1/webhooks/", "/v1/auth/login", "/health", "/openapi", "/docs", "/redoc")


def is_public(path: str) -> bool:
    return path.startswith(PUBLIC_PREFIXES)


# Path parameter -> kind of id in Populated.ids
PARAM_KIND = {
    "booking_id": "booking",
    "payment_id": "payment",
    "sub_id": "webhook",
    "conversation_id": "conversation",
    "article_id": "article",
    "task_id": "task",
    "staff_id": "staff",
    "exception_id": "exception",
    "request_id": "shift_request",
    "contact_id": "contact",
    "lead_id": "lead",
    "branch_id": "branch",
    "service_id": "service",
    "membership_id": "membership",
    "call_id": "call",
}


def body_for(method: str, path: str, own: Populated) -> Any:
    """Minimal VALID body for each mutating route (validated by the control calls with own ids)."""
    w = own.world
    day = future_day(25).isoformat()
    bodies: dict[tuple[str, str], Any] = {
        ("PATCH", "/v1/team/{membership_id}"): {"full_name": "Renamed"},
        ("PATCH", "/v1/branches/{branch_id}"): {"address": "New address"},
        ("PATCH", "/v1/services/{service_id}"): {"description": "changed"},
        ("POST", "/v1/staff/shift-requests/{request_id}/decision"): {"approve": False, "comment": "no"},
        ("PATCH", "/v1/staff/{staff_id}"): {"specialization": "changed"},
        ("PUT", "/v1/staff/{staff_id}/shifts"): [{"weekday": 0, "start_time": "10:00", "end_time": "18:00"}],
        ("POST", "/v1/staff/{staff_id}/exceptions"): {"date": day, "kind": "day_off"},
        ("PATCH", "/v1/bookings/{booking_id}"): {"comment": "changed"},
        ("PATCH", "/v1/contacts/{contact_id}"): {"name": "Changed"},
        ("POST", "/v1/contacts/{contact_id}/notes"): {"text": "note"},
        ("POST", "/v1/contacts/{contact_id}/merge"): {"other_id": own.ids["contact"][2]},
        ("PATCH", "/v1/leads/{lead_id}"): {"tags": ["x"]},
        ("POST", "/v1/conversations/{conversation_id}/messages"): {"text": "hello", "internal_note": True},
        ("PATCH", "/v1/knowledge/{article_id}"): {"title": "Changed"},
        ("POST", "/v1/admin-tasks/{task_id}/resolve"): {"answer": "done"},
        ("POST", "/v1/payments/{payment_id}/refund"): {"amount": "10.00", "reason": "r"},
        ("POST", "/v1/payments/{payment_id}/cancel"): {"reason": "r"},
        ("PATCH", "/v1/integrations/webhooks/{sub_id}"): {"enabled": False},
        # collection routes (role matrix)
        ("PATCH", "/v1/tenant"): {"name": w.tenant.name},
        ("POST", "/v1/team"): {
            "email": f"new-{uuid.uuid4().hex[:6]}@x.kz",
            "password": "password123",
            "role": "analyst",
        },
        ("POST", "/v1/branches"): {"name": "New branch"},
        ("POST", "/v1/services"): {"title_ru": "New", "duration_min": 30, "price": "100"},
        ("POST", "/v1/staff/shift-requests"): {
            "staff_id": str(w.barber1.id),
            "kind": "day_off",
            "date": future_day(26).isoformat(),
        },
        ("POST", "/v1/staff"): {"branch_id": str(w.branch.id), "name": "New barber"},
        ("POST", "/v1/bookings"): {
            "contact": {"name": "Walk-in", "phone": "+77075550077"},
            "service_id": str(w.beard.id),
            "start_at": "2030-01-01T05:00:00+00:00",
        },
        ("POST", "/v1/contacts"): {"name": "New", "phone": f"+7707{uuid.uuid4().int % 10**7:07d}"},
        ("POST", "/v1/conversations/simulate"): {"phone": "+77015550098", "text": "Привет"},
        ("POST", "/v1/calls/simulate"): {"phone": "+77015550096", "utterances": ["Здравствуйте"]},
        ("POST", "/v1/calls/outbound"): {"contact_id": str(w.contact.id), "purpose": "callback"},
        ("POST", "/v1/knowledge"): {"title": "New", "content": "Body"},
        ("POST", "/v1/knowledge/test"): {"question": "Есть парковка?"},
        ("POST", "/v1/admin-tasks"): {"question": "Q?"},
        ("POST", "/v1/payments"): {"booking_id": own.one("booking"), "amount": "1.00"},
        ("PUT", "/v1/integrations/{kind}"): {"enabled": True},
        ("POST", "/v1/integrations/webhooks"): {"url": "https://hooks.example.com/new", "events": ["*"]},
    }
    return bodies.get((method, path))
