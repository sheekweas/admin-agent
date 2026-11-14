# Booking and CRM live QA ? 2026-11-14

Compared calendar, booking forms/drawer, contacts/client detail, leads and funnel request paths, query parameters, request bodies and response types with the live OpenAPI schema. Full request/response evidence: `qa-booking-evidence.json` (no auth tokens).

## Live checks

- Owner GET branches/services/staff, filtered bookings, booking detail/history, contacts/detail/timeline, contact-filtered leads and funnel: 200. Lists are `{items,total}`; money is a decimal string. Booking number is an integer. Booking contact/staff are compact references, nullable phone is allowed.
- Owner contact create 201, profile PATCH 200, note create 201.
- Owner POST booking with a real availability `slot_id`, staff/service/branch/contact and `Idempotency-Key`: 201. Repeating the key replays the same booking, without creating another booking. A fresh key for that slot produces 409 `detail.code=SLOT_UNAVAILABLE` and six alternatives. Staff exercised the actual alternative retry, which returned 201.
- Owner reschedule via PATCH `{action:"reschedule",start_at,staff_id}`: 200 and a NEW booking ID. Old booking becomes rescheduled. Cancellation of the replacement: 200.
- Own confirmed booking ? checked_in ? completed: 200; separate booking ? no_show: 200. Reconfirming an already confirmed booking returns 409 INVALID_TRANSITION (expected); UI now offers only allowed transitions.
- Lead PATCH stage qualified and restore original stage: 200.
- Staff `barber@demo.kz`: bookings GET contains only own staff ID; contacts GET returns expected 403. Staff can create with inline contact and own staff slot, and can mark own visit status. Staff cancellation/reschedule requires full `bookings.write` and returns expected 403. UI reflects these distinctions. Owner cancelled staff QA bookings during cleanup.
- Analyst permissions have no writes; booking mutations remain hidden.

## Frontend fixes

- Contacts create/update sends `phone`, as required, rather than silently ignored `phone_e164`.
- Booking feature types reuse generated schema aliases (integer booking number; actual compact references and nullable fields). Timeline includes actual `title`, `ref_id`, `at`, nullable amount and history payload.
- Client leads request filters `contact_id` server-side, so earlier leads outside an arbitrary global 200-row window are available.
- Booking status actions follow the real transition graph; terminal bookings have no invalid cancel/reschedule/status actions.
- Reschedule closes the old drawer because the endpoint returns a replacement booking. Service cannot be changed during reschedule since the endpoint does not accept that change.
- Staff own-booking create/status controls are available without granting full booking write rights. Staff creation uses inline contact rather than a forbidden contact search; client links are hidden without contacts.read. Staff drag/reschedule/cancel is hidden.
- Conflict alternatives are limited to the chosen branch and own staff for staff users, show date as well as time and clear the stale occupied selection before retry.
- History displays localized action labels and branch-local timestamps; contact timeline uses actual server-provided titles.

## Backend contract gap (not changed)

POST `/v1/bookings`, with `branch_id="fac8eb60-4497-4c8f-bb79-5d5e3e0917b3"` (Almaty), `service_id="7f6b2ba5-c061-4554-b601-0e53ace5ad83"`, `contact_id="7baaa16a-9459-40f5-a319-515f71d1c2b7"`, `staff_id="e6342361-f645-47e1-b619-af3a5998e742"`, `slot_id="e6342361-f645-47e1-b619-af3a5998e742|2026-11-17T05:00:00+00:00|7f6b2ba5-c061-4554-b601-0e53ace5ad83"`, `comment="Frontend QA"` and a fresh Idempotency-Key after that slot is occupied:

Actual: 409 SLOT_UNAVAILABLE alternatives include Baurzhan in Astana, `branch_id="8632be51-5912-466b-95da-1ff9d2a1d4f9"`. Staff conflict response also offers other staff that the caller cannot book (would receive 403). Expected: alternatives respect the submitted branch and caller's own-staff restriction. Frontend filters these alternatives defensively.

OpenAPI describes BookingPatch.status as the four accepted strings but does not expose per-current-status transitions or distinguish full write versus own-visit write requirements. Runtime checks establish the supported actions above.

## Verification

`npx tsc --noEmit` and `npx eslint src/features/booking --max-warnings=0` passed after changes. Parent task runs full lint/test/build and browser sweeps.

QA created clearly named contacts and bookings. Rescheduled/cancellable QA bookings were cancelled; completed/no-show QA records remain as mutation evidence. No backend files were edited.
