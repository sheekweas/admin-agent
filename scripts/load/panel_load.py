"""Admin-panel read load (ТЗ §21 "Ответ API": p95 <= 2 s for typical panel requests) against the running stack.

V virtual users (default 20) loop for D seconds (default 60) over the requests the panel issues for its
main screens - dashboard (reports overview), calendar (bookings for a week), contacts, conversations (inbox)
- with no think time (a worst case for 20 concurrent operators). Reports p50/p95/p99, error rate and RPS
per screen and overall, and compares p95 with the 2 s target.

    python scripts/load/panel_load.py [--users 20] [--duration 60]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import httpx

from common import BASE_URL, Timer, auth, latency_summary, login, save_result

TARGET_P95_MS = 2000


def screens() -> dict[str, tuple[str, dict]]:
    today = date.today()
    week = today - timedelta(days=today.weekday())
    return {
        "dashboard": (
            "/v1/reports/overview",
            {"date_from": (today - timedelta(days=30)).isoformat(), "date_to": today.isoformat()},
        ),
        "calendar": (
            "/v1/bookings",
            {"date_from": week.isoformat(), "date_to": (week + timedelta(days=6)).isoformat(), "limit": 200},
        ),
        "contacts": ("/v1/contacts", {"limit": 50}),
        "conversations": ("/v1/conversations", {"limit": 50}),
    }


async def user(ac: httpx.AsyncClient, until: float, out: dict[str, list], errors: dict[str, list]) -> None:
    sc = screens()
    names = list(sc)
    while time.perf_counter() < until:
        name = random.choice(names)
        path, params = sc[name]
        with Timer() as t:
            try:
                r = await ac.get(path, params=params)
                status = r.status_code
            except httpx.HTTPError as exc:
                status = 0
                errors[name].append(repr(exc))
        out[name].append(t.ms)
        if status != 200:
            errors[name].append(status)


async def run(token: str, users: int, duration: int) -> tuple[dict, dict, float]:
    out: dict[str, list] = defaultdict(list)
    errors: dict[str, list] = defaultdict(list)
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=BASE_URL, headers=auth(token), timeout=30, limits=limits) as ac:
        for path, params in screens().values():  # warm-up
            (await ac.get(path, params=params)).raise_for_status()
        t0 = time.perf_counter()
        await asyncio.gather(*(user(ac, t0 + duration, out, errors) for _ in range(users)))
        wall = time.perf_counter() - t0
    return out, errors, wall


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--duration", type=int, default=60)
    args = ap.parse_args()

    token = login()
    out, errors, wall = asyncio.run(run(token, args.users, args.duration))
    per_screen = {}
    for name, ms in out.items():
        per_screen[name] = {
            **latency_summary(ms),
            "errors": len(errors.get(name, [])),
            "error_rate": round(len(errors.get(name, [])) / len(ms), 4) if ms else None,
            "error_samples": [str(e) for e in errors.get(name, [])[:3]],
        }
    all_ms = [x for ms in out.values() for x in ms]
    all_err = sum(len(e) for e in errors.values())
    overall = {
        **latency_summary(all_ms),
        "errors": all_err,
        "error_rate": round(all_err / len(all_ms), 4) if all_ms else None,
        "rps": round(len(all_ms) / wall, 1),
    }
    ok = (
        overall["p95_ms"] <= TARGET_P95_MS
        and overall["error_rate"] == 0
        and all(s["p95_ms"] <= TARGET_P95_MS for s in per_screen.values())
    )
    summary = {
        "users": args.users,
        "duration_s": args.duration,
        "target_p95_ms": TARGET_P95_MS,
        "overall": overall,
        "screens": per_screen,
        "pass": ok,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    path = save_result("panel_load", summary)
    print(f"\n{'PASS' if ok else 'FAIL'} - results saved to {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
