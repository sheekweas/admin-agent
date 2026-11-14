def test_login_and_me(client, world):
    r = client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": "password123"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "owner"
    assert "finance.read" in me.json()["permissions"]


def test_bad_password(client, world):
    r = client.post("/v1/auth/login", json={"email": "owner@test.kz", "password": "nope"})
    assert r.status_code == 401


def test_cross_tenant_denied_and_logged(client, world, other_world, db):
    """AT-11: user of tenant A cannot open tenant B; event is audited."""
    from app.models import AuditLog

    h = world.headers(world.staff_user)
    h["X-Tenant-Id"] = str(other_world.tenant.id)
    r = client.get("/v1/tenant", headers=h)
    assert r.status_code == 403
    assert db.query(AuditLog).filter_by(action="access.denied", tenant_id=other_world.tenant.id).count() == 1


def test_tenant_settings_defaults_and_patch(client, world):
    r = client.get("/v1/tenant", headers=world.headers())
    assert r.json()["settings"]["escalation"]["repeat_after_min"] == 5
    r = client.patch(
        "/v1/tenant", headers=world.headers(), json={"settings": {"escalation": {"repeat_after_min": 3}}}
    )
    assert r.status_code == 200
    assert r.json()["settings"]["escalation"]["repeat_after_min"] == 3
    assert r.json()["settings"]["escalation"]["call_after_min"] == 10
    r = client.patch("/v1/tenant", headers=world.headers(world.admin), json={"name": "x"})
    assert r.status_code == 403
