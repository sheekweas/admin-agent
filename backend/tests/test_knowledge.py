"""Knowledge base: status flow, versions, publish permission, AI uses only published, sandbox has no side effects."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import AdminTask, AIRun, Booking, Contact, Conversation, KnowledgeArticle, Message
from tests.ai_helpers import reply_text, say

PARKING = {
    "section": "faq",
    "language": "ru",
    "title": "Парковка",
    "content": "Бесплатная парковка во дворе, въезд с улицы Абая.",
    "test_questions": ["Есть ли парковка?"],
}


def _create(client, world, body=PARKING) -> dict:
    r = client.post("/v1/knowledge", headers=world.headers(world.admin), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_crud_publish_versions_archive(client, world, db):
    a = _create(client, world)
    assert a["status"] == "draft" and a["version"] == 1
    admin, owner = world.headers(world.admin), world.headers(world.owner)
    assert client.post(f"/v1/knowledge/{a['id']}/publish", headers=admin).status_code == 403  # owner only
    r = client.post(f"/v1/knowledge/{a['id']}/publish", headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert client.post(f"/v1/knowledge/{a['id']}/publish", headers=owner).status_code == 409

    r = client.patch(
        f"/v1/knowledge/{a['id']}", headers=admin, json={"content": "Парковка платная, 500 ₸/час."}
    )
    assert r.json()["version"] == 2 and r.json()["status"] == "draft"  # must be re-published
    r = client.patch(f"/v1/knowledge/{a['id']}", headers=admin, json={"test_questions": ["Где парковка?"]})
    assert r.json()["version"] == 2  # test questions do not bump content version

    listing = client.get("/v1/knowledge?status=draft", headers=admin).json()
    assert listing["total"] == 1 and {
        "id",
        "section",
        "language",
        "title",
        "content",
        "status",
        "version",
        "test_questions",
        "updated_at",
    } <= set(listing["items"][0])
    assert client.post(f"/v1/knowledge/{a['id']}/archive", headers=admin).json()["status"] == "archived"
    assert client.patch(f"/v1/knowledge/{a['id']}", headers=admin, json={"title": "x"}).status_code == 409
    for user in (world.staff_user, world.analyst):
        assert client.get("/v1/knowledge", headers=world.headers(user)).status_code == 403


def test_ai_uses_only_published_articles(client, world, db):
    a = _create(client, world)
    r = say(client, world, "Есть ли у вас парковка?", phone="+77015550001")
    assert "Бесплатная парковка" not in reply_text(r)  # draft is invisible to the AI -> escalated
    assert db.scalar(select(func.count(AdminTask.id))) == 1

    client.post(f"/v1/knowledge/{a['id']}/publish", headers=world.headers(world.owner))
    r = say(client, world, "Есть ли у вас парковка?", phone="+77015550009")
    assert "Бесплатная парковка во дворе" in reply_text(r)
    run = db.scalars(select(AIRun).order_by(AIRun.created_at.desc())).first()
    assert run.knowledge_article_ids == [a["id"]]


def test_tenant_isolation(client, world, other_world, db):
    a = _create(client, world)
    foreign = other_world.headers(other_world.owner)
    assert client.get(f"/v1/knowledge/{a['id']}", headers=foreign).status_code == 404
    assert client.post(f"/v1/knowledge/{a['id']}/publish", headers=foreign).status_code == 404
    client.post(f"/v1/knowledge/{a['id']}/publish", headers=world.headers(world.owner))
    r = client.post("/v1/knowledge/test", headers=foreign, json={"question": "Есть ли парковка?"})
    assert r.json()["used_articles"] == []


def test_sandbox_has_no_side_effects(client, world, db):
    a = _create(client, world)
    h = world.headers(world.admin)
    r = client.post(
        "/v1/knowledge/test", headers=h, json={"question": "Есть ли парковка?", "include_drafts": True}
    )
    assert r.status_code == 200
    body = r.json()
    assert "Бесплатная парковка" in body["reply"] and body["used_articles"][0]["id"] == a["id"]

    r = client.post("/v1/knowledge/test", headers=h, json={"question": "Есть ли парковка?"})
    assert r.json()["escalated"] is True and r.json()["used_articles"] == []

    r = client.post("/v1/knowledge/test", headers=h, json={"question": "Сколько стоит мужская стрижка?"})
    assert "5000" in r.json()["reply"]
    assert any(c["name"] == "list_services" for c in r.json()["tool_calls"])

    db.expire_all()
    for model in (Booking, AdminTask, Message, Conversation, AIRun):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model
    assert db.scalar(select(Contact.tags).where(Contact.id == world.contact.id)) == []
    assert db.get(KnowledgeArticle, a["id"]).status == "draft"
