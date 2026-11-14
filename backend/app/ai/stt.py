"""Speech-to-text adapter for WhatsApp voice notes (ТЗ §13.1: audio -> STT -> AI -> text).

Select the provider with env STT_PROVIDER: "mock" (default while WhatsApp runs in mock mode) or "none".
A real provider implements `SpeechToText.transcribe`; until one is configured, `NotConfiguredSTT`
raises `STTNotConfigured` and the voice note is routed to a human (data is never lost).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings

MOCK_TRANSCRIPT = "[voice message]"


class STTError(RuntimeError):
    pass


class STTNotConfigured(STTError):
    pass


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str | None = None  # ru|kk if the provider detects it


class SpeechToText(Protocol):
    name: str

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription: ...


class MockSTT:
    name = "mock"

    def __init__(self, transcript: str = MOCK_TRANSCRIPT):
        self.transcript = transcript

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription:
        return Transcription(text=self.transcript, language=language_hint)


class NotConfiguredSTT:
    name = "none"

    def transcribe(self, audio: bytes, mime: str, language_hint: str | None = None) -> Transcription:
        raise STTNotConfigured("No speech-to-text provider is configured")


_override: SpeechToText | None = None


def set_stt(stt: SpeechToText | None) -> None:
    """Install a provider instance (tests / app startup wiring). None restores env-based selection."""
    global _override
    _override = stt


def get_stt() -> SpeechToText:
    if _override is not None:
        return _override
    default = "mock" if get_settings().whatsapp_mode == "mock" else "none"
    provider = os.environ.get("STT_PROVIDER", default).lower()
    return MockSTT() if provider == "mock" else NotConfiguredSTT()
