"""Platform operator CLI (no HTTP endpoint exists for creating organizations).

    python -m app.cli create-tenant --name "Barber Pro" --owner-email owner@barberpro.kz \
        --owner-name "Айдар Сапаров" --branch "Barber Pro Алматы" --timezone Asia/Almaty

Creates tenant + owner user/membership + first branch in one transaction. Idempotent: if an organization with
this name already has this owner, nothing is changed and the existing ids are printed (exit code 0).
The owner password comes from --owner-password, else $CRM_OWNER_PASSWORD, else a random one is generated and
printed once. An existing user account is linked as owner WITHOUT changing its password.
Prints one JSON object (machine-readable) on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Branch, Membership, Role, Tenant, User
from app.security import hash_password
from app.services.audit import audit

NICHES = ("barbershop", "beauty", "dental", "other")
LANGUAGES = ("ru", "kk")
MIN_PASSWORD = 10


class CliError(ValueError):
    pass


@dataclass
class CreatedTenant:
    created: bool
    tenant_id: str
    branch_id: str | None
    owner_user_id: str
    owner_email: str
    owner_password: str | None = None  # only when generated here; shown once


def create_tenant(
    db: Session,
    *,
    name: str,
    owner_email: str,
    owner_name: str = "",
    owner_password: str | None = None,
    owner_phone: str | None = None,
    branch_name: str | None = None,
    branch_address: str = "",
    timezone: str = "Asia/Almaty",
    niche: str = "barbershop",
    language: str = "ru",
    currency: str = "KZT",
) -> CreatedTenant:
    """Create (or find) the organization in the caller's transaction (caller commits)."""
    name = name.strip()
    email = owner_email.strip().lower()
    if not name:
        raise CliError("--name must not be empty")
    if "@" not in email or len(email) > 255:
        raise CliError("--owner-email is not a valid e-mail")
    if niche not in NICHES:
        raise CliError(f"--niche must be one of {', '.join(NICHES)}")
    if language not in LANGUAGES:
        raise CliError(f"--language must be one of {', '.join(LANGUAGES)}")
    if len(currency) != 3:
        raise CliError("--currency must be a 3-letter ISO code")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CliError(f"unknown --timezone {timezone!r}") from exc
    if owner_password is not None and len(owner_password) < MIN_PASSWORD:
        raise CliError(f"owner password must be at least {MIN_PASSWORD} characters")

    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        existing = db.scalar(
            select(Tenant)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(Tenant.name == name, Membership.user_id == user.id, Membership.role == Role.owner.value)
            .order_by(Tenant.created_at)
            .limit(1)
        )
        if existing is not None:
            branch = db.scalar(
                select(Branch).where(Branch.tenant_id == existing.id).order_by(Branch.created_at).limit(1)
            )
            return CreatedTenant(
                created=False,
                tenant_id=str(existing.id),
                branch_id=str(branch.id) if branch else None,
                owner_user_id=str(user.id),
                owner_email=email,
            )

    tenant = Tenant(name=name, niche=niche, default_language=language, currency=currency.upper(), settings={})
    db.add(tenant)
    db.flush()
    branch = Branch(
        tenant_id=tenant.id, name=(branch_name or name).strip(), address=branch_address, timezone=timezone
    )
    db.add(branch)

    generated = None
    if user is None:
        if owner_password is None:
            generated = owner_password = secrets.token_urlsafe(12)
        user = User(
            email=email,
            full_name=owner_name.strip(),
            password_hash=hash_password(owner_password),
            phone_e164=owner_phone,
            ui_language=language,
        )
        db.add(user)
        db.flush()
    db.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.owner.value))
    db.flush()
    audit(
        db,
        tenant.id,
        "system",
        None,
        "tenant.create",
        "tenant",
        str(tenant.id),
        {"via": "cli", "owner_user_id": str(user.id), "branch_id": str(branch.id)},
    )
    db.flush()
    return CreatedTenant(
        created=True,
        tenant_id=str(tenant.id),
        branch_id=str(branch.id),
        owner_user_id=str(user.id),
        owner_email=email,
        owner_password=generated,
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="AI CRM platform operator commands")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create-tenant", help="create an organization with its owner and first branch")
    c.add_argument("--name", required=True, help="organization name")
    c.add_argument("--owner-email", required=True)
    c.add_argument("--owner-name", default="")
    c.add_argument("--owner-password", default=None, help="else $CRM_OWNER_PASSWORD, else generated")
    c.add_argument("--owner-phone", default=None, help="E.164, e.g. +77010000000")
    c.add_argument("--branch", default=None, help="first branch name (default: organization name)")
    c.add_argument("--branch-address", default="")
    c.add_argument("--timezone", default="Asia/Almaty")
    c.add_argument("--niche", default="barbershop", choices=NICHES)
    c.add_argument("--language", default="ru", choices=LANGUAGES, help="default client language")
    c.add_argument("--currency", default="KZT")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from app.db import SessionLocal

    if args.command == "create-tenant":
        password = args.owner_password or os.environ.get("CRM_OWNER_PASSWORD") or None
        with SessionLocal() as db:
            try:
                result = create_tenant(
                    db,
                    name=args.name,
                    owner_email=args.owner_email,
                    owner_name=args.owner_name,
                    owner_password=password,
                    owner_phone=args.owner_phone,
                    branch_name=args.branch,
                    branch_address=args.branch_address,
                    timezone=args.timezone,
                    niche=args.niche,
                    language=args.language,
                    currency=args.currency,
                )
            except CliError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            db.commit()
        print(json.dumps({k: v for k, v in asdict(result).items() if v is not None}, ensure_ascii=False))
        if result.owner_password:
            print("Owner password was generated and is shown only once - store it now.", file=sys.stderr)
        return 0
    return 1  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
