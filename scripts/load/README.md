# Load / concurrency scripts

Python + httpx + asyncio only (httpx is already a backend dependency). Run them against the docker stack.
Results are printed and saved as JSON to `scripts/load/results/`; the latest numbers are in `docs/LOAD_TEST.md`.

## 1. Start and seed the stack (PowerShell, from the repo root `C:\Projects\admin`)

```powershell
cd C:\Projects\admin
docker compose up -d --build
docker compose exec -T api python -m app.seed      # demo tenant "Barber KZ" (idempotent)
curl.exe -s -o NUL -w "%{http_code}`n" http://localhost:8000/docs   # expect 200
```

## 2. Run (use the backend virtualenv's Python; the scripts import `common.py` from their folder)

```powershell
cd C:\Projects\admin\scripts\load
$py = "C:\Projects\admin\backend\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"

& $py slot_race.py                 # 50 racers for one slot + 20 requests with the same Idempotency-Key
& $py webhook_burst.py             # 500 signed WhatsApp webhooks (100 duplicates), concurrency 50
& $py panel_load.py                # 20 virtual users x 60 s on dashboard / calendar / contacts / chats
```

Git Bash equivalent: `cd /c/Projects/admin/scripts/load && PYTHONIOENCODING=utf-8 ../../backend/.venv/Scripts/python slot_race.py`.

Each script exits with code 0 on PASS and 1 on FAIL.

## Options

| Script | Options (defaults) |
|---|---|
| `slot_race.py` | `--racers 50 --same-key 20` |
| `webhook_burst.py` | `--total 500 --duplicates 100 --concurrency 50 --clients 40 --settle 120` |
| `panel_load.py` | `--users 20 --duration 60` |

Environment: `CRM_BASE_URL` (default `http://localhost:8000`), `CRM_OWNER_EMAIL` / `CRM_OWNER_PASS`
(default demo owner), `CRM_COMPOSE_DIR` (folder with `docker-compose.yml`, used for read-only `psql` checks).

## What the scripts touch

- `webhook_burst.py` sets the demo tenant's WhatsApp integration through `PUT /v1/integrations/whatsapp`
  (owner): keeps its `phone_number_id` (or sets `LOADTEST-PNID`) and stores a **new random `app_secret`** that
  is used to sign the payloads. The platform `WHATSAPP_APP_SECRET` is not needed and nothing secret is printed.
  It creates 40 contacts `+7708xxxxxxx` per run.
- `slot_race.py` books random free future slots and cancels everything it created at the end.
- `panel_load.py` is read-only.
- Verification queries run inside the postgres container: `docker compose exec -T postgres psql -U crm -d crm`.

Note: `WHATSAPP_MODE=mock` runs the AI turn inside the API process, so webhook ACK latency includes AI load.
The webhook rate limit (`RATE_LIMIT_WEBHOOK_PER_MINUTE`, default 600/min per IP) must stay above the burst size.
