"""Speech-to-text adapter for WhatsApp voice notes (ТЗ §13.1: audio -> STT -> AI -> text).

Select the provider with env STT_PROVIDER:

- ``openai_compatible`` — ``OpenAICompatibleSTT``: POST multipart to ``{STT_BASE_URL}/audio/transcriptions``.
  The same API is spoken by OpenAI Whisper (``https://api.openai.com/v1``, model ``whisper-1``), Groq
  (``https://api.groq.com/openai/v1``, model ``whisper-large-v3``) and self-hosted faster-whisper servers
  (e.g. speaches / faster-whisper-server, ``http://stt:8000/v1``). Kazakh quality must be validated on real
  recordings before going live.
- ``mock`` — fixed transcript (default while WhatsApp runs in mock mode).
- ``none`` — ``NotConfiguredSTT`` raises ``STTNotConfigured``; the voice note is routed to a human.

Any provider failure (timeout, 5xx, oversize audio, bad JSON) raises ``STTError``; the caller falls back to a
human, the voice note is never lost. The API key is sent only in the Authorization header and never logged.
"""

from __future__ import annotations

import io
import logging
import math
import time
import wave
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import get_settings

log = logging.getLogger("stt")

MOCK_TRANSCRIPT = "[voice message]"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI / Groq upload limit
PROVIDERS = ("openai_compatible", "mock", "none")

_EXT = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "m4a",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


class STTError(RuntimeError):
    pass


class STTNotConfigured(STTError):
    pass


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str | None = None  # ru|kk if the provider detects it
    confidence: float | None = None  # 0..1, None when the provider gives no signal


class SpeechToText(Protocol):
    name: str

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription: ...


class MockSTT:
    name = "mock"
    model: str | None = None

    def __init__(self, transcript: str = MOCK_TRANSCRIPT, confidence: float | None = None):
        self.transcript = transcript
        self.confidence = confidence

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription:
        return Transcription(text=self.transcript, language=language_hint, confidence=self.confidence)


class NotConfiguredSTT:
    name = "none"
    model: str | None = None

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription:
        raise STTNotConfigured("No speech-to-text provider is configured")


def normalize_language(value: Any) -> str | None:
    """Provider language (``ru``, ``russian``, ``kk``, ``kazakh``...) -> ru|kk|None."""
    v = str(value or "").strip().lower()
    if v in ("ru", "rus", "russian") or v.startswith("ru-"):
        return "ru"
    if v in ("kk", "kaz", "kazakh") or v.startswith("kk-"):
        return "kk"
    return None


def confidence_from_segments(segments: Any) -> float | None:
    """Mean of exp(avg_logprob) over the segments (0..1), or None when no segment carries a logprob."""
    if not isinstance(segments, list):
        return None
    probs = []
    for seg in segments:
        lp = seg.get("avg_logprob") if isinstance(seg, dict) else None
        if isinstance(lp, int | float) and not isinstance(lp, bool) and math.isfinite(lp):
            probs.append(min(1.0, max(0.0, math.exp(lp))))
    if not probs:
        return None
    return round(sum(probs) / len(probs), 4)


class OpenAICompatibleSTT:
    """OpenAI ``/audio/transcriptions`` client (OpenAI Whisper, Groq, faster-whisper servers)."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str = "whisper-1",
        timeout: float = 30.0,
        *,
        max_bytes: int = MAX_AUDIO_BYTES,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or None
        self.model = model or "whisper-1"
        self.timeout = float(timeout)
        self.max_bytes = max_bytes
        self._transport = transport  # tests inject httpx.MockTransport

    def __repr__(self) -> str:  # never expose the key through repr/logging
        return f"OpenAICompatibleSTT(base_url={self.base_url!r}, model={self.model!r})"

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription:
        if not self.base_url:
            raise STTNotConfigured("STT_BASE_URL is not set")
        if not audio:
            raise STTError("empty audio")
        if len(audio) > self.max_bytes:
            raise STTError(f"audio too large: {len(audio)} bytes > {self.max_bytes}")
        mime = (mime or "audio/ogg").split(";")[0].strip().lower()
        data: dict[str, str] = {"model": self.model, "response_format": "verbose_json"}
        lang = normalize_language(language_hint)
        if lang:
            data["language"] = lang
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        files = {"file": (f"voice.{_EXT.get(mime, 'ogg')}", audio, mime)}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                r = client.post(
                    f"{self.base_url}/audio/transcriptions", data=data, files=files, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise STTError(f"timeout after {self.timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise STTError(f"network error: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            # Body excerpt only: providers do not echo the Authorization header.
            raise STTError(f"STT HTTP {r.status_code}: {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as exc:
            raise STTError("STT returned invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise STTError("STT response has no text")
        return Transcription(
            text=body["text"].strip(),
            language=normalize_language(body.get("language")) or lang,
            confidence=confidence_from_segments(body.get("segments")),
        )


_override: SpeechToText | None = None


def set_stt(stt: SpeechToText | None) -> None:
    """Install a provider instance (tests / app startup wiring). None restores env-based selection."""
    global _override
    _override = stt


def provider_name() -> str:
    s = get_settings()
    default = "mock" if s.whatsapp_mode == "mock" else "none"
    name = (s.stt_provider or default).strip().lower()
    return name if name in PROVIDERS else "none"


def get_stt() -> SpeechToText:
    if _override is not None:
        return _override
    name = provider_name()
    if name == "mock":
        return MockSTT()
    if name == "openai_compatible":
        s = get_settings()
        if not s.stt_base_url:
            return NotConfiguredSTT()
        return OpenAICompatibleSTT(s.stt_base_url, s.stt_api_key, s.stt_model, s.stt_timeout_sec)
    return NotConfiguredSTT()


def stt_status() -> dict[str, Any]:
    """Public (secret-free) description of the active provider for the integrations page."""
    stt = get_stt()
    name = getattr(stt, "name", "none")
    configured = name == "mock" or bool(getattr(stt, "configured", False))
    return {"provider": name, "configured": configured, "model": getattr(stt, "model", None)}


def sample_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A short silent mono 16-bit WAV used by the integration test endpoint (no binary fixture shipped)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_provider(language_hint: str = "ru") -> dict[str, Any]:
    """Transcribe the bundled 1 s sample with the active provider: {ok, provider, latency_ms, error}."""
    stt = get_stt()
    started = time.perf_counter()
    error = None
    try:
        stt.transcribe(sample_wav(), "audio/wav", language_hint)
    except Exception as exc:  # noqa: BLE001 - reported to the UI (never contains the key)
        error = str(exc)[:300] or exc.__class__.__name__
        log.warning("STT test failed provider=%s: %s", stt.name, error)
    return {
        "ok": error is None,
        "provider": stt.name,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }


test_provider.__test__ = False  # type: ignore[attr-defined]  # not a pytest test
