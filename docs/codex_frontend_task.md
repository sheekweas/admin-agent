# Task for Codex: AI CRM admin panel (frontend)

You are the FRONTEND engineer of the AI CRM project in this repository. Backend engineers (Claude agents)
are building the FastAPI backend in `backend/` right now in parallel — DO NOT touch anything outside
`frontend/`. If your environment supports sub-agents / parallel workers, use them (e.g. one per group of pages)
and coordinate them yourself.

Read first:
- `docs/SPEC_ru.txt` — the full technical specification (Russian). Most relevant: §11 (admin panel pages
  and UI requirements), §12, §14.4 (charts), §2 (roles), §8.2 (booking statuses), §10.2 (lead funnel).
- `docs/API.md` — the REST contract you must code against (exact paths and shapes).

## Stack
Next.js 15 (App Router) + TypeScript (strict) + Tailwind CSS + TanStack Query + react-hook-form + zod +
Recharts + date-fns/date-fns-tz + lucide-react icons. Use `npm`. Output standalone build. Dockerfile at
`frontend/Dockerfile` (multi-stage, node:22-alpine, `next start` on port 3000, env `NEXT_PUBLIC_API_URL`).

## Requirements
- Auth: login page → `POST /v1/auth/login`; store token (localStorage), `GET /v1/auth/me`; organization switcher
  when user has several tenants; logout; redirect to login on 401. Hide nav items / actions by `permissions[]`.
- i18n RU / KZ (Kazakh, code `kk`) for the whole UI with a language switcher (dictionary files, no external service).
- All dates/times rendered in the branch time zone (`branch.timezone`, default Asia/Almaty).
- Responsive: desktop sidebar layout + mobile bottom navigation; mobile matters a lot for admins.
- Explicit status indicators (spec §11.1): "AI отвечает", "человек отвечает", "ждём админа", "ошибка доставки",
  "бронь подтверждена" + booking status colors.
- Critical actions (cancel, reschedule, refund, publish, take over chat) require confirmation dialog and show the
  server result (no optimistic checkmarks). Show API error `detail.message` in toasts; for 409 SLOT_UNAVAILABLE show
  the `alternatives` list and let the user pick one.
- Global search (contacts by name/phone) in the header; quick filters on lists.
- Accessible: keyboard navigation, focus states, aria labels, readable contrast; light + dark theme.

## Pages (spec §11)
1. **Dashboard** `/` — KPI cards (new leads, bookings today, pending AI questions, cancellations, visits, paid revenue,
   conversion, no-show rate, avg check, AI resolution rate) + charts from `GET /v1/reports/overview`
   (revenue by day, bookings vs visits, top services, top staff, lead sources, funnel, load by hour,
   cancellations/no-show). Filters: date range, branch, staff, service, source.
2. **Calendar** `/calendar` — day and week views, columns per staff (day view), colored by status; filters branch/staff/
   service/status; click empty slot → create booking dialog (search/create contact, pick service → loads availability
   → pick slot); click booking → drawer with details, status actions (check-in, complete, no-show, cancel with reason,
   reschedule via availability picker), payment button, history. Drag-and-drop to move a booking → calls PATCH
   reschedule and shows conflicts from the server. Block a slot = create staff exception kind "block".
3. **Clients / CRM** `/clients` — table with search, tags, source, filters, pagination, CSV export (if permission);
   client card `/clients/[id]` with profile edit, consents & opt-out, tags, notes, timeline, bookings, payments, leads.
4. **Leads** `/leads` — kanban by stage (new → info_requested → qualified → booking_pending → booked → visited, lost/
   cancelled, repeat) with drag to change stage (lost requires reason) + funnel numbers.
5. **Inbox** `/inbox` — conversation list (filters: mode ai/human/waiting_admin, unread), chat view with messages
   (client/AI/human/system styled differently, internal notes highlighted, delivery status icons), reply box,
   "Take over" / "Return to AI" buttons, internal note toggle, quick link to contact card and create booking.
   Poll every 5s (TanStack Query refetchInterval). In dev show a "Simulate WhatsApp message" button
   (`POST /v1/conversations/simulate`) for demos.
6. **Admin tasks** `/tasks` — AI questions/escalations list with level badges (sent / repeated / calling), Ack and
   Resolve (answer, relay to client, add to knowledge as draft).
7. **Staff** `/staff` — profiles, services assignment, weekly shift editor with breaks, exceptions (vacation, sick,
   early leave…), shift requests list with conflicts and Approve/Reject. Staff role sees only own schedule and can
   submit a request.
8. **Services & prices** `/services` — catalog CRUD (RU/KZ titles, duration, price KZT, buffers, resource type,
   staff, published toggle).
9. **Finance** `/finance` — payments list, add payment (method, kind prepayment/payment), refund, cancel; unpaid
   completed bookings; totals.
10. **Analytics** `/analytics` — deeper reports (same endpoint, more charts/tables) + export CSV/XLSX.
11. **AI & knowledge** `/ai` — knowledge articles CRUD by section & language, draft/published/archived, publish
   (owner), version shown, "Test the AI" sandbox (`POST /v1/knowledge/test`) showing reply + tool calls; AI settings
   (enabled, tone, fallback messages) via `PATCH /v1/tenant` settings.ai.
12. **Integrations** `/integrations` — cards for WhatsApp, Telegram, Voice (Twilio), Zapier/webhooks with status,
   last error, config forms (secrets write-only), test button; outbound webhook subscriptions; event log with retry.
13. **Settings** `/settings` — organization (name, niche, language), branches CRUD, team & roles (escalation
   primary/backup, telegram chat id, phone), booking rules (slot step, hold TTL, cancel deadline, reminders,
   requires admin confirmation), escalation timers (5/10 min, calls enabled, quiet hours), audit log viewer.

## Quality
- Clean architecture: `src/lib/api.ts` (typed fetch client + error type), `src/lib/types.ts` (types mirroring
  docs/API.md), `src/hooks/*` (query hooks), `src/components/ui/*` (Button, Input, Select, Dialog, Drawer, Table,
  Badge, Card, Tabs, Toast, EmptyState, Skeleton), feature folders per page.
- Loading skeletons, empty states, error states everywhere.
- `npm run build` and `npm run lint` and `npx tsc --noEmit` must pass with zero errors. Write a few component tests
  with Vitest + Testing Library for critical logic (api client error handling, time-zone formatting, status badge).
- Polished, modern, consistent visual design (not a default template): neutral palette + one accent color, good
  spacing, clear typography.
- Write `frontend/README.md` (run, env, structure).

Do NOT run git commit/push. When finished, print a concise summary of what was built, and anything in the API
contract that was ambiguous.
