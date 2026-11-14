# Task for Codex: verify the "Calls" UI against the now-LIVE voice backend (stage 4 QA)

The backend voice endpoints are now implemented and running (docker rebuilt): API http://localhost:8000,
panel http://localhost:3000 (the container may hold your previous build; for E2E you can build and run the panel on
port 3100 as before). Demo password `demo12345` (owner@demo.kz, admin@demo.kz, barber@demo.kz, analyst@demo.kz).
Edit ONLY `frontend/`. Use sub-agents if available. Do not git commit/push, do not stop docker containers.

Read `docs/API.md` section "Voice" (final contract, includes small additions: optional `admin_task_id` on
`POST /v1/calls/outbound`; extra errors 422 `BOOKING_REQUIRED`/`BOOKING_CONTACT_MISMATCH`/`NO_PHONE`, 503
`VOICE_NOT_CONFIGURED`; a provider failure returns 201 with `status:"failed"`; `/v1/reports/voice` requires
`reports.read`; `purpose`/`status` filters accept comma-separated lists; voice conversations now also appear in the
inbox with `channel:"voice"`; `PATCH /v1/tenant` replaces `settings.voice` as a whole) and `/openapi.json`.

1. Compare your types/requests with the real OpenAPI schemas and real JSON from the live API; fix mismatches.
2. Map all new error codes to friendly RU/KZ messages. Make the inbox render `channel:"voice"` conversations
   correctly (phone icon, voice_turn messages). Ensure the voice settings form sends the whole `settings.voice`
   object (merge with current values).
3. Un-skip the 4 skipped Playwright voice tests and make them pass against the live API: simulate an inbound call
   that books (see list + detail + transcript + booking link), outbound without consent → CONSENT_REQUIRED message,
   outbound with consent (set `consent_flags.outbound_calls` via PATCH contact) → call appears, barber has no access,
   analyst sees the Voice card in Analytics.
4. Run the FULL Playwright suite + `npm run lint`, `npx tsc --noEmit`, `npx vitest run`, `npm run build` — all green.
5. Update `frontend/docs/VOICE_UI.md` with results and any backend issues (exact request/response).
Print a concise summary at the end.
