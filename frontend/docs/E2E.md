# Browser E2E tests

The suite exercises the real seeded API and frontend with Chromium at 1440×1000
and 390×844. Use a disposable demo database: tests write bookings, payments,
contacts, conversations, tasks, leads, services and staff.

## Run

From the repository root (Docker access required):

```sh
cp .env.example .env
docker compose up -d --build
curl --fail http://localhost:8000/health
docker compose exec -T api python -m app.seed
cd frontend
npm ci
npx playwright install --with-deps chromium
npm run e2e
```

Wait for both `/health` on the API and `/login` on the panel before running tests.
`E2E_BASE_URL` defaults to `http://localhost:3000`; `E2E_API_URL` defaults to
`http://localhost:8000`. The demo credentials are `owner@demo.kz`,
`barber@demo.kz`, `analyst@demo.kz`, password `demo12345`.

For a fresh frontend without rebuilding its Docker container, use a second port.
The optional Next.js proxy avoids the API's default CORS restriction to port 3000.
In PowerShell, from `frontend/`:

```powershell
$env:E2E_API_URL = 'http://localhost:8000'
$env:NEXT_PUBLIC_API_URL = '/e2e-api'
npm run build
npm run start -- -p 3100
```

In another PowerShell terminal:

```powershell
$env:E2E_API_URL = 'http://localhost:8000'
$env:E2E_BASE_URL = 'http://localhost:3100'
npm run e2e
```

`E2E_CHANNEL=chrome` optionally selects an installed Chromium-based Google Chrome
for local validation when downloading bundled Chromium is unavailable. CI uses
Playwright's bundled Chromium. Install only Chromium, not Firefox or WebKit.

Use `npm run e2e -- --project=mobile`, `--project=desktop`, or a spec path to
select cases. `npx playwright show-report` opens the HTML report. Failures retain
screenshots and traces in `test-results/`; axe results are attached to the report.
CI uploads both directories even when a test fails.

## Coverage and isolation

| Spec | Scenarios |
| --- | --- |
| `access.spec.ts` | Owner login/org/logout, invalid password, actual 401 redirect, persistent RU/KZ and theme, barber scope/direct URL restrictions, analyst read-only UI, axe on dashboard/calendar/inbox/clients |
| `booking.spec.ts` | UI creates new client and booking, confirmed color, drawer check-in/completion/payment, dashboard revenue delta; real API race creates a 409, UI displays alternatives and books one |
| `communication.spec.ts` | WhatsApp simulator/AI response, takeover/human response/release; 30% discount task/Ack/Resolve/relay; lead move editor requires a lost reason |

Each test authenticates independently, discovers IDs through the API, and creates
unique names and phones. Booking tests provision their own service, staff and
shifts instead of competing for demo slots. The suite runs with one worker to
keep the shared tenant's dashboard revenue checks deterministic. Do not run
another revenue-changing test suite against the same tenant simultaneously.

Cleanup cancels open bookings and test payments, removes generated shifts,
deactivates generated staff, soft-deletes services, resolves unfinished tasks,
and releases conversations to AI. The API has no contact/conversation/lead delete
endpoints; those unique records and completed booking/audit history remain.
No fixed demo IDs or synthetic successful API responses are used. The inbox test
delays a real `/v1/auth/me` response to exercise takeover/refetch ordering.

## Stage 4 intermittent failures (2026-11-14)

The inbox timeout had a deterministic modal synchronization cause. Takeover's
global query invalidation can update the human-mode badge/button before
`ConfirmButton` finishes awaiting refetches and closes its native dialog.
Playwright `fill()` can run on the inert textarea behind that dialog: it reports
success, but React does not accept the input, so the controlled value returns to
empty. Delaying the real `/v1/auth/me` response by 2.5 seconds reproduced this
against the unchanged Docker panel on port 3000. This evidence does **not** show
a background refresh clearing an accepted user draft or permissions briefly
becoming false: React Query retains cached session data during refetch.

To repeat the original diagnostic from `frontend/`:

```powershell
$env:E2E_CHANNEL = 'chrome'
node scripts/repro-inbox.mjs
```

The hardened E2E waits for the takeover POST response **and the confirmation
dialog to close** before typing, and asserts the textarea value before sending.
It also types an accepted draft before takeover and checks that it survives a
delayed real permission refresh. No server success is fabricated.

A separate real draft-loss path existed: `selectConversation()` always called
`setText("")`, including when reselecting the active row. Inbox drafts now live
in a map keyed by conversation ID, survive row reselection, switches, mode and
query updates, and clear only after a successful send. A late send clears only
the matching submitted snapshot, preserving newer edits. Failed sends retain
their drafts. The composer stays mounted when write permission changes (hidden
when unauthorized); cached detail errors and cached session refresh errors no
longer replace the mounted editor/page. Logout and actual session expiration
still clear the session as before. Drafts are in-memory for the mounted inbox;
reload persistence is not enabled.

The barber failure was a real staff-directory pagination bug, **not** a booking
outside the calendar range. The original mobile trace returned 50 of 55 staff;
the first page contained accumulated E2E staff and omitted Арман. `/staff`
filtered that incomplete first page to the logged-in staff ID and displayed an
empty schedule. Staff users now fetch their own profile by ID. The E2E obtains
that profile directly, creates unique own/other bookings through the API,
selects the created booking's date in its branch timezone, and verifies the
exact visible date response and own-booking visibility/other-booking exclusion.

New Testing Library regressions cover row reselection/switches, delayed session
refresh without editor remount, mode updates, failed sends, newer edits during
slow sends, successful clearing, cached session refresh failure through the real
Shell, and an own staff profile beyond the first 50 directory entries.
TypeScript excludes generated Playwright reports/results so extracted trace
source snapshots do not become application build inputs.

Stage 4 validation used the unchanged running API at `localhost:8000` and a
fresh production build served at `localhost:3100`, with installed Chrome. All
three full runs were sequential against the same database, with no retries,
database reset, container restart, or implementation changes between runs.

| Check | Result |
| --- | --- |
| Full E2E run 1 | **42 passed**, 1.0 minute |
| Full E2E run 2 | **42 passed**, 1.0 minute |
| Full E2E run 3 | **42 passed**, 1.0 minute |
| `npm run lint` | Passed |
| `npx tsc --noEmit` | Passed |
| `npx vitest run` | **39 passed**, 9 files |
| `npm run build` | Passed |
| `git diff --check -- frontend` | Passed |

Run logs are `frontend/docs/stage4-run1.log` through `stage4-run3.log` locally;
Playwright artifacts are retained in `frontend/test-results-stage4-run1/`
through `test-results-stage4-run3/`. To repeat each full run:

```powershell
$env:E2E_BASE_URL = 'http://localhost:3100'
$env:E2E_CHANNEL = 'chrome'
npx playwright test --output=test-results-stage4-run1
```

Only `frontend/` was edited for this task. No commit/push was made and no
containers were stopped. Port 3000 continues serving the existing Docker panel;
the fresh panel is left running on port 3100.

## Earlier validation (before stage 4)

Validated on 2026-11-14 against the running API at `localhost:8000` and a fresh
production frontend at `localhost:3100`, using installed Google Chrome through
`E2E_CHANNEL=chrome`:

| Check | Result |
| --- | --- |
| `npm run e2e` | **30 passed**, 42.2 seconds, desktop and mobile |
| axe smoke | All eight page/viewport scans have zero serious/critical violations |
| `npm run lint` | Passed |
| `npx tsc --noEmit` | Passed |
| `npx vitest run` | 4 files, **13 tests passed** |
| `npm run build` | Passed, final UI fixes included |
| `git diff --check` | Passed |

UI fixes include readable muted/status colors, a language button whose accessible
name contains its visible RU/KZ text, keyboard-scrollable report tables, mobile
workspace/logout controls, restored HTML language after reload, and restricting
the barber's calendar staff selector to their own profile.

Docker CLI access was denied by the local environment (`permission denied while
trying to connect to the docker API at npipe:////./pipe/docker_engine`). No Docker
containers were stopped or rebuilt. **Rebuild the frontend container to serve
these UI fixes on port 3000.** The complete Docker-based CI job was added but
could not be executed locally. The bundled Chromium download was slow; local
browser execution used installed Chrome. CI installs bundled Chromium normally.

No backend defects were found in the exercised scenarios, so there are no
failing backend request/response pairs to report. Backend source was not edited.
The no-delete cleanup limitations above reflect the available API contract.
