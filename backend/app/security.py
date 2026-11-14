"""Password hashing, JWT, secret encryption, webhook signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

_PBKDF2_ITER = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):  # malformed hash / bad base64 (binascii.Error is a ValueError)
        return False


def create_access_token(user_id: str, tenant_id: str | None, role: str | None) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=s.jwt_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token, get_settings().jwt_secret, algorithms=["HS256"], options={"require": ["exp", "iat", "sub"]}
    )


# --- symmetric encryption of integration secrets (keystream from HMAC-SHA256; authenticated) ---
def _derive(material: str) -> bytes:
    return hashlib.sha256(("enc:" + material).encode()).digest()


def _key() -> bytes:
    """Current key: SECRETS_ENCRYPTION_KEY, else derived from JWT_SECRET (backwards compatible)."""
    s = get_settings()
    return _derive(s.secrets_encryption_key or s.jwt_secret)


def _decrypt_keys() -> list[bytes]:
    """Current key first, then keys that stay readable during a rotation."""
    s = get_settings()
    old = [k.strip() for k in s.secrets_encryption_old_keys.split(",") if k.strip()]
    if s.secrets_encryption_key:
        old.append(s.jwt_secret)  # secrets written before a dedicated key was configured
    keys = [_key()]
    keys += [k for k in (_derive(m) for m in old) if k not in keys]
    return keys


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def _split(token: str) -> tuple[bytes, bytes, bytes]:
    raw = base64.urlsafe_b64decode(token.encode())
    return raw[:16], raw[16:32], raw[32:]


def _tag(key: bytes, nonce: bytes, ct: bytes) -> bytes:
    return hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    key = _key()
    nonce = os.urandom(16)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data)), strict=True))
    return base64.urlsafe_b64encode(nonce + _tag(key, nonce, ct) + ct).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    nonce, tag, ct = _split(token)
    for key in _decrypt_keys():
        if hmac.compare_digest(tag, _tag(key, nonce, ct)):
            return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)), strict=True)).decode()
    raise ValueError("secret integrity check failed")


def needs_reencryption(token: str) -> bool:
    """True when ``token`` is readable only with an old key (see app.rotate_secrets)."""
    if not token:
        return False
    nonce, tag, ct = _split(token)
    return not hmac.compare_digest(tag, _tag(_key(), nonce, ct))


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_meta_signature(app_secret: str | None, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex>"""
    if not app_secret:  # an empty key (e.g. WHATSAPP_APP_SECRET= in .env) would let anyone forge an HMAC
        return False
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(header[7:], sign_payload(app_secret, body))


def verify_crm_webhook(
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    *,
    tolerance_s: int = 300,
    now: float | None = None,
) -> bool:
    """Receiver-side check of OUR outbound webhooks (reference implementation, docs/SECURITY.md).

    X-CRM-Signature = "sha256=" + hex(HMAC-SHA256(secret, f"{X-CRM-Timestamp}." + raw_body)). Timestamps more
    than ``tolerance_s`` away from now are rejected (replay window); receivers should also de-duplicate by
    X-CRM-Delivery.
    """
    if not secret or not signature or not timestamp or not timestamp.isdigit():
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance_s:
        return False
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, "sha256=" + mac)


def random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)
