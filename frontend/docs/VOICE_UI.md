# Voice UI - stage 4 live QA

QA date: 2026-11-14. Changes are limited to `frontend/`. No commits, pushes, or Docker container stops.

## Contract and fixes

- Read `docs/API.md` Voice and fetched live `/openapi.json` into `openapi.qa.json`; regenerated `src/lib/api-schema.ts`. Calls and inbox reuse generated types, including nullable phones, contacts and timestamps.
- Transcript confidence is **0–1** in OpenAPI and real JSON (`0.95` observed), displayed as **95%**. Voice confidence thresholds save as fractions; legacy percentage settings are normalized on save.
- List/report date filters send tenant-local inclusive `YYYY-MM-DD` dates. Purpose/status comma-separated filters were verified against real calls.
- Friendly RU/KZ errors cover `CONSENT_REQUIRED`, `OUTSIDE_HOURS`, `DAILY_CAP_REACHED`, `BOOKING_REQUIRED`, `BOOKING_CONTACT_MISMATCH`, `NO_PHONE`, `VOICE_NOT_CONFIGURED`.
- Outbound supports optional `admin_task_id`. HTTP 201 with `status:"failed"` displays provider failure and a detail link. This branch is unit tested; live QA uses the mock provider.
- Voice reports and the Analytics Voice card use `reports.read`; analysts see KPIs without a Calls link. Calls read/write controls retain their conversation permissions.
- Inbox voice conversations show phone icons. `voice_turn` messages show text, speaker and a call-turn label without chat delivery ticks; admin speakers are labeled correctly.
- Settings fetch the latest tenant immediately before saving and merge edits into the **whole** `settings.voice` object. Hidden keys and unrelated settings survive replacement. Live E2E changes a hidden value after opening the form and checks the save request and persisted result, then restores settings.
- Calls retain responsive list/detail, pagination, filters, recording availability and contact/booking/task links. Simulator preserves empty lines for silence. Populated live data revealed incorrect accessible date-button labels, now fixed.

## Live evidence

Owner calls list/detail and voice report returned 200. Observed report:

```json
{"calls":4,"answered":4,"avg_duration_sec":1,"avg_first_response_ms":44.5,"transfer_rate":25,"booking_rate":50,"recognition_retry_rate":0}
```

Exact request/response for revoked consent:

```http
POST /v1/calls/outbound
Content-Type: application/json

{"contact_id":"bc611805-dfd7-4b5c-a4f3-57f2e03f128b","purpose":"callback"}

HTTP/1.1 409 Conflict
{"detail":{"code":"CONSENT_REQUIRED","message":"The client has not consented to outbound calls"}}
```

After PATCH contact with `consent_flags.outbound_calls:true`, the identical outbound request returned:

```http
HTTP/1.1 201 Created
{"id":"8d517927-7b5b-4050-9414-e10dd58c81dc","direction":"out","purpose":"callback","status":"ringing","from_number":null,"to_number":"+78407181788","contact":{"id":"bc611805-dfd7-4b5c-a4f3-57f2e03f128b","name":"Voice consent-1794636684071-9gfguqn","phone_e164":"+78407181788"},"admin_task_id":null,"booking_id":null,"started_at":null,"ended_at":null,"duration_sec":0,"transferred_to_human":false,"recording_available":false,"created_at":"2026-11-14T06:11:24.665470+00:00"}
```

Tests attach exact simulation, consent rejection and successful outbound request/response JSON to the Playwright report. Simulation commits a real booking, checks list/detail/transcript, follows its booking link, then checks voice messages in Inbox. Test booking resources are cleaned up. Outbound tests temporarily enable all hours, disable quiet hours and raise the cap, then restore tenant settings and revoke test-contact consent in `finally`.

Barber calls list and voice report: 403. Analyst voice report: 200; calls list: 403. No backend defect was found in exercised scenarios. Actual telephony, signed provider webhooks, recordings and real provider failures were not exercised by frontend mock-mode QA.

## Verification

Final results against the fresh production build at `http://localhost:3100` and live API at `http://localhost:8000`:

- Full `npx playwright test`: **42 passed, 0 skipped, 0 failed** (desktop/mobile, 52.5 seconds). Includes the four formerly skipped voice cases, analyst Voice analytics, settings preservation and Calls accessibility.
- `npm run lint`: passed (zero warnings).
- `npx tsc --noEmit`: passed.
- `npx vitest run`: **34 passed across 7 files**.
- `npm run build`: passed.

The early browser runs found frontend-only issues: a simulator language locator, Calls date-button accessible names, and the analyst's Calls link. Each was corrected before the successful full run. The final Playwright HTML report is in `playwright-report/index.html`.

Use a fresh production build on port 3100 with the Next rewrite proxy to avoid alternate-port CORS restrictions:

```powershell
$env:NEXT_PUBLIC_API_URL='/e2e-api'
$env:E2E_API_URL='http://localhost:8000'
npm run build
npm exec -- next start --port 3100
# In another terminal:
$env:E2E_BASE_URL='http://localhost:3100'
$env:E2E_API_URL='http://localhost:8000'
npx playwright test
npm run lint
npx tsc --noEmit
npx vitest run
```

API fixture sessions are cached per role for five minutes within each worker to respect the live login rate limit; explicit UI login tests still authenticate normally. Voice deployment skips have been removed.
