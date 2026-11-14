"""Structured logging with secret redaction (ТЗ §20.1: no secrets in logs).

Every log record — ours, uvicorn's, httpx's, celery's — passes through ``redact`` via a LogRecord factory,
so a bot token inside a URL, a JWT, a Bearer header or a ``password=...`` pair never reaches a handler.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token (also inside https://api.telegram.org/bot<token>/method)
    (re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), "<redacted:bot-token>"),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "<redacted:jwt>"),
    # Authorization: Bearer / Basic
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    # key=value / "key": "value" for secret-looking keys (query strings, dict reprs, form bodies)
    (
        re.compile(
            r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|signature)[\w.-]*[\"']?\s*[:=]\s*[\"']?)"
            r"([^\s\"'&,;}\]]{4,})"
        ),
        r"\1<redacted>",
    ),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes | int | float | bool) or value is None:
        return value
    text = str(value)
    clean = redact(text)
    return value if clean == text else clean


def _redact_record(record: logging.LogRecord, clean_message: str) -> None:
    """Redact msg and args IN PLACE, keeping the args structure: formatters such as uvicorn's access log
    unpack ``record.args`` themselves. Falls back to the pre-formatted message when the secret spans msg/args
    (e.g. ``"token=%s"``)."""
    args = record.args
    if isinstance(args, Mapping):
        new_args: Any = {k: _redact_arg(v) for k, v in args.items()}
    elif isinstance(args, tuple):
        new_args = tuple(_redact_arg(a) for a in args)
    else:
        new_args = args
    msg = redact(record.msg) if isinstance(record.msg, str) else record.msg
    try:
        rendered = str(msg) % new_args if new_args else str(msg)
    except (TypeError, ValueError, KeyError):
        rendered = None
    if rendered is not None and redact(rendered) == rendered:
        record.msg, record.args = msg, new_args
    else:  # () is falsy: getMessage() returns msg verbatim
        record.msg, record.args = clean_message, ()


_installed = {"done": False}


def install_redaction() -> None:
    """Idempotent: wrap the LogRecord factory so messages and tracebacks are redacted at creation."""
    if _installed["done"]:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = base(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed format args: let logging report it as usual
            return record
        clean = redact(message)
        if clean != message:
            _redact_record(record, clean)
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip("\n"))
        return record

    logging.setLogRecordFactory(factory)
    # httpx logs every request URL at INFO (Telegram URLs embed the bot token); keep only problems.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _installed["done"] = True


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    install_redaction()
