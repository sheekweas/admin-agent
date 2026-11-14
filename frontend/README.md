# AI CRM admin frontend

Next.js 15 App Router, strict TypeScript, Tailwind, TanStack Query, React Hook Form/Zod, Recharts, date-fns/date-fns-tz and Lucide. All frontend work is contained in this directory.

## Run

```sh
npm ci
cp .env.example .env.local
npm run dev
```

Open `http://localhost:3000`. Set `NEXT_PUBLIC_API_URL` to the browser-accessible FastAPI origin, without a trailing slash (default `http://localhost:8000`). The backend must allow the frontend origin in CORS. Log in with an existing backend account; there is no frontend mock authentication or seeded production data.

```sh
npm run lint
npx tsc --noEmit
npm test
npm run build
npm start
```

The build produces `.next/standalone`. The requested `npm start` command runs `next start -p 3000`; Next prints an advisory about standalone output but serves the complete `.next` build. To deploy only the minimal standalone bundle instead, copy `public` and `.next/static` into the standalone tree and run `node .next/standalone/server.js`.

## Docker

```sh
docker build --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 -t ai-crm-admin .
docker run --rm -p 3000:3000 ai-crm-admin
```

Three stages use `node:22-alpine`; the runtime uses an unprivileged user and `npm start` on port 3000. The image includes the complete build and runtime dependencies to support the requested `next start` entry point as well as standalone output. `NEXT_PUBLIC_API_URL` is a **build-time** browser variable; passing a different value only to `docker run` does not replace compiled browser code.

## Structure

- `src/app/(panel)`: authenticated routes, including dashboard, calendar, clients/detail, leads, inbox, tasks, staff, services, finance, analytics, AI, integrations, settings.
- `src/app/login`: validated login form.
- `src/components/shell.tsx`: desktop sidebar, mobile navigation, global client search, organization/branch/language/theme controls.
- `src/components/ui`: native accessible form controls, modal dialogs/drawers, cards, badges/status indicators, tabs, tables, skeletons, empty/error states and toast delivery.
- `src/lib/api.ts`: typed fetch, bearer/tenant headers, normalized FastAPI errors, downloads, 401 signaling and `ApiError` with slot alternatives.
- `src/lib/types.ts`: shared contract types; feature-specific types live beside the relevant pages.
- `src/lib/session.tsx`: profile, permissions, tenant switching and branch timezone context.
- `src/lib/i18n.tsx`, `src/lib/locales.ts`, `src/lib/dictionaries/{ru,kk}.json`: local RU/KK dictionaries (565 phrases), with contextual bilingual fallback for server-provided names; no translation service. Regenerate dictionaries from feature copy using `npm run i18n:extract`.
- `src/lib/time.ts`: branch-zone formatting and local-wall-time to UTC conversion, KZT formatting.
- `src/hooks/use-api.ts`: query and mutation helpers. Server results invalidate queries; no optimistic business-state updates.
- `src/features/{booking,communication,operations,reports}`: feature implementation.
- `src/**/*.test.{ts,tsx}`: Vitest/Testing Library coverage for API headers/errors/conflicts/401/204, branch timezone conversion and translated status indicators.

## Behavior

JWT, selected tenant and available tenants are stored in localStorage as requested. Logout and 401 clear credentials; switching tenants clears cached business data. Permission checks use the backend's exact `permissions[]` strings. Route checks and hidden controls complement backend authorization, which remains authoritative.

RU and KK, theme and branch selection are client controls. Dates use the selected branch's IANA timezone; before a branch is selected, the first available branch's zone is used, with `Asia/Almaty` as fallback. Calendar selects a concrete branch when one is available. Cross-branch reports use that selected display zone; date-range bucketing and financial report calculation remain the server's responsibility.

Booking creation sends an Idempotency-Key. Availability drives slot choice, including server-supplied 409 SLOT_UNAVAILABLE alternatives. Moving/rescheduling, cancellation, refund, publication, chat takeover and other critical actions use review/confirmation dialogs. Success is shown after a server response; `detail.message`, validation and network failures appear as errors. Inbox conversations/messages and administrator tasks poll every five seconds. WhatsApp simulation is exposed only in development and still requires backend mock mode.

Dashboard and analytics use real `/v1/reports/overview` data for all ten KPI cards and eight chart/table groups. Analytics adds export XLSX and operational detail metrics. No illustrative values are substituted for missing data.

## Contract assumptions and integration follow-up

`docs/API.md` defines routes but does not fully specify every payload. The following assumptions are isolated in feature types/forms:

1. Contacts use `phone_e164`, `language`, `tags`, `source`, `opted_out`, and `consent_flags` (`processing`, `marketing`, `outbound_calls`, `call_recording`) following backend models. Notes/timeline entries support `kind`/`type`, `created_at`/`at`, text and optional amounts. A concrete serialized timeline schema is still needed.
2. Lead objects include contact, stage, source, tags and loss reason. Funnel responses accept arrays, `{items}` or `{funnel}`. Contact-specific lead panels filter the available lead list because the contract does not document a contact filter. Large lists currently request up to 200 records; CRM clients use server pagination. Calendar, leads, staff, services, payments and inbox should receive cursor/pagination UX if more than 200 rows must be browsed; calendar explicitly warns when truncated.
3. Weekly shifts use weekdays 0–6 (Monday first); confirm this convention with the booking backend. Resolved schedules and shift conflicts are displayed without automatic booking changes.
4. Payment, integration, subscription, event and audit response shapes are not fully documented. Forms use the named request fields and common `id/status/created_at` response fields. Subscription event names and provider config/secret keys need confirmation with each integration implementation. Secrets are blank on edit and sent only when supplied.
5. Knowledge fields (`section`, `language`, `title`, `content`, `status`, `version`) follow backend models. Unread inbox filtering is local because an unread query parameter is not specified. Task in-progress status is `acked`.
6. `POST /v1/team` requires a password of at least eight characters in the implemented backend, although the REST document omits it; the create-member form includes this field.
7. Tenant booking rules are top-level keys inside `settings`, while AI and escalation are nested. These names were checked against `backend/app/services/tenant_settings.py`, including fallback_message_ru/kk and escalation quiet_hours `[22,8]`. PATCH submits the preserved settings alongside edited fields.
8. KPI percentage values are assumed to be percentage points (0–100), rather than fractions. The contract provides no prior-period comparison, so the dashboard does not invent growth percentages. The dashboard uses a separate overview request for bookings today in the branch timezone.
9. The report contract has no payment-status filter or conversion time series. The UI offers the documented filters and funnel totals; those extra filters/series require backend contract extensions. Day-level revenue can be viewed by day, Monday-based week, or month through client-side aggregation.

Build/lint/type checks and the 11 focused unit/component tests pass. Backend feature modules were being implemented in parallel; authenticated end-to-end API workflows and browser visual QA require a running completed backend and browser surface. The available computer-use service did not expose a browser/native surface in this environment. Docker execution was not verified here.
