# Live operations QA

Checked on 2026-11-14 against `http://localhost:8000`, using owner@demo.kz and the supplied demo password. All edits were limited to frontend files. Exact requests and JSON responses are retained in [qa-operations-evidence.json](qa-operations-evidence.json); authentication tokens were omitted and the temporary created account password was redacted.

## Contract and GET checks

Compared all operations feature requests and local response types with OpenAPI and real JSON for:

- `GET /v1/services`, `/v1/staff`, `/v1/branches`, `/v1/tenant`, `/v1/team`, `/v1/audit?entity=&action=`.
- `GET /v1/payments?limit=200`, `/v1/bookings?status=completed&limit=200`, `/v1/reports/overview`.
- `GET /v1/staff/{id}/shifts`, `/v1/staff/{id}/exceptions`, `/v1/staff/shift-requests`.
- `GET /v1/integrations`, `/v1/integrations/webhooks`, `/v1/integrations/events`.

All returned HTTP 200. Money is represented as decimal strings. Booking numbers are numbers. Services can have `resource_type: null`; staff can have `user_id: null`; shift request start/end and exception start/end may be null. Team returns a bare array; integrations returns `{items}` without `total`; other paginated lists include `{items,total}`. Weekly shifts return a bare array with `HH:mm` branch-local times.

## Mutations exercised

| Request | Result |
| --- | --- |
| `POST /v1/payments` with booking_id, kind payment, cash, amount `"1.00"` | 201; money `"1.00"`, status confirmed |
| `GET /v1/payments/{created_id}` | 200; matches returned payment |
| `POST /v1/payments/{id}/refund` with amount `"0.40"`, reason | 201; refund row with refund_of_id and kind refund |
| Same endpoint with remaining amount `"0.60"` | 201; remaining refund accepted |
| `POST /v1/payments` with prepayment `"1.00"`, then `POST /v1/payments/{id}/cancel` without body | 201 then 200 cancelled |
| `POST /v1/staff/shift-requests` vacation date 2026-12-20, null new_start/new_end, reason | 201 pending |
| `POST /v1/staff/shift-requests/{id}/decision` with approve false and comment | 200 rejected |
| Create early_leave for 2026-12-21, new_end `20:00`, then decision approve true | 201 then 200 approved; exception visible by GET |
| Delete the QA-created exception | 204; original staff schedule restored |
| `POST /v1/team` email, full_name, password, analyst role, escalation flags false | 201; membership_id returned |
| `PATCH /v1/team/{membership_id}` active false | 200; QA account deactivated |
| `PATCH /v1/tenant` current name/niche/default_language; second patch with current settings | Both 200; values preserved |
| `PUT /v1/integrations/{whatsapp,telegram,voice,zapier}` with config {}, secrets {}, enabled false | All 200 |
| `POST /v1/integrations/{kind}/test` | All HTTP 200. WhatsApp ok false, missing access_token/phone_number_id; Telegram ok true, mock bot; Voice ok true, mock provider; Zapier ok false, 0/0 subscriptions |
| `POST /v1/services` draft with string price, duration/buffers, active true, staff_ids [] | 201; nullable resource_type returned |
| `PATCH /v1/services/{id}` published true, then DELETE | 200 then 204; QA service removed |
| `POST /v1/staff` inactive QA staff with branch, color and service_ids [] | 201; user_id null |
| `PUT /v1/staff/{id}/shifts` weekly array with break start/end | 200; exact weekly array returned |
| `PATCH /v1/staff/{id}` active false | 200; QA staff remains inactive (no staff deletion endpoint) |

The two payments were fully refunded/cancelled, leaving no net QA revenue. Shift request history and inactive QA member/staff records remain as audit evidence. Integration configuration remained disabled and empty; tests exercised the configured mock providers.

## Frontend fixes

- Audit UI now reads `actor_id` and `payload`, matching actual responses, instead of nonexistent `actor_user_id` and `changes`.
- Refund controls exclude refund rows, cancelled payments, and fully refunded payments. Refund amount defaults/max use the remaining unrefunded amount. Payment cancellation is hidden after a partial refund. Refund rows have a refund label and payment dates use `paid_at`.
- Corrected local nullable types, numeric booking numbers, bare team array, and integrations items-only response shape.
- Forms submit only declared editable fields, preventing server IDs/metadata and hidden nested tenant settings from leaking into mutation payloads. Team email is only entered during creation because MemberPatch does not support changing email. Creation password enforces minimum eight characters.
- Number fields use integer steps by default; service/payment amounts use cents. Service duration and buffers follow schema maxima.
- Operations data boundaries show a translated friendly no-access state for 403 responses, without a retry button. Integration retry controls recognize dead events as well as failed events.
- Staff header and webhook header actions wrap at narrow widths.

## Role checks

Live barber permissions: bookings.read, bookings.write_own, services.read, shift_requests.create, staff.read. Live analyst permissions: bookings.read, contacts.read, finance.read, reports.export, reports.read, services.read, staff.read.

Both roles could GET services, staff, tenant and shift requests. Barber payments returned 403 `{detail:{code:"FORBIDDEN",message:"Missing permission: finance.read"}}`. Both roles' integrations GET returned 403 `{detail:"Missing permission: integrations.read"}`. Analyst payments returned 200. No operations mutation buttons are enabled without their corresponding write permission. Staff view narrows displayed people and shift requests to its own staff_id.

## Backend bugs / contract gaps

1. `GET /v1/reports/overview` without query/body, owner auth: HTTP 200, `kpi.conversion_to_booking: 280.6` (date_from `2026-10-16`, date_to `2026-11-14`; bookings 76, new_leads 134). Expected percentage range is 0–100. The precise calculation should be clarified/fixed by backend; the frontend must not silently change backend metrics. This was observed before the QA mutations.
2. OpenAPI response schemas are empty `{}` for several operations routes (payments, integration status/events/tests and audit). Actual field shapes and nullability cannot be generated reliably from the schema; the evidence file documents current JSON. Expected explicit response models, including items-only integration lists, payment money strings/refunded_amount nullability, audit actor_id/payload and integration test `{ok,detail}`. No backend files changed.

## Validation

`npx tsc --noEmit` passed after operations changes. `npx eslint src/features/operations --max-warnings=0` passed. `npx vitest run src/features/operations/shared.test.tsx` passed (2 tests): friendly forbidden boundary and editable payload filtering. Full application checks and browser screenshots are consolidated in the final frontend QA report.
