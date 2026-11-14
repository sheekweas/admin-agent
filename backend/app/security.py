"""Password hashing, JWT, secret encryption, webhook signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
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
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])


# --- symmetric encryption of integration secrets (keystream from HMAC-SHA256; authenticated) ---
def _key() -> bytes:
    return hashlib.sha256(("enc:" + get_settings().jwt_secret).encode()).digest()


def _keystream(nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(_key(), nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    nonce = os.urandom(16)
    data = plaintext.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data)), strict=True))
    tag = hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    raw = base64.urlsafe_b64decode(token.encode())
    nonce, tag, ct = raw[:16], raw[16:32], raw[32:]
    if not hmac.compare_digest(tag, hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16]):
        raise ValueError("secret integrity check failed")
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)), strict=True)).decode()


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex>"""
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(header[7:], sign_payload(app_secret, body))


def random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)
