"""Meta WhatsApp Cloud API webhook (ТЗ §6): verify, persist fast & idempotently, process AI in background."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.channels import whatsapp as wa
from app.config import get_settings
from app.db import SessionLocal
from app.services import messaging
from app.services.integrations import read_secrets

log = logging.getLogger("webhooks.whatsapp")
router = APIRouter()


@router.get("", response_class=PlainTextResponse)
def verify(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    if (
        mode == "subscribe"
        and token
        and token == get_settings().whatsapp_verify_token
        and challenge is not None
    ):
        return challenge
    raise HTTPException(403, "Verification failed")


def _signature_ok(body: bytes, header: str | None, payload: dict) -> bool:
    """Platform app secret first; otherwise a per-tenant app_secret stored with the integration."""
    if wa.verify_signature(body, header):
        return True
    pnids = {e.phone_number_id for e in wa.parse_webhook(payload)}
    if not pnids:
        return False
    from app.security import verify_meta_signature

    with SessionLocal() as db:
        for pnid in pnids:
            integ = wa.find_integration_by_phone_number_id(db, pnid)
            secret = read_secrets(integ).get("app_secret") if integ else None
            if secret and verify_meta_signature(secret, body, header):
                return True
    return False


def _ingest(payload: dict) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Persist messages/statuses; returns (conversation_id, message_id) pairs to process with AI."""
    to_process: list[tuple[uuid.UUID, uuid.UUID]] = []
    with SessionLocal() as db:
        for entry in wa.parse_webhook(payload):
            integ = wa.find_integration_by_phone_number_id(db, entry.phone_number_id)
            if integ is None:
                log.warning("webhook for unknown phone_number_id=%s ignored", entry.phone_number_id)
                continue
            for inbound in entry.messages:
                msg = messaging.handle_inbound(db, inbound.to_normalized(integ.tenant_id, integ.branch_id))
                if msg is not None:
                    to_process.append((msg.conversation_id, msg.id))
            for status in entry.statuses:
                messaging.apply_status_update(db, integ.tenant_id, status)
        db.commit()
    return to_process


def _process(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> None:
    for conversation_id, message_id in pairs:
        messaging.enqueue_ai_reply(conversation_id, message_id)


@router.post("")
async def receive(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payload")
    header = request.headers.get("x-hub-signature-256")
    if not await run_in_threadpool(_signature_ok, body, header, payload):
        log.warning("whatsapp webhook with invalid signature rejected")
        raise HTTPException(401, "Invalid signature")
    pairs = await run_in_threadpool(_ingest, payload)
    if pairs:
        background.add_task(_process, pairs)  # after the 200 is sent; Celery or inline
    return {"status": "ok", "accepted": len(pairs)}
