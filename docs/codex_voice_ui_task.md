# Task for Codex: "Calls" (voice) section of the admin panel — stage 4

A Claude agent is implementing the backend voice channel right now (endpoints below may not exist yet while you
start — build against the contract, mock responses in unit tests, and integrate against the live API at the end:
API http://localhost:8000, panel http://localhost:3000, demo password `demo12345`, owner@demo.kz). Edit ONLY
`frontend/`. Use sub-agents if available. Do not git commit/push, do not stop docker containers.

Read `docs/SPEC_ru.txt` §13 and §11.1, and the existing frontend structure (`src/lib/api.ts`, `src/lib/types.ts`,
`src/features/*`, i18n dictionaries RU/KZ).

## API contract (Voice)
List responses are `{"items":[...],"total":N}`; datetimes ISO with offset; percentages 0–100.
- `GET /v1/calls?direction=in|out&purpose=client_ai|admin_escalation|reminder|callback|booking_confirmation|reschedule&status&contact_id&date_from&date_to&limit&offset`
  item: `{id, direction, purpose, status, from_number, to_number, contact:{id,name,phone_e164}|null, admin_task_id,
  booking_id|null, started_at, ended_at, duration_sec, transferred_to_human, recording_available, created_at}`
  status values: queued, ringing, in_progress, answered, completed, no_answer, busy, failed, cancelled.
- `GET /v1/calls/{id}` → item + `transcript:[{speaker:"client"|"ai"|"admin"|"system", text, at, confidence|null}]`,
  `metrics:{first_response_ms, turns, recognition_retries, transferred_to_human, booking_created}`
- `POST /v1/calls/outbound {contact_id, purpose:"booking_confirmation"|"reminder"|"callback"|"reschedule", booking_id?}`
  → 201 call item; errors 409 `CONSENT_REQUIRED` | `OUTSIDE_HOURS` | `DAILY_CAP_REACHED` (show friendly messages).
- `POST /v1/calls/simulate {phone, name?, utterances:[string], language?:"ru"|"kk"}` (mock mode only) → call detail.
- `GET /v1/reports/voice?date_from&date_to` → `{calls, answered, avg_duration_sec, avg_first_response_ms,
  transfer_rate, booking_rate, recognition_retry_rate}`
Permissions: reading needs `conversations.read`; outbound needs `conversations.write`.

## UI
1. New nav item "Звонки / Қоңыраулар" `/calls` (icon phone), hidden without `conversations.read`.
2. Calls list: filters (direction, purpose, status, date range), table/cards (mobile), status badges, duration, links
   to contact and booking, "transferred to human" indicator, pagination.
3. Call detail drawer/page: metadata, metrics, transcript as a chat-like timeline with speaker styling and
   confidence hints (low confidence highlighted), link to the admin task for escalation calls.
4. KPI strip on the calls page from `/v1/reports/voice`; add a "Voice" card to Analytics.
5. "Call client" button on the client card and in the booking drawer (purpose select) with confirmation dialog;
   disabled with tooltip when the client has no outbound-call consent (`consent_flags.outbound_calls`).
6. Dev-only "Simulate call" dialog on /calls (like the WhatsApp simulator): phone, language, list of utterances →
   shows resulting transcript.
7. Integrations → Voice card: show phone number, provider, test button (existing endpoints), plus voice settings
   in Settings (recording on/off, transcript retention days, max outbound per day, low-confidence threshold,
   TTS voice RU/KK) — stored via `PATCH /v1/tenant` under `settings.voice` (read keys from `GET /v1/tenant`).
8. RU/KZ translations for everything; accessible; loading/empty/error states.
9. Tests: vitest for transcript rendering + error mapping; Playwright e2e `e2e/calls.spec.ts` (simulate a call that
   books, see it in list/detail; outbound without consent shows CONSENT_REQUIRED message; barber has no access).
10. `npm run lint`, `npx tsc --noEmit`, `npx vitest run`, `npm run build`, and the full Playwright suite must pass
    (against the live API once the backend endpoints exist; if they are still missing when you finish, say so).
Document ambiguities and backend issues in `frontend/docs/VOICE_UI.md`. Print a concise summary at the end.
