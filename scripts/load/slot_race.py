"""Slot race (ТЗ §21 "Создание брони", AT-01) against the running stack.

Scenario 1: N (default 50) concurrent POST /v1/bookings for the SAME staff + start time, each with a different
            client and a different Idempotency-Key  -> exactly 1 x 201 and N-1 x 409 SLOT_UNAVAILABLE.
Scenario 2: M (default 20) concurrent identical POST /v1/bookings with the SAME Idempotency-Key
            -> every response is a success carrying the same booking id; exactly one booking row exists.
The bookings created by the run are cancelled at the end (demo data stays clean).

    python scripts/load/slot_race.py [--racers 50] [--same-key 20]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from collections import Counter
from datetime import date, timedelta

import httpx

from common import BASE_URL, Timer, auth, latency_summary, login, psql, save_result


def pick_target(client: httpx.Client, used: set[tuple[str, str]]) -> dict:
    services = client.get("/v1/services", params={"published": "true", "limit": 200}).json()["items"]
    staff = [
        s for s in client.get("/v1/staff", params={"limit": 200}).json()["items"] if s.get("active", True)
    ]
    random.shuffle(services)
    for svc in services:
        for st in [s for s in staff if svc["id"] in (s.get("service_ids") or [])]:
            day = date.today() + timedelta(days=random.randint(5, 20))
            slots = client.get(
                "/v1/availability",
                params={
                    "service_id": svc["id"],
                    "staff_id": st["id"],
                    "date_from": day.isoformat(),
                    "date_to": (day + timedelta(days=3)).isoformat(),
                },
            ).json()["items"]
            slots = [s for s in slots if (s["staff_id"], s["start_at"]) not in used]
            if slots:
                slot = random.choice(slots)
                used.add((slot["staff_id"], slot["start_at"]))
                return {"service_id": svc["id"], "staff_id": slot["staff_id"], "start_at": slot["start_at"]}
    raise SystemExit("no free slot found - is the demo tenant seeded?")


async def post_booking(ac: httpx.AsyncClient, body: dict, key: str, gate: asyncio.Event) -> dict:
    await gate.wait()  # release all requests at once
    with Timer() as t:
        try:
            r = await ac.post("/v1/bookings", json=body, headers={"Idempotency-Key": key})
            status, data = r.status_code, (r.json() if r.content else {})
        except httpx.HTTPError as exc:
            status, data = 0, {"error": repr(exc)}
    return {"status": status, "data": data, "ms": t.ms}


async def burst(token: str, bodies: list[tuple[dict, str]]) -> list[dict]:
    limits = httpx.Limits(max_connections=len(bodies), max_keepalive_connections=len(bodies))
    async with httpx.AsyncClient(base_url=BASE_URL, headers=auth(token), timeout=60, limits=limits) as ac:
        gate = asyncio.Event()
        jobs = [asyncio.create_task(post_booking(ac, b, k, gate)) for b, k in bodies]
        await asyncio.sleep(0.2)
        gate.set()
        return await asyncio.gather(*jobs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--racers", type=int, default=50)
    ap.add_argument("--same-key", type=int, default=20)
    args = ap.parse_args()

    token = login()
    client = httpx.Client(base_url=BASE_URL, headers=auth(token), timeout=30)
    contacts = client.get("/v1/contacts", params={"limit": 200}).json()["items"]
    if len(contacts) < args.racers:
        raise SystemExit(f"need >= {args.racers} contacts in the demo tenant, found {len(contacts)}")
    used: set[tuple[str, str]] = set()
    run = uuid.uuid4().hex[:8]
    ok = True

    # ---------------------------------------------------------------- 1. different keys, same slot
    target = pick_target(client, used)
    bodies = [
        ({**target, "contact_id": contacts[i]["id"], "comment": f"load slot-race {run}"}, f"race-{run}-{i}")
        for i in range(args.racers)
    ]
    res = asyncio.run(burst(token, bodies))
    codes = Counter(r["status"] for r in res)
    conflict_codes = Counter(r["data"].get("detail", {}).get("code") for r in res if r["status"] == 409)
    with_alternatives = sum(1 for r in res if r["status"] == 409 and r["data"]["detail"].get("alternatives"))
    created = [r["data"]["id"] for r in res if r["status"] == 201]
    rows = int(
        psql(
            "SELECT count(*) FROM bookings WHERE staff_id = '{}' AND start_at = '{}' "
            "AND status NOT IN ('cancelled','rescheduled')".format(target["staff_id"], target["start_at"])
        )
    )
    s1 = {
        "target": target,
        "status_codes": dict(codes),
        "conflict_codes": dict(conflict_codes),
        "conflicts_with_alternatives": with_alternatives,
        "active_rows_for_slot": rows,
        "latency": latency_summary([r["ms"] for r in res]),
    }
    s1_pass = codes.get(201) == 1 and codes.get(409) == args.racers - 1 and rows == 1
    s1["pass"] = s1_pass
    ok &= s1_pass
    print("slot race (different keys):", s1)

    # ---------------------------------------------------------------- 2. same key, same body
    target2 = pick_target(client, used)
    key = f"same-{run}"
    body = {**target2, "contact_id": contacts[0]["id"], "comment": f"load same-key {run}"}
    res2 = asyncio.run(burst(token, [(body, key)] * args.same_key))
    codes2 = Counter(r["status"] for r in res2)
    ids = {r["data"].get("id") for r in res2 if r["status"] in (200, 201)}
    rows2 = int(psql(f"SELECT count(*) FROM bookings WHERE idempotency_key = '{key}'"))
    s2 = {
        "target": target2,
        "status_codes": dict(codes2),
        "distinct_booking_ids": len(ids),
        "rows_with_key": rows2,
        "errors": [r["data"] for r in res2 if r["status"] not in (200, 201)][:3],
        "latency": latency_summary([r["ms"] for r in res2]),
    }
    s2_pass = sum(codes2.get(c, 0) for c in (200, 201)) == args.same_key and len(ids) == 1 and rows2 == 1
    s2["pass"] = s2_pass
    ok &= s2_pass
    print("same idempotency key:", s2)

    # ---------------------------------------------------------------- cleanup
    for bid in set(created) | (ids - {None}):
        client.patch(f"/v1/bookings/{bid}", json={"action": "cancel", "reason": "load test cleanup"})

    path = save_result("slot_race", {"racers": args.racers, "same_key": args.same_key, "s1": s1, "s2": s2})
    print(f"\n{'PASS' if ok else 'FAIL'} - results saved to {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
