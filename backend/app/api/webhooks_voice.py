"""Voice provider webhooks (ТЗ §12.3, §13.2, §26.4).

Admin escalation IVR:
  POST /v1/webhooks/voice/{provider}/gather?call_id=   pressed digit: 1 = take the task, 2 = backup
AI calls with clients:
  POST /v1/webhooks/voice/{provider}/inbound            new inbound call (tenant = the called number)
  POST /v1/webhooks/voice/{provider}/answer?call_id=    our outbound call was answered
  POST /v1/webhooks/voice/{provider}/turn?call_id=&seq= one recognised utterance (SpeechResult, Confidence)
  POST /v1/webhooks/voice/{provider}/dial?call_id=      result of the transfer to an admin (DialCallStatus)
  POST /v1/webhooks/voice/{provider}/recording?call_id= recording ready (stored only with consent;
                                                        otherwise deleted at the provider)
Both:
  POST /v1/webhooks/voice/{provider}/status[?call_id=]  call status (without call_id: looked up by CallSid)

Twilio requests must carry a valid X-Twilio-Signature computed with the tenant's auth token (no token
configured -> rejected, fail closed); the "mock" provider is accepted only when settings.voice_provider ==
"mock". Per-IP rate limits apply to every /v1/webhooks/ path (app.middleware).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import OkOut
from app.channels.voice import (
    IVR_PHRASES,
    final_twiml,
    gather_twiml,
    resolve_tenant_by_number,
    twilio_credentials,
    validate_twilio_signature,
    voice_profile,
)
from app.config import get_settings
from app.db import get_db
from app.models import AdminTask, Call, Membership, User
from app.services import escalation, voice_calls
from app.services.common import utcnow

router = APIRouter()
log = logging.getLogger("webhooks.voice")

TWILIO_STATUS = {  # admin escalation calls
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
FINAL = ("completed", "no_answer", "busy", "failed", "cancelled")
TWIML = {200: {"content": {"application/xml": {"schema": {"type": "string"}}}, "description": "TwiML"}}
FORBIDDEN = {403: {"description": "Bad X-Twilio-Signature"}}


def _check_provider(provider: str) -> None:
    if provider not in ("twilio", "mock") or (provider == "mock" and get_settings().voice_provider != "mock"):
        raise HTTPException(404, "Unknown provider")


def _verify(provider: str, request: Request, db: Session, tenant_id: uuid.UUID, form: dict[str, str]) -> None:
    if provider != "twilio":
        return
    _, token, _ = twilio_credentials(db, tenant_id)
    base = get_settings().public_base_url.rstrip("/")
    url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
    # fail closed: without an auth token there is nothing to verify the callback against
    if not token or not validate_twilio_signature(
        token, url, form, request.headers.get("x-twilio-signature")
    ):
        log.warning("voice callback with invalid/unverifiable signature rejected path=%s", request.url.path)
        raise HTTPException(403, "Bad signature")


async def _form(request: Request) -> dict[str, str]:
    return {k: str(v) for k, v in (await request.form()).items()}


async def _load(provider: str, request: Request, db: Session) -> tuple[Call, dict[str, str]]:
    _check_provider(provider)
    form = await _form(request)
    raw_id = request.query_params.get("call_id")
    call: Call | None = None
    if raw_id is not None:
        try:
            call = db.get(Call, uuid.UUID(raw_id))
        except ValueError as exc:
            raise HTTPException(404, "Unknown call") from exc
    elif form.get("CallSid"):  # status callbacks configured on the phone number carry no call_id
        call = db.scalars(select(Call).where(Call.provider_ref == form["CallSid"])).first()
    if call is None:
        raise HTTPException(404, "Unknown call")
    _verify(provider, request, db, call.tenant_id, form)
    if (
        provider == "twilio"
        and call.provider_ref
        and form.get("CallSid")
        and form["CallSid"] != call.provider_ref
    ):
        raise HTTPException(403, "Call mismatch")
    return call, form


def _xml(body: str) -> Response:
    return Response(body, media_type="application/xml")


def _confidence(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------- shared
@router.post("/{provider}/status", response_model=OkOut, responses=FORBIDDEN)
async def voice_status(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    if call.purpose != "admin_escalation":
        voice_calls.apply_client_status(db, voice_calls.lock_call(db, call.id), form)
        db.commit()
        return {"ok": True}
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


# ---------------------------------------------------------------- admin escalation IVR
@router.post("/{provider}/gather", response_class=Response, responses={**TWIML, **FORBIDDEN})
async def voice_gather(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    lang = "kk" if call.language == "kk" else "ru"
    profile = voice_profile(voice_calls.voice_settings(db, call.tenant_id), lang)
    digit = (form.get("Digits") or "").strip()[:1]
    call.dtmf = digit or call.dtmf
    task = db.get(AdminTask, call.admin_task_id) if call.admin_task_id else None
    if task is None:
        db.commit()
        return _xml(final_twiml(IVR_PHRASES["no_input"][lang], profile))
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
        return _xml(final_twiml(IVR_PHRASES["ack"][lang], profile))
    if digit == "2":
        call.status = "completed"
        escalation.request_backup(db, task, utcnow(), explicit=True)
        db.commit()
        return _xml(final_twiml(IVR_PHRASES["forward"][lang], profile))
    db.commit()
    return _xml(gather_twiml(provider, call.id, lang, profile))


# ---------------------------------------------------------------- AI calls with clients
@router.post("/{provider}/inbound", response_class=Response, responses={**TWIML, **FORBIDDEN})
async def voice_inbound(provider: str, request: Request, db: Session = Depends(get_db)):
    _check_provider(provider)
    form = await _form(request)
    tenant_id = resolve_tenant_by_number(db, form.get("To") or form.get("Called"))
    if tenant_id is None:
        raise HTTPException(404, "Unknown number")
    _verify(provider, request, db, tenant_id, form)
    call_sid = (form.get("CallSid") or "").strip()[:200]
    if not call_sid:
        raise HTTPException(422, "CallSid is required")
    _, xml = voice_calls.start_inbound(
        db,
        tenant_id,
        provider=provider,
        call_sid=call_sid,
        from_number=form.get("From") or form.get("Caller"),
        to_number=form.get("To") or form.get("Called"),
    )
    db.commit()
    return _xml(xml)


@router.post("/{provider}/answer", response_class=Response, responses={**TWIML, **FORBIDDEN})
async def voice_answer(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    if call.direction != "out" or call.purpose == "admin_escalation":
        raise HTTPException(404, "Unknown call")
    xml = voice_calls.handle_answer(
        db, voice_calls.lock_call(db, call.id), answered_by=form.get("AnsweredBy")
    )
    db.commit()
    return _xml(xml)


@router.post("/{provider}/turn", response_class=Response, responses={**TWIML, **FORBIDDEN})
async def voice_turn(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    if call.purpose == "admin_escalation":
        raise HTTPException(404, "Unknown call")
    seq_raw = request.query_params.get("seq", "")
    xml = voice_calls.handle_turn(
        db,
        voice_calls.lock_call(db, call.id),
        seq=int(seq_raw) if seq_raw.isdigit() else None,
        speech=form.get("SpeechResult"),
        confidence=_confidence(form.get("Confidence")),
    )
    db.commit()
    return _xml(xml)


@router.post("/{provider}/dial", response_class=Response, responses={**TWIML, **FORBIDDEN})
async def voice_dial(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    if call.purpose == "admin_escalation":
        raise HTTPException(404, "Unknown call")
    xml = voice_calls.handle_dial_result(
        db,
        voice_calls.lock_call(db, call.id),
        dial_status=form.get("DialCallStatus"),
        dial_duration=form.get("DialCallDuration"),
    )
    db.commit()
    return _xml(xml)


@router.post("/{provider}/recording", response_model=OkOut, responses=FORBIDDEN)
async def voice_recording(provider: str, request: Request, db: Session = Depends(get_db)):
    call, form = await _load(provider, request, db)
    stored = voice_calls.apply_recording(db, voice_calls.lock_call(db, call.id), form)
    if not stored:
        log.info("recording for call %s not stored (no consent / not completed)", call.id)
    db.commit()
    return {"ok": True}
