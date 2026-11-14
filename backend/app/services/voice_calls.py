"""AI phone calls with clients (ТЗ §4.5, §13.2–13.4, §20.1) — provider-neutral dialogue loop.

Turn-based loop (Twilio Programmable Voice or MockVoice):
  inbound / answer  -> disclosure greeting (+ recording notice) + <Gather input="speech" bargeIn>
  turn (seq=N)      -> one utterance: low confidence / silence -> re-ask (AT-12); 2 consecutive failures or
                       "оператор / человек / адаммен…" -> transfer; otherwise ONE orchestrator turn in the
                       call's "voice" conversation (same prompt, tools and booking engine as WhatsApp; a booking
                       exists only after create_booking committed — ТЗ §13.2) -> <Say> + next <Gather>
  transfer          -> <Dial> the on-duty primary admin; outside hours / no phone / no answer -> admin task
                       kind="callback" and "we will call you back"
  status / hangup   -> Call status, duration, conversation closed, metrics finalised

Everything is stored: Call (status transitions, transcript with speaker labels, metrics, recording only with
consent), Messages (message_type="voice_turn") in the call's own voice conversation, AIRun per AI turn.
Functions never commit; the webhook / API owns the transaction. Every turn is idempotent by `seq`
(a retried webhook gets the same TwiML back without running the AI twice).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.channels.voice import (
    VoiceError,
    VoiceNotConfigured,
    VoiceProfile,
    VoiceResponse,
    callback_url,
    get_voice_provider,
    tenant_voice_number,
    voice_profile,
)
from app.config import get_settings
from app.models import (
    AdminTask,
    Booking,
    Call,
    Contact,
    ContactChannel,
    Conversation,
    Message,
    Service,
    Tenant,
)
from app.services import crm, escalation
from app.services import notifications as nt
from app.services.audit import audit
from app.services.common import DomainError, get_or_404, mask_phone, normalize_phone, utcnow
from app.services.tenant_settings import merged_settings

log = logging.getLogger("voice.calls")

OUTBOUND_PURPOSES = ("booking_confirmation", "reminder", "callback", "reschedule")
CLIENT_PURPOSES = ("client_ai", *OUTBOUND_PURPOSES)
FINAL_STATUSES = ("completed", "no_answer", "busy", "failed", "cancelled")
SPEAKERS = ("client", "ai", "admin", "system")
STALE_CALL_HOURS = 4

PHRASES: dict[str, dict[str, str]] = {
    "greeting": {
        "ru": "Здравствуйте! Вы позвонили в {tenant}. Я виртуальный помощник, искусственный интеллект. "
        "Подскажу цены и запишу вас на удобное время.",
        "kk": "Сәлеметсіз бе! Сіз {tenant} нөміріне қоңырау шалдыңыз. Мен виртуалды көмекшімін, жасанды "
        "интеллектпін. Бағаларды айтып, ыңғайлы уақытқа жазып бере аламын.",
    },
    "outbound_intro": {
        "ru": "Здравствуйте{name}! Это виртуальный помощник {tenant}, искусственный интеллект.",
        "kk": "Сәлеметсіз бе{name}! Бұл {tenant} виртуалды көмекшісі, жасанды интеллект.",
    },
    "recording": {
        "ru": "Разговор записывается для контроля качества.",
        "kk": "Сапаны бақылау үшін әңгіме жазылады.",
    },
    "how_help": {"ru": "Чем могу помочь?", "kk": "Қалай көмектесе аламын?"},
    "purpose_booking_confirmation": {
        "ru": "Звоню подтвердить вашу запись: {service}, {date} в {time}. Вы придёте?",
        "kk": "Жазылуыңызды растау үшін хабарласып тұрмын: {service}, {date}, сағат {time}. Келесіз бе?",
    },
    "purpose_reminder": {
        "ru": "Напоминаю о вашей записи: {service}, {date} в {time}. Если планы изменились, скажите "
        "«перенести» или «отменить».",
        "kk": "Жазылуыңыз туралы еске саламын: {service}, {date}, сағат {time}. Жоспар өзгерсе, «ауыстыру» "
        "немесе «бас тарту» деңіз.",
    },
    "purpose_callback": {
        "ru": "Вы просили перезвонить. Чем могу помочь?",
        "kk": "Сіз қайта хабарласуды сұрадыңыз. Қалай көмектесе аламын?",
    },
    "purpose_reschedule": {
        "ru": "Звоню уточнить перенос вашей записи: {service}, {date} в {time}. На какой день и время вам "
        "удобно?",
        "kk": "Жазылуыңызды ауыстыруды нақтылау үшін хабарласып тұрмын: {service}, {date}, сағат {time}. "
        "Қай күн мен уақыт ыңғайлы?",
    },
    "confirmed_thanks": {
        "ru": "Спасибо, ждём вас! До свидания.",
        "kk": "Рақмет, сізді күтеміз! Сау болыңыз.",
    },
    "reask": {
        "ru": "Извините, я вас не расслышала. Повторите, пожалуйста.",
        "kk": "Кешіріңіз, сізді анық естімедім. Қайталап айтыңызшы.",
    },
    "transfer": {
        "ru": "Соединяю вас с администратором, оставайтесь на линии.",
        "kk": "Сізді әкімшімен байланыстырамын, күте тұрыңыз.",
    },
    "callback": {
        "ru": "Сейчас администратор не может ответить. Мы перезвоним вам в ближайшее время. До свидания.",
        "kk": "Қазір әкімші жауап бере алмайды. Жақын арада сізге қайта хабарласамыз. Сау болыңыз.",
    },
    "error": {"ru": "Извините, произошла техническая ошибка.", "kk": "Кешіріңіз, техникалық қате орын алды."},
    "goodbye": {"ru": "Спасибо за звонок! До свидания.", "kk": "Қоңырауыңызға рақмет! Сау болыңыз."},
    "opt_out": {
        "ru": "Хорошо, мы больше не будем вам звонить и присылать рассылки. До свидания.",
        "kk": "Жарайды, енді сізге қоңырау шалмаймыз және жарнама жібермейміз. Сау болыңыз.",
    },
    "whatsapp_sent": {
        "ru": "Подтверждение отправим вам в WhatsApp.",
        "kk": "Растауды WhatsApp-қа жібереміз.",
    },
}
TRANSFER_REASONS = {
    "client_request": "клиент попросил соединить с человеком",
    "recognition_failed": "не удалось распознать речь клиента",
    "ai_escalated": "вопрос вне компетенции AI",
    "ai_error": "техническая ошибка AI",
    "ai_disabled": "AI-звонки отключены",
    "max_turns": "слишком длинный разговор с AI",
    "no_answer": "администратор не ответил на перевод звонка",
}
MONTHS = {
    "ru": (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ),
    "kk": (
        "қаңтар",
        "ақпан",
        "наурыз",
        "сәуір",
        "мамыр",
        "маусым",
        "шілде",
        "тамыз",
        "қыркүйек",
        "қазан",
        "қараша",
        "желтоқсан",
    ),
}

_HUMAN_RE = re.compile(
    r"оператор|администратор|менеджер|әкімші|адаммен|адамға|тірі адам|жив\w+ человек|с человеком",
    re.IGNORECASE,
)
_HUMAN_WORDS = {"человек", "человека", "адам", "оператора"}
_BYE_RE = re.compile(r"до свидания|всего доброго|сау болыңыз|сау бол|қош болыңыз", re.IGNORECASE)
_YES = {"да", "ага", "конечно", "приду", "подтверждаю", "верно", "иә", "ия", "келемін", "жарайды", "болады"}
_NO = {"нет", "не", "жоқ"}


def _t(key: str, lang: str, **kw: Any) -> str:
    return PHRASES[key]["kk" if lang == "kk" else "ru"].format(**kw)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text.lower().replace("ё", "е"))


def wants_human(text: str) -> bool:
    words = _words(text)
    return bool(_HUMAN_RE.search(text)) or (len(words) <= 3 and bool(set(words) & _HUMAN_WORDS))


def is_goodbye(text: str) -> bool:
    return bool(_BYE_RE.search(text)) and len(_words(text)) <= 6


def is_plain_yes(text: str) -> bool:
    words = set(_words(text))
    return bool(words & _YES) and not words & _NO and len(words) <= 5


def speakable(text: str, lang: str) -> str:
    """Make an AI chat reply suitable for TTS: natural dates, no symbols / ids / links (ТЗ §13.2)."""
    months = MONTHS["kk" if lang == "kk" else "ru"]

    def _date(m: re.Match[str]) -> str:
        day, month = int(m.group(1)), int(m.group(2))
        return f"{day} {months[month - 1]}" if 1 <= month <= 12 else m.group(0)

    t = re.sub(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)", _date, text)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\bBK-0*(\d+)\b", r"\1", t)
    t = t.replace("№", " номер " if lang != "kk" else " нөмірі ")
    t = t.replace("₸", " теңге" if lang == "kk" else " тенге")
    t = re.sub(r"[«»\"*_#`~<>\[\]{}|]", "", t)
    t = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s+", " ", t)).strip()
    if len(t) > 700:
        cut = t[:700]
        t = cut[: cut.rfind(".") + 1] or cut
    return t


# ====================================================================== state helpers
def voice_settings(db: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    return merged_settings(db.get(Tenant, tenant_id))["voice"]


def _lang(value: str | None) -> str:
    return "kk" if value == "kk" else "ru"


def empty_metrics() -> dict[str, Any]:
    return {
        "first_response_ms": None,
        "turns": 0,
        "recognition_retries": 0,
        "transferred_to_human": False,
        "booking_created": False,
    }


def _metrics(call: Call, **changes: Any) -> dict[str, Any]:
    m = {**empty_metrics(), **(call.metrics or {}), **changes}
    call.metrics = m
    return m


def _meta(call: Call, **changes: Any) -> dict[str, Any]:
    m = {**(call.meta or {}), **changes}
    call.meta = m
    return m


def _append(
    call: Call, speaker: str, text: str, *, confidence: float | None = None, at: datetime | None = None
):
    at = at or utcnow()
    items = list(call.transcript_items or [])
    items.append(
        {
            "speaker": speaker,
            "text": text,
            "at": at.isoformat(),
            "confidence": round(confidence, 3) if confidence is not None else None,
        }
    )
    call.transcript_items = items
    call.transcript = (call.transcript or "") + f"{speaker}: {text}\n"


def _message(
    db: Session,
    call: Call,
    direction: str,
    author: str,
    text: str,
    *,
    confidence: float | None = None,
    provider_message_id: str | None = None,
) -> Message | None:
    if call.conversation_id is None or not text:
        return None
    now = utcnow()
    msg = Message(
        tenant_id=call.tenant_id,
        conversation_id=call.conversation_id,
        provider_message_id=provider_message_id,
        direction=direction,
        author=author,
        message_type="voice_turn",
        text=text,
        language=call.language,
        provider_status="received" if direction == "in" else "sent",
        received_at=now,
        processed_at=now if direction == "out" else None,
        meta={"call_id": str(call.id), **({"confidence": confidence} if confidence is not None else {})},
    )
    db.add(msg)
    conv = db.get(Conversation, call.conversation_id)
    if conv is not None:
        conv.last_message_at = now
    db.flush()
    return msg


def _profile(db: Session, call: Call) -> VoiceProfile:
    return voice_profile(voice_settings(db, call.tenant_id), _lang(call.language))


def _provider_name(call: Call, fallback: str = "mock") -> str:
    return str((call.meta or {}).get("provider") or fallback)


def _respond(call: Call, xml: str) -> str:
    _meta(call, last_twiml=xml)
    return xml


def _listen(db: Session, call: Call, text: str) -> str:
    """Say `text` and wait for the next utterance (next seq)."""
    meta = call.meta or {}
    seq = int(meta.get("seq", 0)) + 1
    _meta(call, seq=seq)
    action = callback_url(_provider_name(call), "turn", call.id, seq=seq)
    return _respond(call, VoiceResponse().gather_speech(text, _profile(db, call), action).to_xml())


def _say_and_hangup(db: Session, call: Call, text: str, now: datetime) -> str:
    finish_call(db, call, now)
    return _respond(call, VoiceResponse().say(text, _profile(db, call)).hangup().to_xml())


def finish_call(db: Session, call: Call, now: datetime | None = None, status: str = "completed") -> None:
    """Final status, duration, metrics; the call's voice conversation is closed unless a human owns it."""
    now = now or utcnow()
    if call.status not in FINAL_STATUSES:
        call.status = status
    if call.ended_at is None:
        call.ended_at = now
    if call.started_at and not call.duration_sec:
        call.duration_sec = max(int((call.ended_at - call.started_at).total_seconds()), 1)
    _meta(call, ended=True, dialing=False)
    _metrics(call)
    conv = db.get(Conversation, call.conversation_id) if call.conversation_id else None
    if conv is not None and conv.mode == "ai":
        conv.status = "closed"


def lock_call(db: Session, call_id: uuid.UUID) -> Call:
    return db.scalars(
        select(Call).where(Call.id == call_id).with_for_update().execution_options(populate_existing=True)
    ).one()


# ====================================================================== inbound
def _new_conversation(db: Session, contact: Contact, lang: str, state: dict[str, Any] | None = None):
    conv = Conversation(
        tenant_id=contact.tenant_id,
        contact_id=contact.id,
        branch_id=contact.branch_id,
        channel="voice",
        mode="ai",
        status="open",
        language=lang,
        state=state or {},
        last_message_at=utcnow(),
    )
    db.add(conv)
    db.flush()
    return conv


def start_inbound(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    provider: str,
    call_sid: str,
    from_number: str | None,
    to_number: str | None,
    name: str | None = None,
    language: str | None = None,
    now: datetime | None = None,
) -> tuple[Call, str]:
    """Inbound AI call: contact + voice conversation + Call row, disclosure greeting and the first Gather."""
    now = now or utcnow()
    existing = db.scalar(select(Call).where(Call.tenant_id == tenant_id, Call.provider_ref == call_sid))
    if existing is not None:  # Twilio retried the webhook
        return existing, (existing.meta or {}).get("last_twiml") or VoiceResponse().hangup().to_xml()
    tenant = db.get(Tenant, tenant_id)
    assert tenant is not None
    cfg = merged_settings(tenant)
    vcfg = cfg["voice"]
    phone = normalize_phone(from_number) if from_number else None
    contact = crm.get_or_create_contact_by_channel(
        db,
        tenant_id,
        channel="voice",
        external_id=phone or f"anonymous:{call_sid}"[:100],
        phone=phone,
        name=name,
        language=None,
    )
    known = contact.last_contact_at is not None
    lang = language or (contact.language if known else None) or tenant.default_language
    lang = _lang(lang)
    contact.last_contact_at = now
    conv = _new_conversation(db, contact, lang)
    crm.open_or_get_lead(db, tenant_id, contact.id, source="call")
    recording = (
        bool(vcfg.get("recording_enabled"))
        and (contact.consent_flags or {}).get("call_recording") is not False
    )
    call = Call(
        tenant_id=tenant_id,
        direction="in",
        purpose="client_ai",
        from_number=(phone or (from_number or "anonymous"))[:30],
        to_number=(normalize_phone(to_number) if to_number else None) or (to_number or "")[:30],
        contact_id=contact.id,
        conversation_id=conv.id,
        status="in_progress",
        provider_ref=call_sid,
        language=lang,
        started_at=now,
        created_at=now,
        metrics=empty_metrics(),
        meta={"provider": provider, "seq": 0, "failures": 0, "recording": recording},
    )
    db.add(call)
    db.flush()
    greeting = _t("greeting", lang, tenant=tenant.name)
    if recording:
        greeting += " " + _t("recording", lang)
    from app.ai.orchestrator import ai_globally_disabled

    if not vcfg.get("ai_calls_enabled", True) or not cfg["ai"].get("enabled", True) or ai_globally_disabled():
        _append(call, "ai", greeting)
        _message(db, call, "out", "ai", greeting)
        return call, transfer(db, call, reason="ai_disabled", preface=greeting, now=now)
    greeting += " " + _t("how_help", lang)
    _append(call, "ai", greeting)
    _message(db, call, "out", "ai", greeting)
    return call, _listen(db, call, greeting)


# ====================================================================== outbound
def _booking_phrase_ctx(db: Session, booking: Booking | None, lang: str) -> dict[str, str]:
    if booking is None:
        return {"service": "", "date": "", "time": ""}
    zone = escalation.branch_zone(db, booking.tenant_id, booking.branch_id)
    local = booking.start_at.astimezone(zone)
    service = db.get(Service, booking.items[0].service_id) if booking.items else None
    title = ""
    if service is not None:
        title = (service.title_kk if lang == "kk" and service.title_kk else service.title_ru) or ""
    return {
        "service": title,
        "date": f"{local.day} {MONTHS[lang][local.month - 1]}",
        "time": f"{local:%H:%M}",
    }


def _next_booking(db: Session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> Booking | None:
    return db.scalars(
        select(Booking)
        .where(
            Booking.tenant_id == tenant_id,
            Booking.contact_id == contact_id,
            Booking.status.in_(("pending", "confirmed")),
            Booking.start_at > utcnow(),
        )
        .order_by(Booking.start_at)
        .limit(1)
    ).first()


def _local_day_start(db: Session, tenant_id: uuid.UUID, now: datetime) -> datetime:
    zone = escalation.branch_zone(db, tenant_id, None)
    local = now.astimezone(zone)
    return datetime.combine(local.date(), dtime(0), tzinfo=zone)


def place_outbound(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID,
    purpose: str,
    booking_id: uuid.UUID | None = None,
    admin_task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Call:
    """Outbound AI call to a client (ТЗ §13.3): consent, business hours and the daily cap are enforced."""
    now = now or utcnow()
    if purpose not in OUTBOUND_PURPOSES:
        raise DomainError(422, "BAD_PURPOSE", f"purpose must be one of {OUTBOUND_PURPOSES}")
    contact = get_or_404(db, Contact, contact_id, tenant_id)
    if contact.deleted_at is not None or contact.merged_into_id is not None:
        raise DomainError(404, "NOT_FOUND", "Contact not found")
    booking = get_or_404(db, Booking, booking_id, tenant_id) if booking_id else None
    if booking is not None and booking.contact_id != contact.id:
        raise DomainError(422, "BOOKING_CONTACT_MISMATCH", "The booking belongs to another client")
    task = get_or_404(db, AdminTask, admin_task_id, tenant_id) if admin_task_id else None
    flags = contact.consent_flags or {}
    if not flags.get("outbound_calls") or contact.opted_out:
        raise DomainError(409, "CONSENT_REQUIRED", "The client has not consented to outbound calls")
    if not contact.phone_e164:
        raise DomainError(422, "NO_PHONE", "The client has no phone number")
    if purpose != "callback" and booking is None:
        booking = _next_booking(db, tenant_id, contact.id)
        if booking is None:
            raise DomainError(422, "BOOKING_REQUIRED", "The client has no upcoming booking for this call")
    tenant = db.get(Tenant, tenant_id)
    cfg = merged_settings(tenant)
    vcfg = cfg["voice"]
    zone = escalation.branch_zone(db, tenant_id, booking.branch_id if booking else contact.branch_id)
    local = now.astimezone(zone)
    hours = vcfg.get("outbound_hours") or [10, 20]
    in_window = int(hours[0]) <= local.hour < int(hours[1])
    if not in_window or escalation.in_quiet_hours(local, cfg["escalation"].get("quiet_hours")):
        raise DomainError(
            409, "OUTSIDE_HOURS", f"Outbound calls are allowed {int(hours[0]):02d}:00–{int(hours[1]):02d}:00"
        )
    # serialise the cap check per tenant (two admins pressing "call" at once must not exceed it)
    db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    placed_today = db.scalar(
        select(func.count(Call.id)).where(
            Call.tenant_id == tenant_id,
            Call.direction == "out",
            Call.purpose.in_(OUTBOUND_PURPOSES),
            Call.created_at >= _local_day_start(db, tenant_id, now),
        )
    )
    if (placed_today or 0) >= int(vcfg.get("max_outbound_per_day") or 0):
        raise DomainError(409, "DAILY_CAP_REACHED", "The daily limit of outbound calls is reached")
    try:
        provider = get_voice_provider(db, tenant_id)
    except VoiceNotConfigured as exc:
        raise DomainError(503, "VOICE_NOT_CONFIGURED", str(exc)) from exc

    lang = _lang(contact.language)
    state: dict[str, Any] = {"call_purpose": purpose}
    if booking is not None:
        bctx = _booking_phrase_ctx(db, booking, lang)
        state["booking_number"] = nt.booking_number(booking.number)
        if purpose == "reschedule":
            service_id = booking.items[0].service_id if booking.items else None
            state.update(intent="reschedule", reschedule_number=state["booking_number"], awaiting="date")
            if service_id:
                state.update(service_id=str(service_id), service_title=bctx["service"])
    conv = _new_conversation(db, contact, lang, state)
    call = Call(
        tenant_id=tenant_id,
        direction="out",
        purpose=purpose,
        to_number=contact.phone_e164,
        from_number=tenant_voice_number(db, tenant_id),
        contact_id=contact.id,
        booking_id=booking.id if booking else None,
        admin_task_id=task.id if task else None,
        conversation_id=conv.id,
        status="queued",
        language=lang,
        created_at=now,
        metrics=empty_metrics(),
        meta={
            "provider": provider.provider,
            "seq": 0,
            "failures": 0,
            # outbound: recording needs an explicit consent flag
            "recording": bool(vcfg.get("recording_enabled")) and flags.get("call_recording") is True,
            "requested_by": str(user_id) if user_id else None,
        },
    )
    db.add(call)
    db.flush()
    try:
        placed = provider.place_call(
            contact.phone_e164,
            call_id=call.id,
            answer_url=callback_url(provider.provider, "answer", call.id),
            machine_detection=True,
        )
        call.provider_ref, call.status = placed.provider_ref, placed.status
    except (VoiceError, VoiceNotConfigured) as exc:
        call.status, call.ended_at = "failed", now
        _meta(call, error=str(exc)[:300])
        conv.status = "closed"
        log.warning("outbound call failed call=%s err=%s", call.id, exc)
    audit(
        db,
        tenant_id,
        "user" if user_id else "system",
        str(user_id) if user_id else None,
        "call.outbound",
        "call",
        str(call.id),
        {"purpose": purpose, "to": mask_phone(contact.phone_e164), "status": call.status},
    )
    return call


def handle_answer(
    db: Session, call: Call, *, answered_by: str | None = None, now: datetime | None = None
) -> str:
    """Outbound call answered: disclosure + purpose-specific opening, then the same turn loop."""
    now = now or utcnow()
    meta = call.meta or {}
    if meta.get("answered"):
        return meta.get("last_twiml") or VoiceResponse().hangup().to_xml()
    if (answered_by or "").startswith("machine") or answered_by == "fax":
        # voicemail: nothing is left on the machine (no client data on a shared device)
        call.status = "no_answer"
        _append(call, "system", "answering machine — hung up")
        finish_call(db, call, now, status="no_answer")
        return _respond(call, VoiceResponse().hangup().to_xml())
    _meta(call, answered=True)
    call.status, call.started_at = "in_progress", call.started_at or now
    tenant = db.get(Tenant, call.tenant_id)
    lang = _lang(call.language)
    contact = db.get(Contact, call.contact_id) if call.contact_id else None
    booking = db.get(Booking, call.booking_id) if call.booking_id else None
    first_name = (contact.name or "").split(" ")[0] if contact and contact.name else ""
    text = _t("outbound_intro", lang, name=f", {first_name}" if first_name else "", tenant=tenant.name)
    if meta.get("recording"):
        text += " " + _t("recording", lang)
    text += " " + _t(f"purpose_{call.purpose}", lang, **_booking_phrase_ctx(db, booking, lang))
    _append(call, "ai", text, at=now)
    _message(db, call, "out", "ai", text)
    return _listen(db, call, text)


# ====================================================================== turns
def _maybe_start_recording(db: Session, call: Call) -> None:
    meta = call.meta or {}
    if not meta.get("recording") or meta.get("recording_started") or not call.provider_ref:
        return
    _meta(call, recording_started=True)
    try:
        get_voice_provider(db, call.tenant_id).start_recording(call.provider_ref, call_id=call.id)
    except (VoiceError, VoiceNotConfigured) as exc:
        log.warning("could not start recording call=%s err=%s", call.id, exc)


def _booking_from_tools(
    db: Session, tenant_id: uuid.UUID, tool_calls: list[dict[str, Any]]
) -> Booking | None:
    for c in reversed(tool_calls):
        if c.get("ok") and c.get("name") in ("create_booking", "reschedule_booking"):
            number = str((c.get("result") or {}).get("booking_number") or "")
            m = re.match(r"^BK-(\d+)$", number)
            if m:
                return db.scalar(
                    select(Booking).where(Booking.tenant_id == tenant_id, Booking.number == int(m.group(1)))
                )
    return None


def queue_written_confirmation(db: Session, booking: Booking) -> bool:
    """ТЗ §13.2: written confirmation goes to WhatsApp when the client uses WhatsApp and allowed it."""
    contact = db.get(Contact, booking.contact_id)
    if contact is None or contact.deleted_at is not None:
        return False
    flags = contact.consent_flags or {}
    if not flags.get("processing") or flags.get("whatsapp") is False:
        return False
    has_wa = db.scalar(
        select(ContactChannel.id).where(
            ContactChannel.tenant_id == booking.tenant_id,
            ContactChannel.contact_id == contact.id,
            ContactChannel.channel == "whatsapp",
        )
    )
    if not has_wa:
        return False
    nt.queue_client_notification(
        db,
        booking.tenant_id,
        kind="booking_confirmation",
        contact_id=contact.id,
        payload={"booking_id": str(booking.id), "source": "voice"},
        dedupe_key=f"confirm:{booking.id}",
    )
    return True


def handle_turn(
    db: Session,
    call: Call,
    *,
    seq: int | None,
    speech: str | None,
    confidence: float | None,
    now: datetime | None = None,
) -> str:
    """One caller utterance -> TwiML. Caller holds the Call row lock (lock_call) and commits afterwards."""
    started = time.perf_counter()
    now = now or utcnow()
    meta = call.meta or {}
    if call.status in FINAL_STATUSES or meta.get("ended"):
        return VoiceResponse().hangup().to_xml()
    if seq is not None and seq < int(meta.get("seq", 0)):
        return meta.get("last_twiml") or VoiceResponse().hangup().to_xml()  # retried webhook
    xml = _turn(db, call, seq=seq, speech=(speech or "").strip()[:1000], confidence=confidence, now=now)
    if _metrics(call)["first_response_ms"] is None:
        _metrics(call, first_response_ms=int((time.perf_counter() - started) * 1000))
    return xml


def _turn(
    db: Session, call: Call, *, seq: int | None, speech: str, confidence: float | None, now: datetime
) -> str:
    vcfg = voice_settings(db, call.tenant_id)
    lang = _lang(call.language)
    meta = call.meta or {}
    metrics = _metrics(call)
    _maybe_start_recording(db, call)
    threshold = float(vcfg.get("low_confidence_threshold") or 0)
    if not speech or (confidence is not None and confidence < threshold):
        failures = int(meta.get("failures", 0)) + 1
        _meta(call, failures=failures)
        _metrics(call, recognition_retries=int(metrics["recognition_retries"]) + 1)
        if speech:
            _append(call, "client", speech, confidence=confidence, at=now)
        else:
            _append(call, "system", "no speech recognised", at=now)
        if failures >= int(vcfg.get("max_recognition_failures") or 2):
            return transfer(db, call, reason="recognition_failed", now=now)
        reask = _t("reask", lang)
        _append(call, "ai", reask)
        return _listen(db, call, reask)

    _meta(call, failures=0)
    _metrics(call, turns=int(metrics["turns"]) + 1)
    _append(call, "client", speech, confidence=confidence, at=now)
    trigger = _message(
        db,
        call,
        "in",
        "client",
        speech,
        confidence=confidence,
        provider_message_id=f"call:{call.provider_ref or call.id}:{seq if seq is not None else uuid.uuid4().hex}"[
            :200
        ],
    )
    conv = db.get(Conversation, call.conversation_id) if call.conversation_id else None
    if wants_human(speech):
        return transfer(db, call, reason="client_request", now=now)
    from app.services.messaging import detect_opt_out

    if detect_opt_out(speech) and call.contact_id:
        crm.set_opt_out(db, call.tenant_id, call.contact_id, True, actor_type="client", actor_id=None)
        text = _t("opt_out", lang)
        _append(call, "ai", text)
        _message(db, call, "out", "ai", text)
        return _say_and_hangup(db, call, text, now)
    if is_goodbye(speech):
        text = _t("goodbye", lang)
        _append(call, "ai", text)
        _message(db, call, "out", "ai", text)
        return _say_and_hangup(db, call, text, now)
    if (
        call.purpose in ("booking_confirmation", "reminder")
        and metrics["turns"] == 0
        and is_plain_yes(speech)
    ):
        text = _t("confirmed_thanks", lang)
        _metrics(call, client_confirmed=True)
        _append(call, "ai", text)
        _message(db, call, "out", "ai", text)
        return _say_and_hangup(db, call, text, now)
    tenant = db.get(Tenant, call.tenant_id)
    if (
        conv is None
        or trigger is None
        or conv.mode != "ai"
        or not merged_settings(tenant)["ai"].get("enabled")
    ):
        return transfer(db, call, reason="ai_disabled", now=now)
    if int(metrics["turns"]) + 1 > int(vcfg.get("max_turns") or 30):
        return transfer(db, call, reason="max_turns", now=now)

    from app.ai import orchestrator

    sp = db.begin_nested()
    try:
        outcome = orchestrator.run_turn(db, conv, trigger)
        sp.commit()
    except Exception as exc:  # noqa: BLE001 - any AI failure -> honest message + human
        sp.rollback()
        log.warning("voice AI turn failed call=%s: %s", call.id, exc)
        db.refresh(conv)
        db.refresh(trigger)
        orchestrator.record_failed_run(db, conv, trigger, exc)
        return transfer(db, call, reason="ai_error", preface=_t("error", lang), now=now)
    trigger.processed_at = utcnow()
    lang = _lang(outcome.language)
    call.language = lang
    reply = speakable(outcome.reply_text, lang)
    if outcome.booking_done:
        booking = _booking_from_tools(db, call.tenant_id, outcome.tool_calls)
        if booking is not None:
            call.booking_id = booking.id
            created = any(c.get("ok") and c.get("name") == "create_booking" for c in outcome.tool_calls)
            if created:
                _metrics(call, booking_created=True)
            # only confirmed bookings: for a pending one the client was told an admin will confirm it
            if booking.status == "confirmed" and queue_written_confirmation(db, booking):
                reply = f"{reply} {_t('whatsapp_sent', lang)}".strip()
                _metrics(call, whatsapp_confirmation=True)
    _append(call, "ai", reply)
    _message(db, call, "out", "ai", reply)
    if outcome.escalated:
        return transfer(db, call, reason="ai_escalated", preface=reply, now=now)
    return _listen(db, call, reply)


# ====================================================================== transfer / callback
def _on_duty_admin_phone(
    db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID | None, now: datetime
) -> str | None:
    esc = escalation.escalation_settings(db, tenant_id)
    local = now.astimezone(escalation.branch_zone(db, tenant_id, branch_id))
    if escalation.in_quiet_hours(local, esc.get("quiet_hours")):
        return None
    for _, user in nt.escalation_members(db, tenant_id, branch_id, "primary"):
        if user.phone_e164:
            return user.phone_e164
    return None


def _callback_task(
    db: Session, call: Call, reason: str, now: datetime, *, admin_missed: bool = False
) -> AdminTask:
    if call.admin_task_id:
        task = db.get(AdminTask, call.admin_task_id)
        if task is not None and task.status != "resolved":
            return task
    if call.conversation_id:  # the AI already escalated this question during the call
        task = db.scalar(
            select(AdminTask).where(
                AdminTask.tenant_id == call.tenant_id,
                AdminTask.conversation_id == call.conversation_id,
                AdminTask.status.in_(("open", "acked")),
            )
        )
        if task is not None:
            call.admin_task_id = task.id
            return task
    last = next(
        (i["text"] for i in reversed(call.transcript_items or []) if i.get("speaker") == "client"), ""
    )
    why = TRANSFER_REASONS.get(reason, reason)
    if admin_missed and reason != "no_answer":
        why += "; " + TRANSFER_REASONS["no_answer"]
    question = f"Перезвонить клиенту после голосового звонка: {why}."
    if last:
        question += f" Последняя фраза клиента: «{last[:300]}»"
    task = escalation.create_admin_task(
        db,
        call.tenant_id,
        question=question,
        conversation_id=call.conversation_id,
        contact_id=call.contact_id,
        kind="callback",
        priority="high",
        payload={"source": "voice", "call_id": str(call.id), "reason": reason, "admin_missed": admin_missed},
        now=now,
    )
    call.admin_task_id = task.id
    if call.contact_id:
        crm.add_contact_tags(db, call.tenant_id, call.contact_id, ["просит перезвонить"])
    return task


def transfer(
    db: Session,
    call: Call,
    *,
    reason: str,
    preface: str = "",
    now: datetime | None = None,
) -> str:
    """Hand the caller to a human: <Dial> the on-duty admin, or a callback task when nobody can answer."""
    now = now or utcnow()
    lang = _lang(call.language)
    _metrics(call, transferred_to_human=True)
    _meta(call, transfer_reason=reason)
    conv = db.get(Conversation, call.conversation_id) if call.conversation_id else None
    branch_id = conv.branch_id if conv else None
    target = _on_duty_admin_phone(db, call.tenant_id, branch_id, now)
    if conv is not None:
        conv.mode = "waiting_admin"
    if target:
        text = " ".join(x for x in (preface, _t("transfer", lang)) if x)
        _append(call, "system", f"transfer to admin {mask_phone(target)} ({reason})", at=now)
        _append(call, "ai", _t("transfer", lang))
        _message(db, call, "out", "system", _t("transfer", lang))
        _meta(call, dialing=True, dial_to=mask_phone(target))
        timeout = int(voice_settings(db, call.tenant_id).get("transfer_timeout_sec") or 20)
        action = callback_url(_provider_name(call), "dial", call.id)
        xml = (
            VoiceResponse()
            .say(text, _profile(db, call))
            .dial(target, action=action, timeout=timeout)
            .to_xml()
        )
        return _respond(call, xml)
    task = _callback_task(db, call, reason, now)
    _append(call, "system", f"callback task #{task.number} ({reason})", at=now)
    text = " ".join(x for x in (preface, _t("callback", lang)) if x)
    _append(call, "ai", _t("callback", lang))
    _message(db, call, "out", "system", _t("callback", lang))
    return _say_and_hangup(db, call, text, now)


def handle_dial_result(
    db: Session,
    call: Call,
    *,
    dial_status: str | None,
    dial_duration: str | None = None,
    now: datetime | None = None,
) -> str:
    now = now or utcnow()
    meta = call.meta or {}
    if not meta.get("dialing"):
        return meta.get("last_twiml") or VoiceResponse().hangup().to_xml()
    _meta(call, dialing=False)
    if dial_status in ("completed", "answered"):
        _metrics(call, human_answered=True)
        secs = int(dial_duration) if (dial_duration or "").isdigit() else None
        _append(call, "admin", "разговор с администратором" + (f" ({secs} с)" if secs else ""), at=now)
        finish_call(db, call, now)
        return _respond(call, VoiceResponse().hangup().to_xml())
    _append(call, "system", f"admin did not answer the transfer ({dial_status or 'unknown'})", at=now)
    task = _callback_task(db, call, str(meta.get("transfer_reason") or "no_answer"), now, admin_missed=True)
    _append(call, "system", f"callback task #{task.number} (no_answer)", at=now)
    text = _t("callback", _lang(call.language))
    _append(call, "ai", text)
    _message(db, call, "out", "system", text)
    return _say_and_hangup(db, call, text, now)


# ====================================================================== provider status / recording
CLIENT_STATUS = {
    "queued": "queued",
    "initiated": "queued",
    "ringing": "ringing",
    "in-progress": "in_progress",
    "answered": "in_progress",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "cancelled",
}


def apply_client_status(db: Session, call: Call, form: dict[str, str], now: datetime | None = None) -> None:
    now = now or utcnow()
    status = CLIENT_STATUS.get(form.get("CallStatus", ""))
    if form.get("CallDuration", "").isdigit():
        call.duration_sec = int(form["CallDuration"])
    if status is None or call.status in FINAL_STATUSES:
        return
    if status == "in_progress":
        call.status = "in_progress"
        call.started_at = call.started_at or now
    elif status in FINAL_STATUSES:
        if status == "completed" and call.started_at is None:
            status = "no_answer"  # completed without ever being answered
        _append(call, "system", f"call {status}", at=now)
        finish_call(db, call, now, status=status)
    else:
        call.status = status


def apply_recording(call: Call, form: dict[str, str]) -> bool:
    """Store the recording URL only when the call was recorded with consent (§20.1)."""
    if not (call.meta or {}).get("recording"):
        return False
    if form.get("RecordingStatus", "completed") != "completed" or not form.get("RecordingUrl"):
        return False
    call.recording_url = form["RecordingUrl"][:1000]
    return True


# ====================================================================== simulator (mock mode)
def simulate_call(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    phone: str,
    name: str | None,
    utterances: list[str],
    language: str | None,
) -> Call:
    """Full inbound call through the same functions the webhooks use (mock provider only). Commits."""
    if get_settings().voice_provider != "mock":
        raise DomainError(404, "NOT_FOUND", "Call simulator is available only in voice mock mode")
    number = normalize_phone(phone)
    if number is None:
        raise DomainError(422, "BAD_PHONE", "Invalid phone number")
    call, _ = start_inbound(
        db,
        tenant_id,
        provider="mock",
        call_sid=f"SIM{uuid.uuid4().hex}",
        from_number=number,
        to_number=tenant_voice_number(db, tenant_id) or "+77000000000",
        name=name or None,
        language=language,
    )
    db.commit()
    for text in utterances:
        call = lock_call(db, call.id)
        meta = call.meta or {}
        if meta.get("ended") or call.status in FINAL_STATUSES:
            db.commit()
            break
        if meta.get("dialing"):  # simulated admin does not pick up -> callback task
            handle_dial_result(db, call, dial_status="no-answer")
            db.commit()
            break
        handle_turn(
            db, call, seq=int(meta.get("seq", 0)), speech=text, confidence=0.95 if text.strip() else None
        )
        db.commit()
    call = lock_call(db, call.id)
    if (call.meta or {}).get("dialing"):
        handle_dial_result(db, call, dial_status="no-answer")
    if not (call.meta or {}).get("ended"):
        _append(call, "system", "caller hung up")
        finish_call(db, call)
    db.commit()
    return call


# ====================================================================== read models (API)
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def call_items(db: Session, calls: list[Call], *, mask: bool = False) -> list[dict[str, Any]]:
    ids = {c.contact_id for c in calls if c.contact_id}
    contacts = {c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_(ids)))} if ids else {}

    def num(v: str | None) -> str | None:
        return mask_phone(v) if mask and v else v

    out = []
    for c in calls:
        contact = contacts.get(c.contact_id) if c.contact_id else None
        out.append(
            {
                "id": c.id,
                "direction": c.direction,
                "purpose": c.purpose,
                "status": c.status,
                "from_number": num(c.from_number),
                "to_number": num(c.to_number),
                "contact": {"id": contact.id, "name": contact.name, "phone_e164": num(contact.phone_e164)}
                if contact
                else None,
                "admin_task_id": c.admin_task_id,
                "booking_id": c.booking_id,
                "started_at": _iso(c.started_at),
                "ended_at": _iso(c.ended_at),
                "duration_sec": c.duration_sec or 0,
                "transferred_to_human": bool((c.metrics or {}).get("transferred_to_human")),
                "recording_available": bool(c.recording_url),
                "created_at": _iso(c.created_at),
            }
        )
    return out


def call_detail(db: Session, call: Call, *, mask: bool = False) -> dict[str, Any]:
    m = {**empty_metrics(), **(call.metrics or {})}
    return {
        **call_items(db, [call], mask=mask)[0],
        "transcript": [
            {
                "speaker": i.get("speaker") if i.get("speaker") in SPEAKERS else "system",
                "text": i.get("text") or "",
                "at": i.get("at"),
                "confidence": i.get("confidence"),
            }
            for i in (call.transcript_items or [])
        ],
        "metrics": {
            "first_response_ms": m["first_response_ms"],
            "turns": int(m["turns"] or 0),
            "recognition_retries": int(m["recognition_retries"] or 0),
            "transferred_to_human": bool(m["transferred_to_human"]),
            "booking_created": bool(m["booking_created"]),
        },
    }


def local_range(db: Session, tenant_id: uuid.UUID, date_from: date | None, date_to: date | None):
    zone = escalation.branch_zone(db, tenant_id, None)
    start = datetime.combine(date_from, dtime(0), tzinfo=zone) if date_from else None
    end = datetime.combine(date_to + timedelta(days=1), dtime(0), tzinfo=zone) if date_to else None
    return start, end


def voice_report(db: Session, tenant_id: uuid.UUID, date_from: date | None, date_to: date | None) -> dict:
    """ТЗ §13.4 metrics over client calls (admin escalation IVR calls excluded). Rates are 0–100."""
    start, end = local_range(db, tenant_id, date_from, date_to)
    q = select(Call).where(Call.tenant_id == tenant_id, Call.purpose.in_(CLIENT_PURPOSES))
    if start is not None:
        q = q.where(Call.created_at >= start)
    if end is not None:
        q = q.where(Call.created_at < end)
    calls = db.scalars(q).all()
    answered = [c for c in calls if c.started_at is not None]
    metrics = [{**empty_metrics(), **(c.metrics or {})} for c in answered]
    first = [m["first_response_ms"] for m in metrics if m["first_response_ms"] is not None]
    turns = sum(int(m["turns"] or 0) for m in metrics)
    retries = sum(int(m["recognition_retries"] or 0) for m in metrics)

    def pct(part: float, whole: float) -> float:
        return round(100.0 * part / whole, 1) if whole else 0.0

    n = len(answered)
    return {
        "calls": len(calls),
        "answered": n,
        "avg_duration_sec": round(sum(c.duration_sec or 0 for c in answered) / n, 1) if n else 0.0,
        "avg_first_response_ms": round(sum(first) / len(first), 1) if first else 0.0,
        "transfer_rate": pct(sum(1 for m in metrics if m["transferred_to_human"]), n),
        "booking_rate": pct(sum(1 for m in metrics if m["booking_created"]), n),
        "recognition_retry_rate": pct(retries, turns + retries),
    }


# ====================================================================== retention (worker beat)
def purge_expired_transcripts(db: Session, now: datetime | None = None) -> int:
    """ТЗ §20.1: transcripts/recordings older than settings.voice.transcript_retention_days are erased
    (call row and metrics stay for reports). Also finalises client calls stuck in a live status. Commits."""
    now = now or utcnow()
    stale = db.scalars(
        select(Call).where(
            Call.purpose.in_(CLIENT_PURPOSES),
            Call.status.not_in(FINAL_STATUSES),
            Call.created_at < now - timedelta(hours=STALE_CALL_HOURS),
        )
    ).all()
    for call in stale:
        finish_call(db, call, call.updated_at or now, status="completed" if call.started_at else "failed")
    db.commit()
    purged = 0
    tenant_ids = db.scalars(select(Call.tenant_id).distinct()).all()
    for tenant_id in tenant_ids:
        days = int(voice_settings(db, tenant_id).get("transcript_retention_days") or 0)
        if days <= 0:
            continue
        calls = db.scalars(
            select(Call).where(
                Call.tenant_id == tenant_id,
                Call.created_at < now - timedelta(days=days),
                or_(
                    Call.transcript != "",
                    Call.recording_url.is_not(None),
                    Call.meta["purged_at"].as_string().is_(None),
                ),
            )
        ).all()
        conv_ids = [c.conversation_id for c in calls if c.conversation_id]
        for call in calls:
            call.transcript, call.transcript_items, call.recording_url = "", [], None
            _meta(call, purged_at=now.isoformat(), last_twiml=None)
        if conv_ids:
            db.execute(
                update(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.conversation_id.in_(conv_ids),
                    Message.message_type == "voice_turn",
                )
                .values(text="")
                .execution_options(synchronize_session=False)
            )
        purged += len(calls)
        db.commit()
    return purged
