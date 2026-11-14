"""Voice API adapter for admin escalation calls (ТЗ §12.3, §13, §26.4).

TwilioVoice places an outbound call with inline TwiML ``<Gather numDigits="1">`` that speaks a short Russian
prompt (no client PII is spoken) and posts the pressed digit to ``/v1/webhooks/voice/twilio/gather``;
status callbacks go to ``/v1/webhooks/voice/twilio/status``. MockVoice records calls in ``MOCK_CALLS``.

Credentials: Integration(kind="voice").secrets {account_sid, auth_token, from_number}, falling back to the
platform-level settings.twilio_* values.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.integrations import tenant_secrets

log = logging.getLogger("voice")

PROMPT_RU = (
    "Здравствуйте. В CRM есть вопрос клиента, который ожидает ответа более десяти минут. "
    "Нажмите один, чтобы взять задачу в работу, или два, чтобы передать резервному администратору."
)
ACK_RU = "Спасибо. Задача закреплена за вами. Подробности в CRM и Телеграм."
FORWARD_RU = "Задача передаётся резервному администратору. До свидания."
NO_INPUT_RU = "Ответ не получен. До свидания."
VOICE_ATTRS = 'language="ru-RU" voice="Polly.Tatyana"'

MOCK_CALLS: list[dict[str, Any]] = []


class VoiceError(Exception):
    pass


@dataclass(frozen=True)
class PlacedCall:
    provider_ref: str
    status: str  # queued|ringing


class VoiceAdapter(Protocol):
    provider: str
    auth_token: str | None

    def place_call(self, to_number: str, *, call_id: uuid.UUID) -> PlacedCall: ...

    def cancel_call(self, provider_ref: str) -> None: ...


def callback_url(provider: str, kind: str, call_id: uuid.UUID | str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/v1/webhooks/voice/{provider}/{kind}?call_id={call_id}"


def say(text: str) -> str:
    return f"<Say {VOICE_ATTRS}>{escape(text)}</Say>"


def gather_twiml(provider: str, call_id: uuid.UUID | str) -> str:
    action = escape(callback_url(provider, "gather", call_id))
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather numDigits="1" timeout="8" method="POST" action="{action}">{say(PROMPT_RU)}</Gather>'
        f'<Gather numDigits="1" timeout="8" method="POST" action="{action}">{say(PROMPT_RU)}</Gather>'
        f"{say(NO_INPUT_RU)}<Hangup/></Response>"
    )


def final_twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say(text)}<Hangup/></Response>'


class MockVoice:
    provider = "mock"
    auth_token: str | None = None

    def place_call(self, to_number: str, *, call_id: uuid.UUID) -> PlacedCall:
        ref = f"mock-{uuid.uuid4().hex[:12]}"
        MOCK_CALLS.append(
            {
                "action": "call",
                "to": to_number,
                "call_id": str(call_id),
                "provider_ref": ref,
                "twiml": gather_twiml(self.provider, call_id),
            }
        )
        return PlacedCall(provider_ref=ref, status="ringing")

    def cancel_call(self, provider_ref: str) -> None:
        MOCK_CALLS.append({"action": "cancel", "provider_ref": provider_ref})


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

    def place_call(self, to_number: str, *, call_id: uuid.UUID) -> PlacedCall:
        data = {
            "To": to_number,
            "From": self.from_number,
            "Twiml": gather_twiml(self.provider, call_id),
            "StatusCallback": callback_url(self.provider, "status", call_id),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": "25",  # ring time before no-answer
            "MachineDetection": "Enable",
        }
        res = self._post("/Calls.json", data)
        return PlacedCall(provider_ref=str(res["sid"]), status="queued")

    def cancel_call(self, provider_ref: str) -> None:
        # "canceled" only works for queued/ringing; "completed" hangs up an in-progress call.
        try:
            self._post(f"/Calls/{provider_ref}.json", {"Status": "canceled"})
        except VoiceError:
            self._post(f"/Calls/{provider_ref}.json", {"Status": "completed"})

    def check_credentials(self) -> None:
        try:
            r = httpx.get(
                self._base + ".json", auth=(self.account_sid, self.auth_token), timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"network error: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise VoiceError(f"Twilio HTTP {r.status_code}")


def twilio_credentials(db: Session, tenant_id: uuid.UUID) -> tuple[str | None, str | None, str | None]:
    s = get_settings()
    sec = tenant_secrets(db, tenant_id, "voice")
    return (
        sec.get("account_sid") or s.twilio_account_sid,
        sec.get("auth_token") or s.twilio_auth_token,
        sec.get("from_number") or s.twilio_from_number,
    )


def get_voice_adapter(db: Session, tenant_id: uuid.UUID) -> VoiceAdapter | None:
    """None when voice is not configured (escalation then skips the call step)."""
    if get_settings().voice_provider == "mock":
        return MockVoice()
    sid, token, from_number = twilio_credentials(db, tenant_id)
    if not (sid and token and from_number):
        return None
    return TwilioVoice(sid, token, from_number)


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth_token, url + concat(sorted key+value)))."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    return bool(signature) and hmac.compare_digest(twilio_signature(auth_token, url, params), signature or "")
