"""Re-encrypt integration secrets with the current key (docs/SECURITY.md "Rotating secrets").

    SECRETS_ENCRYPTION_KEY=<new>  SECRETS_ENCRYPTION_OLD_KEYS=<previous key or JWT secret>  \
        python -m app.rotate_secrets [--dry-run]

Idempotent; rows already under the current key are skipped. Afterwards drop the old key from
SECRETS_ENCRYPTION_OLD_KEYS.
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Integration, WebhookSubscription
from app.security import decrypt_secret, encrypt_secret, needs_reencryption


def _reencrypt(token: str, *, plaintext_fallback: bool) -> str | None:
    """New ciphertext, or None when ``token`` is already under the current key."""
    try:
        if not needs_reencryption(token):
            return None
        return encrypt_secret(decrypt_secret(token))
    except ValueError:  # undecodable with every known key (binascii.Error is a ValueError)
        if plaintext_fallback:  # legacy plaintext webhook secret
            return encrypt_secret(token)
        raise


def rotate(db: Session) -> int:
    changed = 0
    for integ in db.scalars(select(Integration)):
        if (new := _reencrypt(integ.secrets_enc, plaintext_fallback=False)) is not None:
            integ.secrets_enc, changed = new, changed + 1
    for sub in db.scalars(select(WebhookSubscription)):
        if (new := _reencrypt(sub.secret, plaintext_fallback=True)) is not None:
            sub.secret, changed = new, changed + 1
    return changed


def main(argv: list[str]) -> None:
    from app.db import SessionLocal

    with SessionLocal() as db:
        n = rotate(db)
        if "--dry-run" in argv:
            db.rollback()
        else:
            db.commit()
    print(f"re-encrypted {n} secret(s){' (dry run)' if '--dry-run' in argv else ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
