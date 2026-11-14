# Task for Codex: browser E2E tests for the admin panel (stage 3)

The full stack runs in docker: API http://localhost:8000, panel http://localhost:3000, demo tenant "Barber KZ"
(password `demo12345`: owner@demo.kz, admin@demo.kz, backup@demo.kz, barber@demo.kz, analyst@demo.kz).
Claude agents are working on `backend/` in parallel — edit ONLY `frontend/` and `.github/workflows/ci.yml`
(the `e2e` job only). Use sub-agents if available. Do not git commit/push. Do not stop docker containers.

1. Add Playwright (`@playwright/test`, chromium only) to `frontend/` with `playwright.config.ts`
   (baseURL from `E2E_BASE_URL`, default http://localhost:3000; API from `E2E_API_URL`), `npm run e2e` script,
   traces/screenshots on failure, `frontend/e2e/` folder. Tests must be independent and create their own data via
   the API (unique names/phones), never relying on fixed demo ids; clean up what they can.
2. Scenarios (desktop 1440 and mobile 390 projects where it makes sense):
   - login as owner, org name visible; logout; wrong password error; 401 redirect.
   - RU ↔ KZ language switch persists; dark theme toggle.
   - Calendar: create a booking for a new client through the UI (service → slot → confirm), it appears in the grid
     with "confirmed" color; open drawer → check-in → complete → add payment → revenue on dashboard increases.
   - Conflict: occupy a slot via API, try to book it in the UI → 409 alternatives shown → pick one → booked.
   - Inbox: simulate WhatsApp message (dev button) → AI reply visible with "AI отвечает" badge; take over → send human
     reply → badge "человек отвечает"; return to AI.
   - Tasks: ask the AI about a 30% discount via simulate → task appears → Ack → Resolve with relay → client sees answer
     in chat.
   - Leads kanban: drag/move a lead to "lost" → reason required.
   - Staff role (barber@demo.kz): only own schedule & bookings, no Finance/Analytics/Integrations in nav, direct URL
     shows "no access" state; analyst sees reports but cannot edit.
   - Accessibility smoke: `@axe-core/playwright` on dashboard, calendar, inbox, clients — no serious/critical violations
     (fix them in the UI).
3. CI: add a job `e2e` to `.github/workflows/ci.yml` that runs `docker compose up -d --build`, seeds
   (`docker compose exec -T api python -m app.seed`), waits for health, runs `npx playwright install --with-deps chromium`
   and `npm run e2e`, uploads the Playwright report as an artifact. It needs `cp .env.example .env` first.
4. Run everything locally until green; also `npm run lint`, `npx tsc --noEmit`, `npx vitest run`, `npm run build`.
   If the frontend container must be rebuilt to include your UI fixes, say so in the summary (you may not have
   Docker access) — and for local E2E runs you may instead start `npm run build && npm run start -- -p 3100` and point
   `E2E_BASE_URL` at it.
5. Write `frontend/docs/E2E.md` (how to run, scenarios, results). If you find BACKEND bugs, document them there with
   exact request/response — do not edit backend.
Print a concise summary at the end.
