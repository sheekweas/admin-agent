"""WhatsApp webhook burst (ТЗ §21 "Доставка событий", §22.1 Load, AT-02) against the running stack.

Sends N signed webhooks (default 500 = 400 unique + 100 redeliveries of already-sent message ids) at
concurrency C (default 50) from a pool of simulated clients, then verifies in the database:
  * no 5xx / transport errors, p50/p95/p99 acknowledgement latency;
  * stored inbound messages == unique message ids (no duplicates), one contact + one conversation per client;
  * the AI answered every unique message at most once (wait for background processing).

Setup: the demo tenant gets a WhatsApp integration through the API (PUT /v1/integrations/whatsapp as owner)
with a per-run random app_secret used to sign the payloads - no platform secret is needed or printed.
The existing phone_number_id of the tenant is kept when there is one.

    python scripts/load/webhook_burst.py [--total 500] [--duplicates 100] [--concurrency 50] [--clients 40]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import secrets
import sys
import time
import uuid
from collections import Counter

import httpx

from common import BASE_URL, Timer, auth, latency_summary, login, psql, save_result, sign

TEXTS = [
    "Здравствуйте",
    "Сколько стоит мужская стрижка?",
    "Где вы находитесь?",
    "Сәлеметсіз бе",
    "Добрый день, подскажите адрес",
]


def setup_integration(token: str) -> tuple[str, str]:
    client = httpx.Client(base_url=BASE_URL, headers=auth(token), timeout=30)
    items = client.get("/v1/integrations").json()["items"]
    current = next((i for i in items if i.get("kind") == "whatsapp"), None) or {}
    config = dict(current.get("config") or {})
    pnid = str(config.get("phone_number_id") or "LOADTEST-PNID")
    config["phone_number_id"] = pnid
    app_secret = secrets.token_urlsafe(24)
    new_secrets = {"app_secret": app_secret}
    if not (current.get("secrets") or {}).get("access_token"):
        new_secrets["access_token"] = "loadtest-token"
    r = client.put(
        "/v1/integrations/whatsapp", json={"config": config, "secrets": new_secrets, "enabled": True}
    )
    r.raise_for_status()
    return pnid, app_secret


def payload(pnid: str, wa_id: str, mid: str, text: str) -> bytes:
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA-LOAD",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "77000000000", "phone_number_id": pnid},
                            "contacts": [{"profile": {"name": f"Load {wa_id[-3:]}"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": mid,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(body, ensure_ascii=False).encode()


async def run_burst(jobs: list[bytes], secret: str, concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60, limits=limits) as ac:

        async def one(body: bytes) -> dict:
            async with sem:
                with Timer() as t:
                    try:
                        r = await ac.post(
                            "/v1/webhooks/whatsapp",
                            content=body,
                            headers={
                                "Content-Type": "application/json",
                                "X-Hub-Signature-256": sign(secret, body),
                            },
                        )
                        status = r.status_code
                        accepted = r.json().get("accepted") if status == 200 else None
                    except httpx.HTTPError as exc:
                        status, accepted = 0, repr(exc)
                return {"status": status, "accepted": accepted, "ms": t.ms}

        t0 = time.perf_counter()
        res = await asyncio.gather(*(one(b) for b in jobs))
        wall = time.perf_counter() - t0
    for r in res:
        r["wall_s"] = wall
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=500)
    ap.add_argument("--duplicates", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--clients", type=int, default=40)
    ap.add_argument("--settle", type=int, default=120, help="max seconds to wait for AI processing")
    args = ap.parse_args()

    token = login()
    pnid, secret = setup_integration(token)
    run = uuid.uuid4().hex[:8]
    # 7708<run-specific 3 digits><4 digits>: valid KZ mobile numbers, unique per run
    prefix = f"7708{random.randint(100, 999)}"
    wa_ids = [f"{prefix}{i:04d}" for i in range(args.clients)]
    unique_n = args.total - args.duplicates
    uniques = []
    for n in range(unique_n):
        wa_id = wa_ids[n % len(wa_ids)]
        uniques.append(payload(pnid, wa_id, f"wamid.LOAD.{run}.{n}", random.choice(TEXTS)))
    dups = random.sample(uniques, args.duplicates)
    # Redeliveries are interleaved with first deliveries, as Meta does under retries.
    jobs = uniques + dups
    random.shuffle(jobs)

    res = asyncio.run(run_burst(jobs, secret, args.concurrency))
    codes = Counter(r["status"] for r in res)
    accepted = sum(r["accepted"] or 0 for r in res if isinstance(r["accepted"], int))
    wall = res[0]["wall_s"] if res else 0

    # ------------------------------------------------------------ verification in the DB
    like = f"wamid.LOAD.{run}.%"
    stored = int(psql(f"SELECT count(*) FROM messages WHERE provider_message_id LIKE '{like}'"))
    distinct = int(
        psql(
            f"SELECT count(DISTINCT provider_message_id) FROM messages WHERE provider_message_id LIKE '{like}'"
        )
    )
    phones = ",".join(f"'+{w}'" for w in wa_ids)
    contacts = int(
        psql(f"SELECT count(*) FROM contacts WHERE phone_e164 IN ({phones}) AND deleted_at IS NULL")
    )
    convs = int(
        psql(
            "SELECT count(*) FROM conversations c JOIN contacts k ON k.id = c.contact_id "
            f"WHERE k.phone_e164 IN ({phones})"
        )
    )
    # Wait until the AI processed every unique inbound (inline background task in mock mode / Celery live).
    deadline = time.time() + args.settle
    while True:
        unprocessed = int(
            psql(
                f"SELECT count(*) FROM messages WHERE provider_message_id LIKE '{like}' AND processed_at IS NULL"
            )
        )
        if unprocessed == 0 or time.time() > deadline:
            break
        time.sleep(2)
    replies = int(
        psql(
            "SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            f"JOIN contacts k ON k.id = c.contact_id WHERE k.phone_e164 IN ({phones}) AND m.direction = 'out'"
        )
    )

    summary = {
        "run": run,
        "requests": len(jobs),
        "unique": unique_n,
        "duplicates": args.duplicates,
        "concurrency": args.concurrency,
        "clients": args.clients,
        "status_codes": dict(codes),
        "accepted_total": accepted,
        "throughput_rps": round(len(jobs) / wall, 1) if wall else None,
        "wall_s": round(wall, 2),
        "ack_latency": latency_summary([r["ms"] for r in res]),
        "stored_inbound": stored,
        "distinct_inbound": distinct,
        "contacts": contacts,
        "conversations": convs,
        "unprocessed_after_settle": unprocessed,
        "ai_replies": replies,
    }
    checks = {
        "no_5xx_or_transport_errors": all(200 <= c < 500 for c in codes) and codes.get(0, 0) == 0,
        "all_200": codes.get(200, 0) == len(jobs),
        "accepted_equals_unique": accepted == unique_n,
        "stored_equals_unique": stored == unique_n == distinct,
        "one_contact_per_client": contacts == args.clients,
        "one_conversation_per_client": convs == args.clients,
        "all_processed": unprocessed == 0,
        "no_duplicate_replies": replies <= unique_n,
    }
    summary["checks"] = checks
    ok = all(checks.values())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    path = save_result("webhook_burst", summary)
    print(f"\n{'PASS' if ok else 'FAIL'} - results saved to {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
