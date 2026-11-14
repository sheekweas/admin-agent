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
No fixed demo IDs, browser request stubs or synthetic successful responses are used.

## Local results

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
