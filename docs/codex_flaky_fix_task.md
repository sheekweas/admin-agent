# Task for Codex: fix two intermittent E2E failures (real UI bug suspected) — stage 4 gate

Running the full Playwright suite twice against the rebuilt docker stack (API :8000, panel :3000) gave intermittent
failures; each test passes when run alone. Edit ONLY `frontend/`. Use sub-agents if available. No git commit/push,
don't stop containers.

## 1. Inbox: typed reply disappears (REAL USER-FACING BUG)
`e2e/communication.spec.ts:38` ("inbox: simulator AI reply, human takeover and return to AI") timed out (desktop
in one run, mobile in another). The trace shows: after "Взять диалог" → confirm, the test fills the textarea
"Ответ клиенту", then clicking "Отправить" waits forever because the button is `disabled` — the screenshot
(`frontend/test-results/communication-inbox-simula-fe3d6-n-takeover-and-return-to-AI-desktop/test-failed-1.png`,
trace.zip in the same folder) shows the conversation in "Человек отвечает" mode and an EMPTY textarea.
So the draft text is lost after it was typed — an admin would lose what they wrote. Find the root cause
(candidates: `selectConversation()` running again / `setText("")` from a late async path, the component or form
remounting on refetch (5 s polling of messages/conversations, detail refetch after takeover, session/permissions
refetch), `can("conversations.write")` briefly false while `/v1/auth/me` refetches, list re-sorting causing a click
on a different conversation, etc.). Reproduce deterministically (e.g., slow down responses with `page.route` delays,
or run with many existing conversations as now in the DB) and fix it properly:
- the draft must never be wiped by background refetches or mode changes; keep drafts per conversation id (state map,
  optionally sessionStorage) and clear only after a successful send or explicit conversation switch with an empty
  draft;
- the form must not unmount during permission/session refetches.
Add a unit test (vitest + Testing Library) reproducing the loss, and harden the E2E (wait for the takeover response
before typing; assert the textarea value before clicking send).

## 2. Barber sees own bookings (flaky)
`e2e/access.spec.ts:69` mobile failed once: `locator('main button').filter({ hasText: 'Арман' })` expected 1,
received 0 (15 s). Determine whether it is test-data dependent (the barber's booking may fall outside the visible
calendar range / day, or be hidden by accumulated data from previous runs) or a UI bug, and make the test create its
own booking for the barber's staff member in the visible range via the API, then assert it.

## 3. Prove stability
Run the FULL suite 3 times in a row against http://localhost:3000 (the docker panel) — if your UI fix needs the new
build, build and run the panel on :3100 and run 3 times there; all 42 must pass every time. Also `npm run lint`,
`npx tsc --noEmit`, `npx vitest run`, `npm run build`. Document root causes + fixes in `frontend/docs/E2E.md`.
Print a concise summary (root causes!) at the end.
