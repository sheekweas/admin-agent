"""Shared helpers for the load / concurrency scripts (httpx + asyncio only, no extra dependencies).

Environment variables (all optional):
  CRM_BASE_URL      default http://localhost:8000
  CRM_OWNER_EMAIL   default owner@demo.kz   (demo seed)
  CRM_OWNER_PASS    default demo12345       (demo seed)
  CRM_COMPOSE_DIR   directory with docker-compose.yml (for DB verification via psql), default: repo root
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("CRM_BASE_URL", "http://localhost:8000").rstrip("/")
OWNER_EMAIL = os.environ.get("CRM_OWNER_EMAIL", "owner@demo.kz")
OWNER_PASS = os.environ.get("CRM_OWNER_PASS", "demo12345")
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = Path(os.environ.get("CRM_COMPOSE_DIR", str(REPO_ROOT)))
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def login(email: str = OWNER_EMAIL, password: str = OWNER_PASS) -> str:
    r = httpx.post(f"{BASE_URL}/v1/auth/login", json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def latency_summary(ms: list[float]) -> dict[str, float]:
    return {
        "count": len(ms),
        "p50_ms": round(percentile(ms, 50), 1),
        "p95_ms": round(percentile(ms, 95), 1),
        "p99_ms": round(percentile(ms, 99), 1),
        "max_ms": round(max(ms), 1) if ms else float("nan"),
    }


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def psql(sql: str) -> str:
    """Run a read-only verification query inside the compose postgres container."""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "crm", "-d", "crm", "-tAc", sql],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout.strip()


def save_result(name: str, data: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{name}-{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


class Timer:
    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self.t0) * 1000
