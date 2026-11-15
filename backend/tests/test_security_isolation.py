"""Tenant isolation / IDOR sweep and RBAC matrix (ТЗ §17.1, §20.1, §22.1 "Security").

Every authenticated route is discovered from ``app.main.app`` — a new route is covered automatically, and a
route this suite cannot exercise fails ``test_sweep_covers_every_id_route`` instead of being skipped silently.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.deps import PERMISSIONS
from app.main import app
from app.models import AuditLog, Contact, Membership, User
from tests.booking_helpers import future_day
from tests.conftest import session_token
from tests.security_helpers import (
    PARAM_KIND,
    Populated,
    RouteInfo,
    api_routes,
    body_for,
    is_public,
    tenant_fingerprint,
)

ROUTES = api_routes(app)
ROLES = ("owner", "admin", "staff", "analyst")
CONTROL_OK = {200, 201, 204, 409}  # with own ids the request must be valid (not 404/422)


def _id_routes() -> list[RouteInfo]:
    routes = [r for r in ROUTES if not is_public(r.path) and r.path_params and r.path_params != ["kind"]]
    return sorted(routes, key=lambda r: (r.method == "DELETE", r.path, r.method))


def _url(r: RouteInfo, values: dict[str, str]) -> str:
    url = r.path.format(**values)
    if "date_from" in r.query_params:  # the only required query parameter on id routes
        url += f"?date_from={future_day(3).isoformat()}"
    return url


def _assert_no_foreign_data(resp, foreign: Populated) -> None:
    body = resp.text
    leaked = [(k, i) for k, values in foreign.ids.items() for i in values if i in body]
    assert not leaked, (resp.request.method, resp.request.url, leaked, body[:500])
    assert "bravo" not in body, (resp.request.method, resp.request.url, body[:500])


def _event_candidates(p: Populated) -> list[str]:
    return [p.one("notification"), p.one("outbox")]


def _param_values(r: RouteInfo, p: Populated) -> list[dict[str, str]]:
    if r.path_params == ["event_id"]:
        return [{"event_id": v} for v in _event_candidates(p)]
    if r.path.endswith("/sign-out"):  # signing the owner out would also end the spare session used below
        return [{"membership_id": p.one("signout_membership")}]
    if "exception_id" in r.path_params:  # the exception belongs to barber2
        return [{"staff_id": p.ids["staff"][1], "exception_id": p.one("exception")}]
    return [{name: p.one(PARAM_KIND[name]) for name in r.path_params}]


def test_sweep_covers_every_id_route():
    unknown = [
        (r.method, r.path, n)
        for r in _id_routes()
        for n in r.path_params
        if n not in PARAM_KIND and n != "event_id"
    ]
    assert not unknown, f"add these path params to PARAM_KIND: {unknown}"
    assert len(_id_routes()) >= 40


def test_idor_sweep_foreign_ids_are_404(client, db, tenant_a, tenant_b):
    """Tenant A's owner uses tenant B's real ids on every id route: 403/404, no data, no side effects."""
    headers = tenant_a.world.headers()
    before = tenant_fingerprint(db, tenant_b.world.tenant.id)
    checked = 0
    for r in _id_routes():
        body = body_for(r.method, r.path, tenant_a)
        own_values = _param_values(r, tenant_a)[0]
        for foreign_values in _param_values(r, tenant_b):
            # all params foreign, then each param foreign on its own (e.g. own staff + foreign exception)
            variants = [foreign_values]
            if len(r.path_params) > 1:
                variants += [{**own_values, k: v} for k, v in foreign_values.items()]
            for values in variants:
                resp = client.request(r.method, _url(r, values), headers=headers, json=body)
                assert resp.status_code in (403, 404), (r.method, r.path, values, resp.status_code, resp.text)
                _assert_no_foreign_data(resp, tenant_b)
                checked += 1
        # control: the same request with A's own ids is valid, so the 404 above really is isolation
        control = client.request(r.method, _url(r, own_values), headers=headers, json=body)
        assert control.status_code in CONTROL_OK, (r.method, r.path, control.status_code, control.text)
    assert checked >= 45
    assert tenant_fingerprint(db, tenant_b.world.tenant.id) == before


def _foreign_query_values(name: str, b: Populated) -> list[str]:
    kind = {"visit_id": "visit"}.get(name) or PARAM_KIND.get(name)
    return b.ids.get(kind, [])[:2] if kind else []


def test_list_endpoints_never_return_foreign_rows(client, db, tenant_a, tenant_b):
    headers = tenant_a.world.headers()
    lists = [r for r in ROUTES if r.method == "GET" and not r.path_params and not is_public(r.path)]
    assert len(lists) >= 20
    base = {"service_id": tenant_a.one("service"), "date_from": future_day(3).isoformat()}

    def params_for(r):
        return base if r.path == "/v1/availability" else {}

    for r in lists:
        resp = client.get(r.path, headers=headers, params=params_for(r))
        assert resp.status_code == 200, (r.path, resp.status_code, resp.text)
        _assert_no_foreign_data(resp, tenant_b)
    # filters pointing at the other tenant's objects must not widen the result
    # (/v1/audit is skipped here: A's audit legitimately echoes the filter values A itself just sent)
    for r in lists:
        for q in r.query_params:
            for value in _foreign_query_values(q, tenant_b):
                if r.path == "/v1/audit":
                    continue
                resp = client.get(r.path, headers=headers, params={**params_for(r), q: value})
                assert resp.status_code < 500, (r.path, q, resp.text)
                _assert_no_foreign_data(resp, tenant_b)
    # and the other tenant does see its own data there (the checks above are meaningful)
    resp = client.get("/v1/bookings", headers=tenant_b.world.headers())
    assert tenant_b.one("booking") in resp.text


@pytest.mark.parametrize(
    "method,path,body",
    [
        (
            "POST",
            "/v1/bookings",
            lambda a, b: {
                "contact_id": b.one("contact"),
                "service_id": a.one("service"),
                "start_at": "2030-01-01T05:00:00+00:00",
            },
        ),
        (
            "POST",
            "/v1/bookings",
            lambda a, b: {
                "contact_id": a.one("contact"),
                "service_id": b.one("service"),
                "start_at": "2030-01-01T05:00:00+00:00",
            },
        ),
        (
            "POST",
            "/v1/bookings",
            lambda a, b: {
                "contact_id": a.one("contact"),
                "service_id": a.one("service"),
                "staff_id": b.one("staff"),
                "start_at": "2030-01-01T05:00:00+00:00",
            },
        ),
        (
            "POST",
            "/v1/bookings",
            lambda a, b: {
                "contact_id": a.one("contact"),
                "service_id": a.one("service"),
                "branch_id": b.one("branch"),
                "start_at": "2030-01-01T05:00:00+00:00",
            },
        ),
        (
            "POST",
            "/v1/bookings",
            lambda a, b: {
                "contact_id": a.one("contact"),
                "service_id": a.one("service"),
                "slot_id": f"{b.one('staff')}|2030-01-01T05:00:00+00:00|{a.one('service')}",
            },
        ),
        ("POST", "/v1/payments", lambda a, b: {"booking_id": b.one("booking"), "amount": "10"}),
        ("POST", "/v1/payments", lambda a, b: {"visit_id": b.one("visit"), "amount": "10"}),
        ("POST", "/v1/admin-tasks", lambda a, b: {"question": "q", "conversation_id": b.one("conversation")}),
        ("POST", "/v1/admin-tasks", lambda a, b: {"question": "q", "contact_id": b.one("contact")}),
        ("POST", "/v1/admin-tasks", lambda a, b: {"question": "q", "branch_id": b.one("branch")}),
        ("POST", "/v1/contacts/{a_contact}/merge", lambda a, b: {"other_id": b.one("contact")}),
        (
            "POST",
            "/v1/contacts",
            lambda a, b: {"name": "x", "phone": "+77075550123", "branch_id": b.one("branch")},
        ),
        ("PATCH", "/v1/contacts/{a_contact}", lambda a, b: {"branch_id": b.one("branch")}),
        ("PATCH", "/v1/contacts/{a_contact}", lambda a, b: {"owner_user_id": b.ids["user"][0]}),
        ("PATCH", "/v1/leads/{a_lead}", lambda a, b: {"owner_user_id": b.ids["user"][0]}),
        ("POST", "/v1/staff", lambda a, b: {"branch_id": b.one("branch"), "name": "x"}),
        (
            "POST",
            "/v1/staff",
            lambda a, b: {"branch_id": a.one("branch"), "name": "x", "user_id": b.ids["user"][3]},
        ),
        ("PATCH", "/v1/staff/{a_staff}", lambda a, b: {"service_ids": [b.one("service")]}),
        ("PATCH", "/v1/staff/{a_staff}", lambda a, b: {"branch_id": b.one("branch")}),
        (
            "POST",
            "/v1/services",
            lambda a, b: {"title_ru": "x", "duration_min": 30, "price": "1", "staff_ids": [b.one("staff")]},
        ),
        ("PATCH", "/v1/services/{a_service}", lambda a, b: {"staff_ids": [b.one("staff")]}),
        (
            "POST",
            "/v1/staff/shift-requests",
            lambda a, b: {"staff_id": b.one("staff"), "kind": "day_off", "date": future_day(30).isoformat()},
        ),
        ("PATCH", "/v1/team/{a_membership}", lambda a, b: {"branch_scope": [b.one("branch")]}),
    ],
)
def test_foreign_ids_in_request_body_are_rejected(client, db, tenant_a, tenant_b, method, path, body):
    before = tenant_fingerprint(db, tenant_b.world.tenant.id)
    url = path.format(
        a_contact=tenant_a.one("contact"),
        a_lead=tenant_a.one("lead"),
        a_staff=tenant_a.one("staff"),
        a_service=tenant_a.one("service"),
        a_membership=tenant_a.ids["membership"][1],
    )
    resp = client.request(method, url, headers=tenant_a.world.headers(), json=body(tenant_a, tenant_b))
    assert 400 <= resp.status_code < 500, (method, path, resp.status_code, resp.text)
    _assert_no_foreign_data(resp, tenant_b)
    assert tenant_fingerprint(db, tenant_b.world.tenant.id) == before


def test_team_cannot_hijack_other_tenants_user(client, db, tenant_a, tenant_b):
    """Adding someone else's account by e-mail must not expose or let A rewrite their profile (e.g. redirect
    tenant B's Telegram escalations to A's chat)."""
    victim = tenant_b.world.admin
    h = tenant_a.world.headers()
    resp = client.post(
        "/v1/team", headers=h, json={"email": victim.email, "password": "guessed-wrong", "role": "admin"}
    )
    assert resp.status_code == 409, resp.text
    assert victim.telegram_chat_id not in resp.text and (victim.phone_e164 or "") not in resp.text
    # a real multi-org user (knows the password) may be added, but A cannot rewrite the shared profile
    resp = client.post(
        "/v1/team", headers=h, json={"email": victim.email, "password": "password123", "role": "admin"}
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["membership_id"]
    for field, value in [("telegram_chat_id", "666"), ("phone_e164", "+77770000666"), ("full_name", "Evil")]:
        r = client.patch(f"/v1/team/{mid}", headers=h, json={field: value})
        assert r.status_code == 409, r.text
    # role/flags of A's own membership remain editable
    assert client.patch(f"/v1/team/{mid}", headers=h, json={"role": "analyst"}).status_code == 200
    db.expire_all()
    assert db.get(User, victim.id).telegram_chat_id == victim.telegram_chat_id


# ---------------------------------------------------------------- tokens & tenant switching
def test_tenant_switch_header_and_forged_tokens(client, db, tenant_a, tenant_b):
    a, b = tenant_a.world, tenant_b.world
    h = {**a.headers(), "X-Tenant-Id": str(b.tenant.id)}
    assert client.get("/v1/bookings", headers=h).status_code == 403
    denied = db.scalar(
        select(AuditLog).where(AuditLog.tenant_id == b.tenant.id, AuditLog.action == "access.denied")
    )
    assert denied is not None and denied.actor_id == str(a.owner.id)
    # a correctly signed token naming B's tenant still needs a membership
    tok = session_token(db, a.owner.id, b.tenant.id, "owner")
    assert client.get("/v1/bookings", headers={"Authorization": f"Bearer {tok}"}).status_code == 403

    now = datetime.now(UTC)
    claims = {"sub": str(b.owner.id), "tid": str(b.tenant.id), "role": "owner", "iat": now}
    forged = [
        jwt.encode(
            {**claims, "exp": now + timedelta(hours=1)}, "not-the-secret-" + "x" * 40, algorithm="HS256"
        ),
        jwt.encode(
            {**claims, "exp": now - timedelta(seconds=5)}, get_settings().jwt_secret, algorithm="HS256"
        ),
        jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256"),  # no expiry
        jwt.encode({**claims, "exp": now + timedelta(hours=1)}, None, algorithm="none"),
        "garbage",
    ]
    for t in forged:
        assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401, t


def test_deactivated_membership_loses_access(client, db, tenant_a):
    w = tenant_a.world
    h = w.headers(w.admin)
    m = db.scalar(select(Membership).where(Membership.user_id == w.admin.id))
    m.active = False
    db.commit()
    # v1.1.0: deactivating the membership also revokes the member's sessions of this tenant -> 401
    assert client.get("/v1/bookings", headers=h).status_code == 401
    # a still-active session not bound to this tenant is refused by the per-request membership check
    other = {
        "Authorization": f"Bearer {session_token(db, w.admin.id, None, None)}",
        "X-Tenant-Id": str(w.tenant.id),
    }
    assert client.get("/v1/bookings", headers=other).status_code == 403


# ---------------------------------------------------------------- RBAC matrix
_MANUAL_PERMS = {  # routes that check permissions in the handler instead of require(...)
    ("GET", "/v1/payments"): ["finance.read"],
    ("POST", "/v1/payments/{payment_id}/refund"): ["finance.read", "finance.write"],
    ("POST", "/v1/payments/{payment_id}/cancel"): ["finance.read", "finance.write"],
}
_AUTH_ONLY = {
    ("GET", "/v1/auth/me"),
    ("GET", "/v1/tenant"),
    # own sessions / password (v1.1.0): every authenticated user, scoped to ctx.user
    ("POST", "/v1/auth/logout"),
    ("POST", "/v1/auth/logout-all"),
    ("GET", "/v1/auth/sessions"),
    ("DELETE", "/v1/auth/sessions/{session_id}"),
    ("POST", "/v1/auth/change-password"),
}


def _required(r: RouteInfo) -> list[str]:
    if (r.method, r.path) in _MANUAL_PERMS:
        return _MANUAL_PERMS[(r.method, r.path)]
    perm = r.permission
    if perm is None:
        assert (r.method, r.path) in _AUTH_ONLY, f"route without a permission check: {r.method} {r.path}"
        return []
    return [perm]


def test_every_route_declares_a_known_permission():
    for r in ROUTES:
        if is_public(r.path):
            continue
        for perm in _required(r):
            assert perm in PERMISSIONS, (r.method, r.path, perm)


def test_role_matrix_enforces_permissions(client, db, tenant_a):
    w = tenant_a.world
    users = {"owner": w.owner, "admin": w.admin, "staff": w.staff_user, "analyst": w.analyst}
    own_values = {}
    for r in ROUTES:
        if r.path_params and r.path_params != ["kind"] and not is_public(r.path):
            own_values[(r.method, r.path)] = _param_values(r, tenant_a)[0]
    checked = 0
    for role, r in itertools.product(ROLES, ROUTES):
        if is_public(r.path):
            continue
        missing = [p for p in _required(r) if role not in PERMISSIONS[p]]
        if not missing:
            continue
        values = own_values.get((r.method, r.path), {})
        if r.path_params == ["kind"]:
            values = {"kind": "telegram"}
        resp = client.request(
            r.method,
            _url(r, values),
            headers=w.headers(users[role]),
            json=body_for(r.method, r.path, tenant_a),
        )
        assert resp.status_code == 403, (role, r.method, r.path, missing, resp.status_code, resp.text[:300])
        checked += 1
    assert checked >= 100


def test_staff_role_sees_only_own_calendar(client, db, tenant_a):
    w = tenant_a.world
    h = w.headers(w.staff_user)
    items = client.get("/v1/bookings", headers=h).json()["items"]
    assert all(b["staff"]["id"] == str(w.barber1.id) for b in items)
    for path in (f"/v1/staff/{w.barber2.id}/shifts", f"/v1/staff/{w.barber2.id}/exceptions"):
        assert client.get(path, headers=h).status_code == 403
    other = tenant_a.ids["booking"][0]  # barber of booking may differ; status change on foreign staff is 404
    r = client.patch(f"/v1/bookings/{other}", headers=h, json={"action": "cancel"})
    assert r.status_code in (403, 404)


def test_analyst_sees_masked_phones(client, db, tenant_a):
    w = tenant_a.world
    body = client.get("/v1/contacts", headers=w.headers(w.analyst)).text
    phones = [
        c.phone_e164
        for c in db.scalars(select(Contact).where(Contact.tenant_id == w.tenant.id))
        if c.phone_e164
    ]
    assert phones and not any(p in body for p in phones)


def test_unknown_ids_are_indistinguishable_from_foreign(client, tenant_a, tenant_b):
    h = tenant_a.world.headers()
    foreign = client.get(f"/v1/bookings/{tenant_b.one('booking')}", headers=h)
    missing = client.get(f"/v1/bookings/{uuid.uuid4()}", headers=h)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


# ---------------------------------------------------------------- branch scope: calls (v1.1)
def test_calls_respect_branch_scope_in_list_detail_and_report(client, db, world):
    """A branch-scoped admin does not see calls linked to a contact / booking / task of another branch."""
    from app.models import AdminTask, Booking, Branch, Call

    w = world
    b2 = Branch(tenant_id=w.tenant.id, name="Second", timezone="Asia/Almaty")
    db.add(b2)
    db.flush()
    c_in = Contact(tenant_id=w.tenant.id, name="In", phone_e164="+77015551001", branch_id=w.branch.id)
    c_out = Contact(tenant_id=w.tenant.id, name="Out", phone_e164="+77015551002", branch_id=b2.id)
    c_none = Contact(tenant_id=w.tenant.id, name="Any", phone_e164="+77015551003")
    db.add_all([c_in, c_out, c_none])
    db.flush()
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5)
    booking = Booking(
        number=900,
        tenant_id=w.tenant.id,
        branch_id=b2.id,
        contact_id=c_none.id,
        staff_id=w.barber2.id,
        start_at=start,
        end_at=start + timedelta(hours=1),
        occupied_start=start,
        occupied_end=start + timedelta(hours=1),
        status="confirmed",
    )
    task = AdminTask(tenant_id=w.tenant.id, number=900, question="Q", branch_id=b2.id)
    db.add_all([booking, task])
    db.flush()

    def call(**links) -> str:
        c = Call(
            tenant_id=w.tenant.id,
            direction="in",
            purpose="client_ai",
            to_number="+77270000001",
            status="completed",
            started_at=datetime.now(UTC),
            **links,
        )
        db.add(c)
        db.flush()
        return str(c.id)

    visible = {call(contact_id=c_in.id), call(contact_id=c_none.id), call()}
    hidden = {call(contact_id=c_out.id), call(contact_id=c_none.id, booking_id=booking.id), call(admin_task_id=task.id)}
    m = db.scalar(select(Membership).where(Membership.user_id == w.admin.id))
    m.branch_scope = [str(w.branch.id)]
    db.commit()
    admin, owner = w.headers(w.admin), w.headers()

    listing = client.get("/v1/calls", headers=admin).json()
    assert {i["id"] for i in listing["items"]} == visible and listing["total"] == 3
    full = client.get("/v1/calls", headers=owner).json()
    assert {i["id"] for i in full["items"]} == visible | hidden
    # filtering by an out-of-scope contact does not widen the result
    assert client.get("/v1/calls", headers=admin, params={"contact_id": str(c_out.id)}).json()["total"] == 0

    for cid in visible:
        assert client.get(f"/v1/calls/{cid}", headers=admin).status_code == 200
    unknown = client.get(f"/v1/calls/{uuid.uuid4()}", headers=admin)
    for cid in hidden:
        r = client.get(f"/v1/calls/{cid}", headers=admin)
        assert r.status_code == 404 and r.json() == unknown.json()  # indistinguishable from a missing call
        assert client.get(f"/v1/calls/{cid}", headers=owner).status_code == 200

    assert client.get("/v1/reports/voice", headers=admin).json()["calls"] == 3
    assert client.get("/v1/reports/voice", headers=owner).json()["calls"] == 6
