"""Telegram Bot API adapter for admin notifications (ТЗ §12).

Per-tenant bot token: Integration(kind="telegram").secrets["bot_token"].
Mock mode (settings.telegram_mode == "mock") never touches the network: every call is appended to
``MOCK_SENT`` so tests can assert on it; ``set_mock_outage(True)`` simulates Telegram being down (AT-10).

Buttons: a list of rows, each row a list of ``{"text": ..., "callback_data": ...}`` or
``{"text": ..., "url": ...}``; a flat list of buttons is treated as a single row.
"""

from __future__ import annotations

import itertools
import logging
import threading
import uuid
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.integrations import tenant_secrets

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
MOCK_SENT: list[dict[str, Any]] = []
_mock_ids = itertools.count(1000)
_mock_lock = threading.Lock()
_mock_state = {"outage": False}

Buttons = list[list[dict[str, str]]] | list[dict[str, str]] | None


class TelegramError(Exception):
    """retryable=False for permanent failures (chat not found, bot blocked, bad request)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def set_mock_outage(down: bool) -> None:
    _mock_state["outage"] = down


def reset_mock() -> None:
    MOCK_SENT.clear()
    _mock_state["outage"] = False


def keyboard(buttons: Buttons) -> dict[str, Any] | None:
    if not buttons:
        return None
    rows = buttons if isinstance(buttons[0], list) else [buttons]
    return {"inline_keyboard": [[dict(b) for b in row] for row in rows]}  # type: ignore[union-attr]


class TelegramClient:
    def __init__(self, token: str | None, *, mode: str | None = None, timeout: float = 10.0):
        self.token = token
        self.mode = mode or get_settings().telegram_mode
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if self.mode == "mock":
            with _mock_lock:
                if _mock_state["outage"]:
                    raise TelegramError("mock outage: Telegram unavailable", retryable=True)
                result: Any = True
                if method in ("sendMessage", "editMessageText"):
                    mid = payload.get("message_id") or next(_mock_ids)
                    result = {"message_id": mid, "chat": {"id": payload.get("chat_id")}}
                MOCK_SENT.append({"method": method, **payload})
            return result
        if not self.token:
            raise TelegramError("Telegram bot token is not configured", retryable=True)
        try:
            r = httpx.post(f"{API_BASE}/bot{self.token}/{method}", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise TelegramError(f"network error: {exc.__class__.__name__}", retryable=True) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("ok"):
            return data.get("result")
        desc = str(data.get("description") or r.text[:200])
        # 429 / 5xx are transient; 400/401/403/404 are permanent for this request.
        retryable = r.status_code == 429 or r.status_code >= 500
        raise TelegramError(f"HTTP {r.status_code}: {desc}", retryable=retryable)

    # ------------------------------------------------------------------ API
    def send_message(
        self,
        chat_id: str | int,
        text: str,
        buttons: Buttons = None,
        *,
        force_reply: bool = False,
        reply_to_message_id: int | None = None,
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        kb = keyboard(buttons)
        if kb:
            payload["reply_markup"] = kb
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        if reply_to_message_id:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def edit_message_text(
        self, chat_id: str | int, message_id: int, text: str, buttons: Buttons = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        kb = keyboard(buttons)
        payload["reply_markup"] = kb or {"inline_keyboard": []}
        try:
            self._call("editMessageText", payload)
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def answer_callback_query(self, callback_query_id: str, text: str = "", *, alert: bool = False) -> None:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200], "show_alert": alert},
        )

    def set_webhook(self, url: str, secret_token: str) -> None:
        self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )

    def get_me(self) -> dict[str, Any]:
        if self.mode == "mock":
            return {"id": 1, "username": "mock_crm_bot", "is_bot": True}
        return dict(self._call("getMe", {}))


def client_for_tenant(db: Session, tenant_id: uuid.UUID) -> TelegramClient:
    return TelegramClient(tenant_secrets(db, tenant_id, "telegram").get("bot_token"))


def mock_messages(chat_id: str | int | None = None, method: str = "sendMessage") -> list[dict[str, Any]]:
    """Test helper: messages recorded in mock mode (optionally for one chat)."""
    return [
        m
        for m in MOCK_SENT
        if m["method"] == method and (chat_id is None or str(m["chat_id"]) == str(chat_id))
    ]
