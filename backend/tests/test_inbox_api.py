"""Inbox REST API: list/filter, messages, human reply, internal notes, takeover/release/read, RBAC, isolation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.channels import whatsapp as wa
from app.config import get_settings
from app.models import Contact, Conversation, Message
from tests.ai_helpers import say


@pytest.fixture(autouse=True)
def _outbox():
    wa.clear_mock_outbox()
    yield
    wa.clear_mock_outbox()


def test_list_and_filter_conversations(client, world, db):
    say(client, world, "Здравствуйте", phone="+77015550001", name="Айдар")
    say(client, world, "Сколько стоит окрашивание волос?", phone="+77015550002", name="Бекзат")  # escalates
    h = world.headers(world.admin)
    r = client.get("/v1/conversations", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    item = next(i for i in body["items"] if i["contact"]["name"] == "Бекзат")
    assert item["mode"] == "waiting_admin" and item["open_task_id"] and item["last_message"]["author"] == "ai"
    assert item["unread_count"] == 1
    assert client.get("/v1/conversations?mode=waiting_admin", headers=h).json()["total"] == 1
    assert client.get("/v1/conversations?q=Айдар", headers=h).json()["total"] == 1
    assert client.get("/v1/conversations?q=550002", headers=h).json()["total"] == 1
    assert client.get("/v1/conversations?unread=true", headers=h).json()["total"] == 2
    conv_id = item["id"]
    assert client.post(f"/v1/conversations/{conv_id}/read", headers=h).json()["unread_count"] == 0
    assert client.get("/v1/conversations?unread=false", headers=h).json()["total"] == 1


def test_messages_human_reply_and_internal_note(client, world, db):
    r = say(client, world, "Здравствуйте")
    conv_id = r["conversation_id"]
    h = world.headers(world.admin)
    wa.clear_mock_outbox()

    note = client.post(
        f"/v1/conversations/{conv_id}/messages", headers=h, json={"text": "VIP клиент", "internal_note": True}
    )
    assert note.status_code == 201 and note.json()["is_internal_note"] is True
    assert wa.mock_outbox() == []  # notes are never delivered
    db.expire_all()
    assert db.get(Conversation, conv_id).mode == "ai"  # a note does not take over

    reply = client.post(
        f"/v1/conversations/{conv_id}/messages", headers=h, json={"text": "Добрый день, это Алия"}
    )
    assert reply.status_code == 201
    assert reply.json()["author"] == "human" and reply.json()["provider_status"] == "sent"
    assert wa.mock_outbox()[0]["text"]["body"] == "Добрый день, это Алия"
    db.expire_all()
    conv = db.get(Conversation, conv_id)
    assert conv.mode == "human" and conv.owner_user_id == world.admin.id

    msgs = client.get(f"/v1/conversations/{conv_id}/messages", headers=h).json()
    assert msgs["total"] == 4
    assert [m["author"] for m in msgs["items"]] == ["client", "ai", "human", "human"]
    assert msgs["items"][0]["received_at"] <= msgs["items"][-1]["received_at"]


def test_takeover_release(client, world, db):
    conv_id = say(client, world, "Здравствуйте")["conversation_id"]
    h = world.headers(world.admin)
    assert client.post(f"/v1/conversations/{conv_id}/takeover", headers=h).json()["mode"] == "human"
    assert client.post(f"/v1/conversations/{conv_id}/release", headers=h).json()["mode"] == "ai"
    detail = client.get(f"/v1/conversations/{conv_id}", headers=h).json()
    assert detail["mode"] == "ai" and "state" in detail


def test_rbac_and_tenant_isolation(client, world, other_world, db):
    conv_id = say(client, world, "Здравствуйте")["conversation_id"]
    for user in (world.staff_user, world.analyst):
        assert client.get("/v1/conversations", headers=world.headers(user)).status_code == 403
        assert (
            client.post(f"/v1/conversations/{conv_id}/takeover", headers=world.headers(user)).status_code
            == 403
        )
    foreign = other_world.headers(other_world.owner)
    assert client.get(f"/v1/conversations/{conv_id}/messages", headers=foreign).status_code == 404
    assert (
        client.post(f"/v1/conversations/{conv_id}/messages", headers=foreign, json={"text": "x"}).status_code
        == 404
    )
    assert client.get("/v1/conversations", headers=foreign).json()["total"] == 0


def test_simulate_only_in_mock_mode(client, world, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    r = client.post(
        "/v1/conversations/simulate",
        headers=world.headers(world.admin),
        json={"phone": "+77015550001", "name": "A", "text": "Здравствуйте"},
    )
    assert r.status_code == 404
    assert db.scalar(select(Message)) is None


def test_simulate_reuses_existing_contact_by_phone(client, world, db):
    say(client, world, "Здравствуйте", phone="+77071234567", name="")  # phone of the World contact
    db.expire_all()
    contacts = db.scalars(select(Contact).where(Contact.phone_e164 == "+77071234567")).all()
    assert len(contacts) == 1 and contacts[0].id == world.contact.id
