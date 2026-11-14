# Frontend integration QA — 2026-11-14

All source changes and QA artifacts are inside `frontend/`. No backend files were edited and no commit/push was made. This report lives at `frontend/docs/QA_FRONTEND.md` to respect the frontend-only edit constraint.

## Environment and evidence

- Live API: `http://localhost:8000`; demo tenant Barber KZ. Owner, barber/staff and analyst logins were exercised using the supplied demo accounts.
- Downloaded live schema: [openapi.qa.json](../openapi.qa.json). [Generated schema types](../src/lib/api-schema.ts) replace manually divergent shared types; regenerate with `node scripts/generate-api-types.cjs`.
- [GET evidence](qa-get-evidence.json): 38 owner GET requests, including every page's lists, record details, schedules, histories, timelines, availability and exports. All returned 200. Structured response models were recursively compared for required fields, types, nesting and nullability with no mismatches. Generic/empty OpenAPI responses required manual JSON comparison and remain a backend documentation gap.
- Exact mutation requests/responses: [booking](qa-booking-evidence.json), [operations](qa-operations-evidence.json), [communication](qa-communication-evidence.json). Tokens are omitted and the temporary account password is redacted.
- Detailed feature notes: [booking](qa-booking.md), [operations](qa-operations.md), [communication](qa-communication.md).

## Checked and fixed

Compared frontend paths, HTTP methods, query parameters, body fields and authentication headers against OpenAPI and actual responses. Booking creation sends `Idempotency-Key`; replay returned the original booking. Lists use `{items,total}` except the actual bare team array, weekly shift array and integrations `{items}` response. Money remains decimal strings; percentages are displayed directly, without multiplying by 100.

- Corrected numeric booking numbers, nullable contact phone, nullable service resource type/staff user ID, compact booking contact/staff references and complete response fields. Login permits the schema's nullable tenant ID and selects an available tenant.
- Contact writes now send `phone`; client leads use the server's `contact_id` filter. Booking status actions follow valid transitions. Rescheduling closes the obsolete booking drawer because the API creates a replacement booking. Conflict choices show branch-local dates and exclude alternatives outside the selected branch or staff scope.
- Corrected audit `actor_id`/`payload`; refund eligibility, remaining refund amount, refund labels and `paid_at`; forms submit declared editable fields instead of response metadata. Numeric and text limits now match request schemas, and team editing no longer sends immutable email.
- Fixed internal-note handling: notes do not claim takeover or display external delivery ticks. Conversation deep links load detail independently. Knowledge sandbox includes drafts. Authorized demo users can simulate WhatsApp in the production UI.
- Permission checks use the live `permissions[]`. Staff can create an own booking with inline contact and update own visit status without querying forbidden contacts. Staff cannot cancel/reschedule or see client links. Analyst sees permitted read pages and exports, without mutation actions. Denied routes and 403 query boundaries show a localized no-access state.
- Repaired broken role, history and chart grouping labels and invalid dictionary entries. Fixed inbox grid minimum width and clients table accessibility text escaping its scroll container at mobile width; corrected search-input padding.
- Branch timestamps use `Asia/Almaty`; tests verify UTC-to-Almaty date boundaries and local calendar input conversion (`10:00` Almaty -> `05:00Z`).

## Live mutations

| Flow | Observed result |
| --- | --- |
| Availability slot -> booking; same-key replay; occupied slot -> alternatives -> retry | 201; same booking on replay; 409 `SLOT_UNAVAILABLE`; retry 201 |
| Reschedule, cancel, checked-in/completed/no-show | 200; replacement ID on reschedule; invalid reconfirmation correctly returns 409 |
| Lead stage update and restore; contact create/patch/note | 200; 201/200/201 |
| Payment, two partial refunds exhausting remaining amount, separate payment cancellation | 201; 201/201; 200; no net QA revenue |
| Simulate WhatsApp, read, takeover, reply, internal note, release | 200/201, correct mode changes and message nesting |
| Admin task create, ack, resolve with relay and knowledge draft | 201/200/200 |
| Knowledge create, edit, publish, test including drafts, archive | 201/200; test response includes reply, language, escalation, tool calls and used articles |
| Shift request create, reject, create and approve | 201/200; approved exception verified and removed after QA |
| Team member create/deactivate; service/staff CRUD and weekly shifts | Accepted; temporary member/staff inactive, service removed |
| Tenant patch including current settings | 200; existing configuration preserved |
| Integration PUT and test for WhatsApp, Telegram, Voice, Zapier | HTTP 200; unconfigured WhatsApp/Zapier tests correctly return `ok:false` |

QA contacts, terminal bookings, conversation, resolved task, knowledge draft/archived article, shift history and inactive member/staff remain as audit evidence. Cancellable QA bookings were cancelled; payments were fully refunded/cancelled. Real external WhatsApp delivery was not established: the supplied backend uses mock providers and missing production credentials are reported by integration tests.

## Browser validation

Playwright with installed Chrome exercises real UI login and every panel route (including client detail) at owner widths 1440 and 390, plus direct navigation to every route as staff and analyst. Screenshots are in `frontend/qa-screenshots/` (gitignored); machine-readable results are in [qa-browser-evidence.json](qa-browser-evidence.json).

Final sweep: 54 page checks, zero console/page errors, zero document-width overflows and zero broken question-mark labels. Owner screenshots were visually inspected, including the corrected mobile inbox and clients layouts.

The existing container on port 3000 could not be rebuilt/restarted: the environment denied Docker engine/config access. The updated production build was served locally on port 3001. Playwright forwarded only frontend page/assets from origin `http://localhost:3000` to that build, preserving the allowed browser origin. API requests went directly to the real port 8000 with normal authentication and CORS. Chrome's local-network permission was granted for localhost. No API responses were mocked. Opening port 3001 directly is rejected by the backend's configured CORS policy.

The existing frontend container therefore still needs rebuilding by an environment with Docker access to serve these source changes normally on port 3000.

## Backend bugs / contract gaps (not changed)

1. **Conversion above 100%.** Owner `GET /v1/reports/overview?date_from=2026-10-15&date_to=2026-11-14`, Bearer token + `X-Tenant-Id`, no body: HTTP 200 with `kpi.conversion_to_booking:286.1`, `new_leads:136`, `bookings:79`. An earlier unfiltered request returned `280.6`. Expected conversion percentage in 0–100 with a defined numerator/denominator and consistent date scope. Frontend preserves the returned value rather than concealing the backend defect.
2. **Conflict alternatives ignore branch and caller scope.** Owner `POST /v1/bookings` with fresh `Idempotency-Key`, body `{"branch_id":"fac8eb60-4497-4c8f-bb79-5d5e3e0917b3","service_id":"7f6b2ba5-c061-4554-b601-0e53ace5ad83","contact_id":"7baaa16a-9459-40f5-a319-515f71d1c2b7","staff_id":"e6342361-f645-47e1-b619-af3a5998e742","slot_id":"e6342361-f645-47e1-b619-af3a5998e742|2026-11-17T05:00:00+00:00|7f6b2ba5-c061-4554-b601-0e53ace5ad83","comment":"Frontend QA"}` after occupying the slot: HTTP 409 `SLOT_UNAVAILABLE`; alternatives include Astana branch `8632be51-5912-466b-95da-1ff9d2a1d4f9`. Staff responses also offer other staff the caller cannot book (403 on use). Expected alternatives constrained to requested branch and caller permissions. Frontend filters defensively; exact full responses are in booking evidence.
3. **Underspecified OpenAPI responses.** GET/POST payments, integration status/events/tests, audit, reports, and conversation/knowledge routes frequently declare `{}` or generic dictionaries instead of actual response models. Expected explicit nested shapes/nullability, decimal-string money, percentage bounds and export media types. Evidence files retain actual JSON. OpenAPI also does not express the booking transition graph or the difference between full write and own-status permissions.

## Final checks

`npm run lint`, `npx tsc --noEmit`, `npx vitest run` (13 tests), and `npm run build` passed. Production browser sweep results and screenshots reflect the final code.
