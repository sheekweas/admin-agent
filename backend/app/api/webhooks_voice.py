"""Voice provider callbacks (ТЗ §12.3, §26.4): call status + IVR digit (1 = take the task, 2 = backup).

POST /v1/webhooks/voice/{provider}/status?call_id=<our Call.id>
POST /v1/webhooks/voice/{provider}/gather?call_id=<our Call.id>
Twilio requests must carry a valid X-Twilio-Signature (no auth token configured -> rejected); the "mock"
provider is accepted only when settings.voice_provider == "mock".
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import OkOut
from app.channels.voice import (
    ACK_RU,
    FORWARD_RU,
    NO_INPUT_RU,
    final_twiml,
    gather_twiml,
    twilio_credentials,
    validate_twilio_signature,
)
from app.config import get_settings
from app.db import get_db
from app.models import AdminTask, Call, Membership, User
from app.services import escalation
from app.services.common import utcnow

router = APIRouter()
log = logging.getLogger("webhooks.voice")

TWILIO_STATUS = {
    "queued": "queued",
    "initiated": "queued",
    "ringing": "ringing",
    "in-progress": "answered",
    "answered": "answered",
    "completed": "completed",
    "busy": "no_answer",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "cancelled",
}
FINAL = ("completed", "no_answer", "failed", "cancelled")


async def _load(provider: str, request: Request, db: Session) -> tuple[Call, dict[str, str]]:
    if provider not in ("twilio", "mock") or (provider == "mock" and get_settings().voice_provider != "mock"):
        raise HTTPException(404, "Unknown provider")
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        call_id = uuid.UUID(request.query_params.get("call_id", ""))
    except ValueError as exc:
        raise HTTPException(404, "Unknown call") from exc
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(404, "Unknown call")
    if provider == "twilio":
        _, token, _ = twilio_credentials(db, call.tenant_id)
        base = get_settings().public_base_url.rstrip("/")
        url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        # fail closed: without an auth token there is nothing to verify the callback against
        if not token or not validate_twilio_signature(
            token, url, form, request.headers.get("x-twilio-signature")
        ):
            log.warning("voice callback with invalid/unverifiable signature rejected call=%s", call.id)
            raise HTTPException(403, "Bad signature")
        if call.provider_ref and form.get("CallSid") and form["CallSid"] != call.provider_ref:
            raise HTTPException(403, "Call mismatch")
    return call, form


def _xml(body: str) -> Response:
    return Response(body, media_type="application/xml")


@router.post(
    "/{provider}/status", response_model=OkOut, responses={403: {"description": "Bad X-Twilio-Signature"}}
)
async def voice_status(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    status = TWILIO_STATUS.get(form.get("CallStatus", ""), None)
    if status and call.status not in FINAL:
        call.status = status
    if form.get("CallDuration", "").isdigit():
        call.duration_sec = int(form["CallDuration"])
    if form.get("AnsweredBy", "").startswith("machine") and call.status not in FINAL:
        call.status = "no_answer"  # voicemail is not an answer
    task = db.get(AdminTask, call.admin_task_id) if call.admin_task_id else None
    if task is not None and task.status == "open" and call.status in ("no_answer", "failed"):
        escalation.request_backup(db, task, utcnow())  # next person on the next worker tick
    db.commit()
    return {"ok": True}


@router.post(
    "/{provider}/gather",
    response_class=Response,
    responses={200: {"content": {"application/xml": {"schema": {"type": "string"}}}, "description": "TwiML"}},
)
async def voice_gather(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    digit = (form.get("Digits") or "").strip()[:1]
    call.dtmf = digit or call.dtmf
    task = db.get(AdminTask, call.admin_task_id) if call.admin_task_id else None
    if task is None:
        db.commit()
        return _xml(final_twiml(NO_INPUT_RU))
    if digit == "1":
        user = db.scalar(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == call.tenant_id,
                Membership.active.is_(True),
                Membership.role.in_(("owner", "admin")),
                User.phone_e164 == call.to_number,
            )
        )
        escalation.ack_task(
            db, call.tenant_id, task.id, user.id if user else None, "voice", source_call_id=call.id
        )
        call.status = "answered"
        db.commit()
        return _xml(final_twiml(ACK_RU))
    if digit == "2":
        call.status = "completed"
        escalation.request_backup(db, task, utcnow(), explicit=True)
        db.commit()
        return _xml(final_twiml(FORWARD_RU))
    db.commit()
    return _xml(gather_twiml(provider, call.id))
