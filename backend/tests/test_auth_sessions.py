"""v1.1.0: server-side sessions and JWT revocation (docs/STAGE_v1.1_CONTRACT.md §A, docs/SECURITY.md)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, AuthSession, Membership, User
from app.services import auth_sessions
from app.services.common import utcnow
from app.workers.tasks import run_purge_auth_sessions

PASSWORD = "password123"


def _login(client, email="owner@test.kz", password=PASSWORD, ua="pytest-agent") -> dict:
    r = client.post("/v1/auth/login", json={"email": email, "password": password}, headers={"User-Agent": ua})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _sid(headers: dict) -> uuid.UUID:
    token = headers["Authorization"].split(" ", 1)[1]
    return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sid"])


def _ok(client, h) -> bool:
    r = client.get("/v1/auth/me", headers=h)
    assert r.status_code in (200, 401), r.text
    return r.status_code == 200


def _actions(db, action: str) -> list[AuditLog]:
    db.expire_all()
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


# ---------------------------------------------------------------- login / token binding
def test_login_creates_session_and_token_has_sid(client, db, world):
    h = _login(client, ua="Mozilla/5.0 " + "x" * 400)
    s = db.get(AuthSession, _sid(h))
    assert s is not None and s.user_id == world.owner.id and s.tenant_id == world.tenant.id
    assert s.revoked_at is None and s.expires_at > utcnow()
    assert s.ip == "testclient" and len(s.user_agent) == 300
    claims = jwt.decode(h["Authorization"][7:], options={"verify_signature": False})
    assert abs(claims["exp"] - s.expires_at.timestamp()) < 2
    assert _ok(client, h)


def test_legacy_token_without_sid_rejected(client, world):
    now = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": str(world.owner.id),
            "tid": str(world.tenant.id),
            "role": "owner",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401


def test_unknown_or_foreign_sid_rejected(client, db, world):
    from app.security import create_access_token

    unknown = create_access_token(str(world.owner.id), str(world.tenant.id), "owner", sid=str(uuid.uuid4()))
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {unknown}"}).status_code == 401
    garbage = create_access_token(str(world.owner.id), str(world.tenant.id), "owner", sid="not-a-uuid")
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {garbage}"}).status_code == 401
    # a validly signed token naming the admin's session for the owner is refused (sid is bound to sub)
    admin_sid = _sid(_login(client, "admin@test.kz"))
    stolen = create_access_token(str(world.owner.id), str(world.tenant.id), "owner", sid=str(admin_sid))
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {stolen}"}).status_code == 401


def test_expired_session_rejected(client, db, world):
    h = _login(client)
    s = db.get(AuthSession, _sid(h))
    s.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert not _ok(client, h)


def test_last_seen_is_throttled(client, db, world):
    h = _login(client)
    sid = _sid(h)
    old = utcnow() - timedelta(minutes=2)
    db.get(AuthSession, sid).last_seen_at = old
    db.commit()
    assert _ok(client, h)
    db.expire_all()
    assert db.get(AuthSession, sid).last_seen_at == old  # < 5 min: no write
    older = utcnow() - timedelta(minutes=6)
    db.get(AuthSession, sid).last_seen_at = older
    db.commit()
    assert _ok(client, h)
    db.expire_all()
    assert db.get(AuthSession, sid).last_seen_at > older + timedelta(minutes=5)


# ---------------------------------------------------------------- logout / logout-all / list / delete
def test_logout_revokes_only_current_session(client, db, world):
    h1, h2 = _login(client), _login(client)
    r = client.post("/v1/auth/logout", headers=h1)
    assert r.status_code == 204 and not r.content
    assert not _ok(client, h1) and _ok(client, h2)
    assert client.post("/v1/auth/logout", headers=h1).status_code == 401
    [a] = _actions(db, "auth.logout")
    assert a.entity_id == str(_sid(h1)) and a.actor_id == str(world.owner.id)


def test_logout_all_revokes_every_session_of_the_user(client, db, world):
    h1, h2, other = _login(client), _login(client), _login(client, "admin@test.kz")
    r = client.post("/v1/auth/logout-all", headers=h1)
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    assert not _ok(client, h1) and not _ok(client, h2)
    assert _ok(client, other)
    [a] = _actions(db, "auth.logout_all")
    assert a.payload == {"revoked": 2}


def test_list_sessions_only_active_own(client, db, world):
    h1, h2 = _login(client, ua="Phone"), _login(client, ua="Laptop")
    _login(client, "admin@test.kz")
    revoked = _login(client)
    client.post("/v1/auth/logout", headers=revoked)
    body = client.get("/v1/auth/sessions", headers=h1).json()
    assert body["total"] == 2 and len(body["items"]) == 2
    by_id = {i["id"]: i for i in body["items"]}
    assert set(by_id) == {str(_sid(h1)), str(_sid(h2))}
    assert by_id[str(_sid(h1))]["current"] is True and by_id[str(_sid(h2))]["current"] is False
    assert by_id[str(_sid(h2))]["user_agent"] == "Laptop" and by_id[str(_sid(h2))]["ip"] == "testclient"
    assert set(body["items"][0]) == {
        "id",
        "created_at",
        "last_seen_at",
        "expires_at",
        "ip",
        "user_agent",
        "current",
    }


def test_delete_session(client, db, world):
    h1, h2 = _login(client), _login(client)
    assert client.delete(f"/v1/auth/sessions/{_sid(h2)}", headers=h1).status_code == 204
    assert _ok(client, h1) and not _ok(client, h2)
    # already revoked / unknown -> 404
    assert client.delete(f"/v1/auth/sessions/{_sid(h2)}", headers=h1).status_code == 404
    assert client.delete(f"/v1/auth/sessions/{uuid.uuid4()}", headers=h1).status_code == 404
    [a] = _actions(db, "auth.session_revoked")
    assert a.entity_id == str(_sid(h2))
    # deleting the current session works like logout
    assert client.delete(f"/v1/auth/sessions/{_sid(h1)}", headers=h1).status_code == 204
    assert not _ok(client, h1)


def test_cross_user_session_delete_denied(client, db, world):
    owner, admin = _login(client), _login(client, "admin@test.kz")
    r = client.delete(f"/v1/auth/sessions/{_sid(owner)}", headers=admin)
    assert r.status_code == 404
    assert _ok(client, owner)


def test_cross_tenant_session_delete_denied(client, db, world, other_world):
    mine = _login(client)
    foreign = _login(client, other_world.owner.email)
    r = client.delete(f"/v1/auth/sessions/{_sid(mine)}", headers=foreign)
    assert r.status_code == 404
    unknown = client.delete(f"/v1/auth/sessions/{uuid.uuid4()}", headers=foreign)
    assert r.json() == unknown.json()
    assert _ok(client, mine)
    assert str(_sid(mine)) not in client.get("/v1/auth/sessions", headers=foreign).text


# ---------------------------------------------------------------- password change
def test_change_password_revokes_other_sessions(client, db, world):
    current, other, other2 = _login(client), _login(client), _login(client)
    admin = _login(client, "admin@test.kz")
    r = client.post(
        "/v1/auth/change-password",
        headers=current,
        json={"current_password": PASSWORD, "new_password": "new-secret-42"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked_other_sessions": 2}
    assert _ok(client, current) and not _ok(client, other) and not _ok(client, other2)
    assert _ok(client, admin)
    assert (
        client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": PASSWORD}).status_code
        == 401
    )
    _login(client, password="new-secret-42")
    [a] = _actions(db, "auth.password_changed")
    assert a.payload == {"revoked_other_sessions": 2}
    assert "new-secret-42" not in str(a.payload)


def test_change_password_errors(client, db, world):
    h = _login(client)
    other = _login(client)
    cases = [
        ({"current_password": "wrong-password", "new_password": "new-secret-42"}, "INVALID_CURRENT_PASSWORD"),
        ({"current_password": PASSWORD, "new_password": "short1"}, "WEAK_PASSWORD"),
        ({"current_password": PASSWORD, "new_password": "12345678901234"}, "WEAK_PASSWORD"),
    ]
    for body, code in cases:
        r = client.post("/v1/auth/change-password", headers=h, json=body)
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == code
    assert _ok(client, other)  # nothing revoked on failure
    assert _actions(db, "auth.password_changed") == []


# ---------------------------------------------------------------- deactivation & admin sign-out
def test_user_deactivation_revokes_sessions(client, db, world):
    h1, h2 = _login(client, "admin@test.kz"), _login(client, "admin@test.kz")
    keep = _login(client)
    user = db.get(User, world.admin.id)
    user.active = False
    db.commit()
    db.expire_all()
    rows = db.scalars(select(AuthSession).where(AuthSession.user_id == world.admin.id)).all()
    assert len(rows) == 2 and all(r.revoked_at is not None for r in rows)
    user.active = True  # reactivation does not resurrect old tokens
    db.commit()
    assert not _ok(client, h1) and not _ok(client, h2)
    assert _ok(client, keep)


def test_membership_deactivation_revokes_that_tenants_sessions(client, db, world):
    h = _login(client, "admin@test.kz")
    other_tenant_sid = db.get(AuthSession, _sid(_login(client, "admin@test.kz")))
    other_tenant_sid.tenant_id = None  # a session not bound to this organization stays
    db.commit()
    m = db.scalar(select(Membership).where(Membership.user_id == world.admin.id))
    r = client.patch(f"/v1/team/{m.id}", headers=world.headers(), json={"active": False})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(AuthSession, _sid(h)).revoked_at is not None
    assert db.get(AuthSession, other_tenant_sid.id).revoked_at is None
    m.active = True
    db.commit()
    assert not _ok(client, h)


def test_admin_signs_out_member(client, db, world):
    staff1, staff2 = _login(client, "barber@test.kz"), _login(client, "barber@test.kz")
    owner = _login(client)
    m = db.scalar(select(Membership).where(Membership.user_id == world.staff_user.id))
    admin_h = world.headers(world.admin)
    r = client.post(f"/v1/team/{m.id}/sign-out", headers=admin_h)
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    assert not _ok(client, staff1) and not _ok(client, staff2) and _ok(client, owner)
    [a] = _actions(db, "auth.user_signed_out")
    assert a.entity_id == str(m.id) and a.payload == {"user_id": str(world.staff_user.id), "revoked": 2}
    assert a.actor_id == str(world.admin.id)
    # nothing left to revoke
    assert client.post(f"/v1/team/{m.id}/sign-out", headers=admin_h).json() == {"revoked": 0}


def test_sign_out_permissions(client, db, world, other_world):
    owner_m = db.scalar(select(Membership).where(Membership.user_id == world.owner.id))
    admin_m = db.scalar(select(Membership).where(Membership.user_id == world.admin.id))
    for user in (world.staff_user, world.analyst):
        r = client.post(f"/v1/team/{admin_m.id}/sign-out", headers=world.headers(user))
        assert r.status_code == 403
    # an admin cannot sign out an owner
    owner_h = _login(client)
    r = client.post(f"/v1/team/{owner_m.id}/sign-out", headers=world.headers(world.admin))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "OWNER_PROTECTED"
    assert _ok(client, owner_h)
    # other tenant's membership -> 404, nothing revoked
    admin_h = _login(client, "admin@test.kz")
    r = client.post(f"/v1/team/{admin_m.id}/sign-out", headers=other_world.headers())
    assert r.status_code == 404
    assert _ok(client, admin_h)


def test_sign_out_self_keeps_current_session(client, db, world):
    current = world.headers()
    other = _login(client)
    owner_m = db.scalar(select(Membership).where(Membership.user_id == world.owner.id))
    r = client.post(f"/v1/team/{owner_m.id}/sign-out", headers=current)
    assert r.status_code == 200 and r.json() == {"revoked": 1}
    assert _ok(client, current) and not _ok(client, other)


# ---------------------------------------------------------------- purge job
def test_purge_job_deletes_old_expired_and_revoked_sessions(db, world):
    now = utcnow()

    def mk(expires_delta: timedelta, revoked_delta: timedelta | None = None) -> uuid.UUID:
        _, s = auth_sessions.create_session(db, world.owner.id, world.tenant.id, "owner", now=now)
        s.expires_at = now + expires_delta
        s.revoked_at = now + revoked_delta if revoked_delta is not None else None
        return s.id

    old_expired = mk(-timedelta(days=31))
    recent_expired = mk(-timedelta(days=29))
    old_revoked = mk(timedelta(hours=1), -timedelta(days=31))
    recent_revoked = mk(timedelta(hours=1), -timedelta(days=1))
    active = mk(timedelta(hours=1))
    db.commit()
    assert run_purge_auth_sessions(now=now) == 2
    db.expire_all()
    left = set(db.scalars(select(AuthSession.id)))
    assert old_expired not in left and old_revoked not in left
    assert {recent_expired, recent_revoked, active} <= left
