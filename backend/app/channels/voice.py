"""Voice providers (ТЗ §12.3, §13, §26.4): admin escalation IVR + AI phone calls with clients.

``VoiceProvider`` is the provider-neutral protocol: place/hang up/cancel a call, start a recording and build
call-control responses (say / gather / dial / hangup) through ``response()``.

- ``TwilioVoice``  — Twilio Programmable Voice (REST + TwiML). Turn-based: ``<Gather input="speech">`` posts
  the recognised utterance to ``/v1/webhooks/voice/twilio/turn``; barge-in is on (``bargeIn="true"``).
- ``MockVoice``    — records calls in ``MOCK_CALLS`` and produces the same TwiML (dev / tests / demo).
- ``WhatsAppCallingProvider`` — placeholder for the WhatsApp Business Calling API (§13.1, §27). It is NOT
  implemented: availability depends on Meta approval per business account and country, calls need the user's
  call permission and a streaming media (WebRTC/SIP via a BSP such as Twilio) pipeline. Every method raises
  ``VoiceNotConfigured`` with that explanation; the ``WHATSAPP_CALLING_ENABLED`` flag only routes to it.

TTS/STT languages per tenant come from ``settings.voice`` (``tts_voice_ru``/``tts_voice_kk``,
``tts_language_kk``, ``stt_language_ru``/``stt_language_kk``). Twilio offers no Kazakh TTS voice, so by default
Kazakh phrases are spoken by the Russian ``Polly.Tatyana`` voice (``language="ru-RU"``): intelligible, with an
accent. Speech recognition uses ``kk-KZ`` (verify it is enabled for the account) — see README.

Credentials: Integration(kind="voice").secrets {account_sid, auth_token, from_number}, falling back to the
platform-level settings.twilio_* values. The tenant's public number lives in Integration.config.phone_number
(falls back to from_number) and is how inbound calls are routed to a tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Integration
from app.services.common import normalize_phone
from app.services.integrations import read_secrets, tenant_secrets

log = logging.getLogger("voice")

# ---------------------------------------------------------------- admin escalation IVR (§26.4, RU + KK)
IVR_PHRASES: dict[str, dict[str, str]] = {
    "prompt": {
        "ru": "Здравствуйте. В CRM есть вопрос клиента, который ожидает ответа более десяти минут. "
        "Нажмите один, чтобы взять задачу в работу, или два, чтобы передать резервному администратору.",
        "kk": "Сәлеметсіз бе. CRM жүйесінде он минуттан астам жауап күтіп тұрған клиент сұрағы бар. "
        "Тапсырманы жұмысқа алу үшін бірді басыңыз, резервтегі әкімшіге беру үшін екіні басыңыз.",
    },
    "ack": {
        "ru": "Спасибо. Задача закреплена за вами. Подробности в CRM и Телеграм.",
        "kk": "Рақмет. Тапсырма сізге бекітілді. Толығырақ CRM мен Телеграмда.",
    },
    "forward": {
        "ru": "Задача передаётся резервному администратору. До свидания.",
        "kk": "Тапсырма резервтегі әкімшіге беріледі. Сау болыңыз.",
    },
    "no_input": {"ru": "Ответ не получен. До свидания.", "kk": "Жауап алынбады. Сау болыңыз."},
}
# Backwards-compatible names (RU)
PROMPT_RU = IVR_PHRASES["prompt"]["ru"]
ACK_RU = IVR_PHRASES["ack"]["ru"]
FORWARD_RU = IVR_PHRASES["forward"]["ru"]
NO_INPUT_RU = IVR_PHRASES["no_input"]["ru"]

MOCK_CALLS: list[dict[str, Any]] = []


class VoiceError(Exception):
    pass


class VoiceNotConfigured(VoiceError):
    pass


@dataclass(frozen=True)
class PlacedCall:
    provider_ref: str
    status: str  # queued|ringing


@dataclass(frozen=True)
class VoiceProfile:
    """How one language is spoken and recognised on a call."""

    tts_voice: str = "Polly.Tatyana"
    tts_language: str = "ru-RU"
    stt_language: str = "ru-RU"


DEFAULT_PROFILES = {
    "ru": VoiceProfile(),
    "kk": VoiceProfile("Polly.Tatyana", "ru-RU", "kk-KZ"),
}


def voice_profile(voice_settings: dict[str, Any] | None, lang: str) -> VoiceProfile:
    vs = voice_settings or {}
    if lang == "kk":
        return VoiceProfile(
            str(vs.get("tts_voice_kk") or "Polly.Tatyana"),
            str(vs.get("tts_language_kk") or "ru-RU"),
            str(vs.get("stt_language_kk") or "kk-KZ"),
        )
    return VoiceProfile(
        str(vs.get("tts_voice_ru") or "Polly.Tatyana"), "ru-RU", str(vs.get("stt_language_ru") or "ru-RU")
    )


def callback_url(provider: str, kind: str, call_id: uuid.UUID | str, **params: Any) -> str:
    base = get_settings().public_base_url.rstrip("/")
    query = urlencode({"call_id": str(call_id), **{k: v for k, v in params.items() if v is not None}})
    return f"{base}/v1/webhooks/voice/{provider}/{kind}?{query}"


# ---------------------------------------------------------------- call-control response builder (TwiML)
class VoiceResponse:
    """Builds TwiML. Only static phrases, the AI reply and what the caller said go in here — never client
    data from the CRM (phone numbers, other bookings, notes)."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    @staticmethod
    def _say(text: str, profile: VoiceProfile) -> str:
        return (
            f"<Say voice={quoteattr(profile.tts_voice)} language={quoteattr(profile.tts_language)}>"
            f"{escape(text)}</Say>"
        )

    def say(self, text: str, profile: VoiceProfile = DEFAULT_PROFILES["ru"]) -> VoiceResponse:
        if text:
            self._parts.append(self._say(text, profile))
        return self

    def gather_speech(self, text: str, profile: VoiceProfile, action: str) -> VoiceResponse:
        """Ask and listen: the utterance (or an empty result) is POSTed to `action`. Barge-in enabled."""
        self._parts.append(
            f'<Gather input="speech" language={quoteattr(profile.stt_language)} speechTimeout="auto" '
            f'bargeIn="true" actionOnEmptyResult="true" method="POST" action={quoteattr(action)}>'
            f"{self._say(text, profile) if text else ''}</Gather>"
        )
        return self

    def gather_digits(
        self, text: str, profile: VoiceProfile, action: str, *, timeout: int = 8
    ) -> VoiceResponse:
        self._parts.append(
            f'<Gather numDigits="1" timeout="{int(timeout)}" method="POST" action={quoteattr(action)}>'
            f"{self._say(text, profile)}</Gather>"
        )
        return self

    def dial(
        self, number: str, *, action: str, timeout: int = 20, caller_id: str | None = None
    ) -> VoiceResponse:
        cid = f" callerId={quoteattr(caller_id)}" if caller_id else ""
        self._parts.append(
            f'<Dial timeout="{int(timeout)}" method="POST" action={quoteattr(action)}{cid}>'
            f"<Number>{escape(number)}</Number></Dial>"
        )
        return self

    def hangup(self) -> VoiceResponse:
        self._parts.append("<Hangup/>")
        return self

    def to_xml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(self._parts) + "</Response>"


def say(text: str, lang: str = "ru") -> str:
    return VoiceResponse._say(text, DEFAULT_PROFILES.get(lang, DEFAULT_PROFILES["ru"]))


def gather_twiml(
    provider: str, call_id: uuid.UUID | str, lang: str = "ru", profile: VoiceProfile | None = None
) -> str:
    """Admin escalation IVR: "press 1 / press 2", asked twice, then goodbye (§12.3, §26.4)."""
    lang = "kk" if lang == "kk" else "ru"
    profile = profile or DEFAULT_PROFILES[lang]
    action = callback_url(provider, "gather", call_id)
    prompt = IVR_PHRASES["prompt"][lang]
    return (
        VoiceResponse()
        .gather_digits(prompt, profile, action)
        .gather_digits(prompt, profile, action)
        .say(IVR_PHRASES["no_input"][lang], profile)
        .hangup()
        .to_xml()
    )


def final_twiml(text: str, profile: VoiceProfile | None = None) -> str:
    return VoiceResponse().say(text, profile or DEFAULT_PROFILES["ru"]).hangup().to_xml()


# ---------------------------------------------------------------- providers
class VoiceProvider(Protocol):
    provider: str
    auth_token: str | None

    def place_call(
        self,
        to_number: str,
        *,
        call_id: uuid.UUID,
        twiml: str | None = None,
        answer_url: str | None = None,
        machine_detection: bool = True,
    ) -> PlacedCall: ...

    def hangup(self, provider_ref: str) -> None: ...

    def cancel_call(self, provider_ref: str) -> None: ...

    def start_recording(self, provider_ref: str, *, call_id: uuid.UUID) -> None: ...

    def delete_recording(self, recording_sid: str) -> None:
        """Remove a recording at the provider (recordings made without consent, §20.1)."""
        ...

    def response(self) -> VoiceResponse: ...


VoiceAdapter = VoiceProvider  # old name


class MockVoice:
    provider = "mock"
    auth_token: str | None = None

    def place_call(
        self,
        to_number: str,
        *,
        call_id: uuid.UUID,
        twiml: str | None = None,
        answer_url: str | None = None,
        machine_detection: bool = True,
    ) -> PlacedCall:
        ref = f"mock-{uuid.uuid4().hex[:12]}"
        entry: dict[str, Any] = {
            "action": "call",
            "to": to_number,
            "call_id": str(call_id),
            "provider_ref": ref,
        }
        if answer_url:
            entry["answer_url"] = answer_url
        else:
            entry["twiml"] = twiml or gather_twiml(self.provider, call_id)
        MOCK_CALLS.append(entry)
        return PlacedCall(provider_ref=ref, status="ringing")

    def cancel_call(self, provider_ref: str) -> None:
        MOCK_CALLS.append({"action": "cancel", "provider_ref": provider_ref})

    def hangup(self, provider_ref: str) -> None:
        MOCK_CALLS.append({"action": "hangup", "provider_ref": provider_ref})

    def start_recording(self, provider_ref: str, *, call_id: uuid.UUID) -> None:
        MOCK_CALLS.append({"action": "record", "provider_ref": provider_ref, "call_id": str(call_id)})

    def delete_recording(self, recording_sid: str) -> None:
        MOCK_CALLS.append({"action": "delete_recording", "recording_sid": recording_sid})

    def response(self) -> VoiceResponse:
        return VoiceResponse()


class TwilioVoice:
    provider = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str, timeout: float = 10.0):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.timeout = timeout

    @property
    def _base(self) -> str:
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}"

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            r = httpx.post(
                self._base + path, data=data, auth=(self.account_sid, self.auth_token), timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"network error: {exc.__class__.__name__}") from exc
        if r.status_code >= 300:
            raise VoiceError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def place_call(
        self,
        to_number: str,
        *,
        call_id: uuid.UUID,
        twiml: str | None = None,
        answer_url: str | None = None,
        machine_detection: bool = True,
    ) -> PlacedCall:
        data: dict[str, Any] = {
            "To": to_number,
            "From": self.from_number,
            "StatusCallback": callback_url(self.provider, "status", call_id),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": "25",  # ring time before no-answer
        }
        if answer_url:  # AI call: TwiML is built when the client answers
            data.update(Url=answer_url, Method="POST")
        else:
            data["Twiml"] = twiml or gather_twiml(self.provider, call_id)
        if machine_detection:
            data["MachineDetection"] = "Enable"
        res = self._post("/Calls.json", data)
        return PlacedCall(provider_ref=str(res["sid"]), status="queued")

    def cancel_call(self, provider_ref: str) -> None:
        # "canceled" only works for queued/ringing; "completed" hangs up an in-progress call.
        try:
            self._post(f"/Calls/{provider_ref}.json", {"Status": "canceled"})
        except VoiceError:
            self._post(f"/Calls/{provider_ref}.json", {"Status": "completed"})

    def hangup(self, provider_ref: str) -> None:
        self._post(f"/Calls/{provider_ref}.json", {"Status": "completed"})

    def start_recording(self, provider_ref: str, *, call_id: uuid.UUID) -> None:
        self._post(
            f"/Calls/{provider_ref}/Recordings.json",
            {
                "RecordingStatusCallback": callback_url(self.provider, "recording", call_id),
                "RecordingStatusCallbackEvent": "completed",
                "RecordingStatusCallbackMethod": "POST",
            },
        )

    def delete_recording(self, recording_sid: str) -> None:
        """DELETE /Recordings/{sid}.json (204). An already-deleted recording (404) counts as done."""
        if not re.fullmatch(r"RE[0-9a-fA-F]{32}", recording_sid or ""):
            raise VoiceError("invalid recording sid")
        try:
            r = httpx.delete(
                f"{self._base}/Recordings/{recording_sid}.json",
                auth=(self.account_sid, self.auth_token),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"network error: {exc.__class__.__name__}") from exc
        if r.status_code not in (200, 204, 404):
            raise VoiceError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")

    def response(self) -> VoiceResponse:
        return VoiceResponse()

    def check_credentials(self) -> None:
        try:
            r = httpx.get(
                self._base + ".json", auth=(self.account_sid, self.auth_token), timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"network error: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise VoiceError(f"Twilio HTTP {r.status_code}")


WHATSAPP_CALLING_NOT_AVAILABLE = (
    "WhatsApp Business Calling API is not available in this build: it requires Meta approval for the "
    "business account and country, the user's call permission and a streaming media gateway (BSP, e.g. "
    "Twilio WhatsApp Business Calling). Use the phone (PSTN) voice channel instead. See ТЗ §13.1, §27."
)


class WhatsAppCallingProvider:
    """Stub behind settings.whatsapp_calling_enabled. Documents the integration point; never pretends to work.

    A real implementation would: receive `calls` webhooks from the WhatsApp Cloud API (connect/terminate),
    answer with an SDP offer through the BSP media gateway, stream audio to STT/TTS and reuse the same
    dialogue loop (app.services.voice_calls) as PSTN calls. Outbound business-initiated calls additionally
    need an accepted call-permission request from the user (Meta rules).
    """

    provider = "whatsapp"
    auth_token: str | None = None

    def _unavailable(self, *_: Any, **__: Any) -> Any:
        raise VoiceNotConfigured(WHATSAPP_CALLING_NOT_AVAILABLE)

    place_call = hangup = cancel_call = start_recording = delete_recording = response = _unavailable


# ---------------------------------------------------------------- tenant wiring
def twilio_credentials(db: Session, tenant_id: uuid.UUID) -> tuple[str | None, str | None, str | None]:
    s = get_settings()
    sec = tenant_secrets(db, tenant_id, "voice")
    return (
        sec.get("account_sid") or s.twilio_account_sid,
        sec.get("auth_token") or s.twilio_auth_token,
        sec.get("from_number") or s.twilio_from_number,
    )


def get_voice_provider(db: Session, tenant_id: uuid.UUID, channel: str = "pstn") -> VoiceProvider:
    """Raises VoiceNotConfigured when the channel cannot place calls for this tenant."""
    if channel == "whatsapp":
        if not get_settings().whatsapp_calling_enabled:
            raise VoiceNotConfigured("WhatsApp calling is disabled (WHATSAPP_CALLING_ENABLED=false)")
        return WhatsAppCallingProvider()  # type: ignore[return-value]
    if get_settings().voice_provider == "mock":
        return MockVoice()
    sid, token, from_number = twilio_credentials(db, tenant_id)
    if not (sid and token and from_number):
        raise VoiceNotConfigured("Twilio account_sid, auth_token and from_number are required")
    return TwilioVoice(sid, token, from_number)


def get_voice_adapter(db: Session, tenant_id: uuid.UUID) -> VoiceProvider | None:
    """None when voice is not configured (escalation then skips the call step)."""
    try:
        return get_voice_provider(db, tenant_id)
    except VoiceNotConfigured:
        return None


def _integration_numbers(integ: Integration) -> set[str]:
    cfg = integ.config or {}
    raw = [cfg.get("phone_number"), cfg.get("from_number"), integ.external_id]
    if not any(raw):
        raw.append(read_secrets(integ).get("from_number"))
    return {n for n in (normalize_phone(str(r)) for r in raw if r) if n}


def tenant_voice_number(db: Session, tenant_id: uuid.UUID) -> str | None:
    integ = db.scalars(
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.kind == "voice", Integration.enabled.is_(True))
        .order_by(Integration.created_at)
    ).first()
    if integ is not None:
        cfg = integ.config or {}
        for raw in (cfg.get("phone_number"), cfg.get("from_number"), read_secrets(integ).get("from_number")):
            if raw and (n := normalize_phone(str(raw))):
                return n
    return normalize_phone(get_settings().twilio_from_number) if get_settings().twilio_from_number else None


def resolve_tenant_by_number(db: Session, called: str | None) -> uuid.UUID | None:
    """Inbound routing: the called number identifies the organisation (Integration kind=voice)."""
    number = normalize_phone(called) if called else None
    if not number:
        return None
    rows = db.scalars(
        select(Integration).where(Integration.kind == "voice", Integration.enabled.is_(True))
    ).all()
    matches = {i.tenant_id for i in rows if number in _integration_numbers(i)}
    if len(matches) != 1:  # unknown or (misconfigured) shared number -> refuse rather than guess
        if len(matches) > 1:
            log.error("voice number %s is configured for %d tenants", number[-4:], len(matches))
        return None
    return matches.pop()


# ---------------------------------------------------------------- webhook signatures
def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth_token, url + concat(sorted key+value)))."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    return bool(signature) and hmac.compare_digest(twilio_signature(auth_token, url, params), signature or "")
