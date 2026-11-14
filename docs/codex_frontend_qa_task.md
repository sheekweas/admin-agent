# Task for Codex: integration QA of the admin panel against the LIVE backend

The backend is now complete and running (docker compose): API at http://localhost:8000 (OpenAPI schema at
http://localhost:8000/openapi.json, Swagger at /docs), frontend container at http://localhost:3000.
Demo tenant "Barber KZ", password `demo12345` for: owner@demo.kz, admin@demo.kz, backup@demo.kz,
barber@demo.kz (role staff), analyst@demo.kz.

Your job: make the admin panel in `frontend/` work correctly against the REAL API. Use sub-agents if available
(e.g. split pages between them). Edit ONLY files in `frontend/`. Do not touch `backend/`. Do not git commit/push.

1. Fetch `/openapi.json` and compare every request the frontend makes (paths, methods, query params, bodies,
   headers like Idempotency-Key) and every TypeScript type in `src/lib/types.ts` with the real schemas. Then, logged
   in as owner, call each GET endpoint the panel uses with real data and compare actual JSON to the types (field
   names, nesting, nullability, money as strings, `{items,total}` lists, percentages 0–100). Fix all mismatches in
   the frontend.
2. Exercise the main mutations through the API exactly as the UI would (create booking from availability slot,
   409 alternatives flow, reschedule, cancel, status changes, payment + refund, lead stage change, conversation
   takeover/release/reply, simulate WhatsApp message, admin task ack/resolve, knowledge create/publish/test,
   shift request create/decide, team member create, tenant settings patch, integrations PUT/test) and make sure the
   UI code sends what the API accepts.
3. Role checks: staff (barber@demo.kz) and analyst must see only what their `permissions[]` allow; no page should
   crash on 403 — show a friendly "no access" state.
4. Visual check: if a browser tool / Playwright is available, open each page at desktop (1440px) and mobile (390px)
   widths as owner, take screenshots into `frontend/qa-screenshots/` (gitignored is fine) and fix broken layouts,
   console errors, untranslated keys, and wrong time zones (times must be shown in Asia/Almaty for Almaty branch).
5. Re-run `npm run lint`, `npx tsc --noEmit`, `npx vitest run`, `npm run build` — all must pass.
6. Write `docs/QA_FRONTEND.md`: what you checked, what you fixed, and any BACKEND bugs or contract gaps you found
   (with exact endpoint, request, actual response, expected) — do not fix backend yourself.
Print a concise summary at the end.
