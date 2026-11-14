"""HTTP hardening (ТЗ §20.1): security headers, request body limits, per-IP rate limits.

Pure ASGI middlewares (no BaseHTTPMiddleware buffering). Settings are read per request, so tests and
operators can tune them without re-creating the app.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.exceptions import HTTPException

from app.config import get_settings

log = logging.getLogger("api.security")

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

WEBHOOK_PREFIX = "/v1/webhooks/"
LOGIN_PATH = "/v1/auth/login"
DOCS_PATHS = ("/docs", "/redoc")  # Swagger/ReDoc HTML loads scripts from a CDN: no strict CSP there
# API responses are JSON (or file downloads): nothing may be executed, embedded or framed.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


async def _send_json(send: Send, status: int, detail: Any, headers: list[tuple[bytes, bytes]] | None = None):
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- security headers
class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        docs = path.startswith(DOCS_PATHS)
        production = get_settings().is_production

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}

                def add(name: bytes, value: str) -> None:
                    if name not in present:
                        headers.append((name, value.encode()))

                add(b"x-content-type-options", "nosniff")
                add(b"x-frame-options", "DENY")
                add(b"referrer-policy", "no-referrer")
                add(b"cross-origin-opener-policy", "same-origin")
                add(b"permissions-policy", "camera=(), microphone=(), geolocation=()")
                if not docs:
                    add(b"content-security-policy", API_CSP)
                if path.startswith("/v1/"):
                    add(b"cache-control", "no-store")
                if production:
                    add(b"strict-transport-security", "max-age=31536000; includeSubDomains")
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


# ---------------------------------------------------------------- body size limit
class BodyLimitMiddleware:
    """413 for bodies over the limit: webhooks (public, unauthenticated) get a tight one, the API a generous
    one. Enforced on Content-Length and on the streamed byte count (chunked uploads)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        s = get_settings()
        limit = (
            s.max_webhook_body_bytes
            if scope.get("path", "").startswith(WEBHOOK_PREFIX)
            else s.max_request_body_bytes
        )
        declared = next((v for k, v in scope.get("headers") or [] if k == b"content-length"), None)
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                await _send_json(send, 400, "Bad Content-Length")
                return
            if too_big:
                await _send_json(send, 413, "Request body too large")
                return

        received = 0

        async def _receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # handled by FastAPI/Starlette like any HTTPException -> 413
                    raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, _receive, send)


# ---------------------------------------------------------------- rate limiting
class MemoryCounter:
    """Fixed-window counters in process memory (single API process / fallback)."""

    def __init__(self) -> None:
        self._hits: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, window_s: int) -> int:
        window = int(time.time() // window_s)
        with self._lock:
            if len(self._hits) > 50_000:  # bound memory: drop stale windows
                self._hits = {k: v for k, v in self._hits.items() if v[0] == window}
            w, n = self._hits.get(key, (window, 0))
            n = n + 1 if w == window else 1
            self._hits[key] = (window, n)
            return n

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisCounter:
    """Fixed-window counters shared by all API processes."""

    def __init__(self, url: str) -> None:
        import redis

        self._r = redis.Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
        self._r.ping()

    def hit(self, key: str, window_s: int) -> int:
        k = f"rl:{key}:{int(time.time() // window_s)}"
        pipe = self._r.pipeline()
        pipe.incr(k)
        pipe.expire(k, window_s + 1)
        return int(pipe.execute()[0])


_memory = MemoryCounter()
_backend: dict[str, Any] = {"name": None, "counter": None}


def counter_backend() -> MemoryCounter | RedisCounter:
    s = get_settings()
    wanted = s.rate_limit_backend
    if _backend["name"] != wanted:
        counter: MemoryCounter | RedisCounter = _memory
        if wanted in ("auto", "redis"):
            try:
                counter = RedisCounter(s.redis_url)
            except Exception as exc:  # noqa: BLE001 - any connection problem -> in-process fallback
                if wanted == "redis":
                    log.warning(
                        "rate limit: Redis unavailable (%s), using in-memory counters", type(exc).__name__
                    )
        _backend.update(name=wanted, counter=counter)
    return _backend["counter"]


def reset_rate_limits() -> None:
    _memory.reset()
    _backend.update(name=None, counter=None)


def _limit_for(path: str) -> int | None:
    s = get_settings()
    if path == LOGIN_PATH:
        return s.rate_limit_login_per_minute
    if path.startswith(WEBHOOK_PREFIX):
        return s.rate_limit_webhook_per_minute
    return None


class RateLimitMiddleware:
    """Per client IP + path, 60 s fixed window, for the unauthenticated entry points (login, webhooks).
    Behind a reverse proxy run uvicorn with --proxy-headers so the client IP is the real one."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not get_settings().rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        limit = _limit_for(path)
        if limit is None or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        ip = (scope.get("client") or ("unknown", 0))[0]
        backend = counter_backend()
        try:
            hits = backend.hit(f"{ip}:{path}", 60)
        except Exception:  # noqa: BLE001 - Redis hiccup must not take the API down
            hits = _memory.hit(f"{ip}:{path}", 60)
        if hits > limit:
            log.warning("rate limit exceeded ip=%s path=%s", ip, path)
            retry = str(60 - int(time.time()) % 60)
            await _send_json(send, 429, "Too many requests", [(b"retry-after", retry.encode())])
            return
        await self.app(scope, receive, send)
