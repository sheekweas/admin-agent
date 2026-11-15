# REST API (v1) — agreed contract between backend modules and the admin panel

Base: `http://localhost:8000`. Auth: `Authorization: Bearer <jwt>` (from `POST /v1/auth/login`).
Optional `X-Tenant-Id` header to switch organization. Errors: `{"detail": {"code": "...", "message": "..."}}`
or FastAPI validation `{"detail": [...]}`. All datetimes are ISO-8601 with offset (UTC from server);
UI renders them in the branch time zone (`branch.timezone`). Money is a decimal string. IDs are UUID strings.
List endpoints return `{"items": [...], "total": N}` and accept `limit` (default 50, max 200) & `offset`.

## Auth / tenant (done)
- `POST /v1/auth/login {email,password,tenant_id?}` → `{access_token, tenant_id, role, tenants:[{tenant_id,name,role}]}`
- `GET /v1/auth/me` → `{user_id,email,full_name,ui_language,tenant_id,tenant_name,role,staff_id,branch_scope,permissions[]}`
- Sessions (v1.1.0). Every token is bound to a server-side session (JWT claim `sid`); a token whose session was revoked or
  expired, or a v1.0 token without `sid`, gets **401** on every endpoint → the UI logs the user out.
  - `POST /v1/auth/logout` → 204 (ends the current session)
  - `POST /v1/auth/logout-all` → `{"revoked": N}` (all sessions of the user, including the current one)
  - `GET /v1/auth/sessions` → `{"items":[{id,created_at,last_seen_at,expires_at,ip,user_agent,current}],"total":N}` — active
    sessions of the current user, most recently seen first; `current=true` marks the caller's session. `last_seen_at` is
    refreshed at most every 5 minutes.
  - `DELETE /v1/auth/sessions/{id}` → 204; 404 `NOT_FOUND` if it is not an active session of the current user
  - `POST /v1/auth/change-password {current_password,new_password}` → `{"revoked_other_sessions": N}`; the current session stays
    valid. Errors (in `detail.code`): 400 `INVALID_CURRENT_PASSWORD`, 400 `WEAK_PASSWORD` (min 10 chars, not only digits);
    429 after 10 wrong current passwords in 5 min.
- `GET/PATCH /v1/tenant` → `{id,name,niche,plan,default_language,currency,settings}`
- `GET/POST /v1/team`, `PATCH /v1/team/{membership_id}` (members: email, full_name, phone_e164, telegram_chat_id, role, is_escalation_primary/backup)
- `POST /v1/team/{membership_id}/sign-out` (perm `team.manage`: owner, admin) → `{"revoked": N}` — ends all sessions of that member
  (the caller's own current session is kept); 404 for another organization's membership; 403 `OWNER_PROTECTED` when an admin
  targets an owner. `PATCH /v1/team/{id} {"active": false}` also ends the member's sessions in this organization.

## Catalogue & staff (booking agent)
- `GET/POST /v1/branches`, `PATCH /v1/branches/{id}` — `{id,name,address,timezone,contacts,active}`
- `GET/POST /v1/services`, `PATCH/DELETE /v1/services/{id}` — `{id,category,title_ru,title_kk,description,duration_min,price,buffer_before_min,buffer_after_min,resource_type,published,active,staff_ids[]}`; GET filters: `branch_id, published, q`
- `GET/POST /v1/staff`, `PATCH /v1/staff/{id}` — `{id,branch_id,user_id,name,specialization,color,active,service_ids[]}`
- `GET/PUT /v1/staff/{id}/shifts` — weekly shifts `[{weekday,start_time:"10:00",end_time:"21:00",breaks:[{start,end}]}]`
- `GET/POST /v1/staff/{id}/exceptions`, `DELETE /v1/staff/{id}/exceptions/{exc_id}` — `{date,kind,start_time,end_time,reason}`
- `GET /v1/staff/shift-requests?status=` ; `POST /v1/staff/shift-requests {staff_id?,kind,date,new_start,new_end,reason}` → includes `conflicts:[booking]`
- `POST /v1/staff/shift-requests/{id}/decision {approve:bool, comment}`
- `GET /v1/staff/{id}/schedule?date_from&date_to` — resolved working intervals per day

## Booking (booking agent)
- `GET /v1/availability?service_id&date_from&date_to&staff_id?&branch_id?` → `{items:[{slot_id,staff_id,staff_name,branch_id,start_at,end_at}]}`
- `GET /v1/bookings?date_from&date_to&staff_id&branch_id&status&contact_id` → items: `{id,number,branch_id,contact:{id,name,phone_e164},staff:{id,name,color},services:[{service_id,title_ru,title_kk,price,duration}],start_at,end_at,status,channel,source,total_price,paid_amount,comment,created_at}`
- `POST /v1/bookings` (header `Idempotency-Key`) `{contact_id | contact:{name,phone}, service_id, staff_id?, start_at | slot_id, comment?, branch_id?}` → 201 booking | 409 `{code:"SLOT_UNAVAILABLE", alternatives:[slot]}`
- `GET /v1/bookings/{id}` ; `PATCH /v1/bookings/{id}` `{action:"reschedule",start_at,staff_id?}` | `{action:"cancel",reason}` | `{action:"status",status:"checked_in|completed|no_show|confirmed"}` | `{comment}`
- `GET /v1/bookings/{id}/history` — audit entries

## CRM (booking agent)
- `GET /v1/contacts?q&tag&source&branch_id&opted_out` ; `POST /v1/contacts` ; `GET/PATCH /v1/contacts/{id}`
- `GET /v1/contacts/{id}/timeline` — merged messages/bookings/visits/payments/tasks/notes sorted desc
- `POST /v1/contacts/{id}/notes {text}` ; `POST /v1/contacts/{id}/merge {other_id}` ; `GET /v1/contacts/export.csv`
- `GET /v1/leads?stage&source&date_from&date_to` ; `PATCH /v1/leads/{id} {stage,lost_reason,tags,owner_user_id}` ; `GET /v1/leads/funnel?date_from&date_to`

## Inbox & AI (AI agent)
- `GET /v1/conversations?mode&status&q` → items: `{id,contact:{id,name,phone_e164},channel,mode,status,language,last_message:{text,author,at},unread_count,open_task_id}`
- `GET /v1/conversations/{id}/messages` → `[{id,direction,author,message_type,text,media_url,provider_status,is_internal_note,received_at,author_user_id,error,language,meta}]`
  - `meta` (v1.1) is the message's channel metadata as stored. WhatsApp voice notes (`message_type:"audio"`) carry
    `{"transcribed": bool, "stt": "openai_compatible"|"mock"|"none", "stt_confidence": 0–1|null, "stt_language": "ru"|"kk"|null,
    "stt_error": str|null}`; `"stt_low_confidence": true` when the transcript was empty or below
    `settings.voice.low_confidence_threshold` — the AI then asked the client (RU/KK) to repeat or type instead of answering;
    the third unintelligible note in a row, or any STT error (`stt_error` set, `transcribed:false`), hands the dialogue
    to a human (fallback message + admin task, mode `waiting_admin`).
- `POST /v1/conversations/{id}/messages {text, internal_note?:bool}` — human reply (switches to human mode automatically)
- `POST /v1/conversations/{id}/takeover` ; `POST /v1/conversations/{id}/release` (return to AI) ; `POST /v1/conversations/{id}/read`
- `GET/POST /v1/knowledge`, `PATCH /v1/knowledge/{id}`, `POST /v1/knowledge/{id}/publish`, `POST /v1/knowledge/{id}/archive`
- `POST /v1/knowledge/test {question, language}` → `{reply, tool_calls, used_articles}` (sandbox, no side effects)
- `POST /v1/conversations/simulate {phone,name,text}` — dev/demo: inject an inbound WhatsApp message (mock mode only)
- Webhooks: `GET/POST /v1/webhooks/whatsapp`

## Escalation, finance, reports, integrations (ops agent)
- `GET /v1/admin-tasks?status` → `{id,number,kind,question,priority,status,assignee,contact,conversation_id,escalation_level,created_at,ack_at,resolved_at,answer}`
- `POST /v1/admin-tasks` ; `POST /v1/admin-tasks/{id}/ack` ; `POST /v1/admin-tasks/{id}/resolve {answer, relay_to_client, add_to_knowledge}`
- `GET /v1/payments?date_from&date_to&booking_id` ; `POST /v1/payments {booking_id?,visit_id?,amount,method,kind,transaction_ref?,comment}` ; `POST /v1/payments/{id}/refund {amount,reason}` ; `POST /v1/payments/{id}/cancel`
- `GET /v1/reports/overview?date_from&date_to&branch_id&staff_id&service_id&source` → `{kpi:{revenue,refunds,net_revenue,bookings,visits,no_shows,cancellations,new_leads,conversion_to_booking,conversion_to_visit,no_show_rate,avg_check,ai_resolution_rate,open_tasks,unpaid_completed}, revenue_by_day:[{date,amount}], bookings_by_day:[{date,bookings,visits}], top_services:[{service_id,title,revenue,count}], top_staff:[{staff_id,name,revenue,visits,utilization}], lead_sources:[{source,count}], funnel:[{stage,count}], load_by_hour:[{hour,bookings}], cancellations_by_day:[{date,cancelled,no_show}]}`
- `GET /v1/reports/export.csv?...` ; `GET /v1/reports/export.xlsx?...`
- `GET /v1/integrations` ; `PUT /v1/integrations/{kind}` (kind: whatsapp|telegram|voice|zapier) `{config, secrets, enabled}` ; `POST /v1/integrations/{kind}/test`
- Speech-to-text (v1.1, platform-level env config `STT_PROVIDER`/`STT_BASE_URL`/`STT_API_KEY`/`STT_MODEL`/`STT_TIMEOUT_SEC`):
  `GET /v1/integrations/stt` (`integrations.read`) → `{provider:"openai_compatible"|"mock"|"none", configured:bool, model|null}`
  (the key is never returned); `POST /v1/integrations/stt/test` (`integrations.write` = owner; the contract's
  "integrations.manage") → `{ok, provider, latency_ms, error|null}` — transcribes a bundled 1 s silent WAV with the
  active provider (mock → ok; audited as `integration.test` entity `stt`).
- `GET/POST /v1/integrations/webhooks`, `DELETE /v1/integrations/webhooks/{id}` ; `GET /v1/integrations/events?status` ; `POST /v1/integrations/events/{id}/retry`
- `GET /v1/audit?entity&entity_id&action&date_from&date_to`
- Webhooks: `POST /v1/webhooks/telegram/{tenant_id}` ; `POST /v1/webhooks/voice/{provider}/status`, `/gather`

## Voice (stage 4, ТЗ §13)
AI phone calls reuse the WhatsApp agent: same prompt (+ phone rules), tools and booking engine; a booking exists only
after `create_booking` committed. Reading needs `conversations.read` (owner, admin); outbound / simulate need
`conversations.write`; `/v1/reports/voice` needs `reports.read` (owner, admin, analyst). Numbers are masked for the
analyst role. Datetimes ISO with offset; rates 0–100.

- `GET /v1/calls?direction=in|out&purpose&status&contact_id&date_from&date_to&limit&offset` (`purpose`/`status` accept
  comma-separated lists; dates are organisation-local, inclusive) → `{items, total}`, newest first. Item:
  `{id, direction, purpose, status, from_number, to_number, contact:{id,name,phone_e164}|null, admin_task_id,
  booking_id|null, started_at, ended_at, duration_sec, transferred_to_human, recording_available, created_at}`
  - purpose: `client_ai` (inbound AI) | `admin_escalation` (IVR to an admin) | `booking_confirmation` | `reminder` |
    `callback` | `reschedule`
  - status: `queued | ringing | in_progress | answered | completed | no_answer | busy | failed | cancelled`
    (`answered` is used by admin IVR calls; client calls go `in_progress` → `completed`). `started_at` = answered.
- `GET /v1/calls/{id}` → item + `transcript:[{speaker:"client"|"ai"|"admin"|"system", text, at, confidence|null}]`,
  `metrics:{first_response_ms|null, turns, recognition_retries, transferred_to_human, booking_created}`.
  Transcript is `[]` after `settings.voice.transcript_retention_days`. Other organisations' ids → 404.
- Branch scope (v1.1): for a member with a `branch_scope`, list / detail / `GET /v1/reports/voice` hide calls linked to a
  contact, booking or admin task of a branch outside the scope (detail → 404, same body as an unknown id). Calls
  without such links, or linked to branch-less records, stay visible — the rule used for contacts and conversations.
- `POST /v1/calls/outbound {contact_id, purpose:"booking_confirmation"|"reminder"|"callback"|"reschedule",
  booking_id?, admin_task_id?}` → **201** call item (status `ringing`/`queued`; a provider error is returned as
  status `failed`). `booking_id` defaults to the client's next upcoming booking (required except for `callback`);
  `admin_task_id` optionally links a "callback" task (task button). Errors:
  - 409 `CONSENT_REQUIRED` — `contact.consent_flags.outbound_calls` is not true or the client opted out
  - 409 `OUTSIDE_HOURS` — outside `settings.voice.outbound_hours` (local, default 10–20) or in escalation quiet hours
  - 409 `DAILY_CAP_REACHED` — `settings.voice.max_outbound_per_day` outbound client calls already placed today
  - 422 `BOOKING_REQUIRED` | `BOOKING_CONTACT_MISMATCH` | `NO_PHONE`; 404 foreign contact/booking; 503 `VOICE_NOT_CONFIGURED`
- `POST /v1/calls/simulate {phone, name?, utterances:[string], language?:"ru"|"kk"}` (voice mock mode only, else 404)
  → call detail. Runs a whole inbound call through the real turn logic; `""` simulates silence; a transfer is
  simulated as "admin did not answer" (→ callback task).
- `GET /v1/reports/voice?date_from&date_to` → `{calls, answered, avg_duration_sec, avg_first_response_ms,
  transfer_rate, booking_rate, recognition_retry_rate}` over client calls (admin IVR calls excluded); rates are
  percentages of answered calls, `recognition_retry_rate` = failed recognitions / all client utterances.
- Settings (`GET/PATCH /v1/tenant`, key `settings.voice`): `ai_calls_enabled`, `recording_enabled`,
  `transcript_retention_days`, `max_outbound_per_day`, `outbound_hours:[from,to]`, `low_confidence_threshold` (0–1),
  `max_recognition_failures`, `max_turns`, `tts_voice_ru`, `tts_voice_kk`, `tts_language_kk`, `stt_language_ru`,
  `stt_language_kk`, `transfer_timeout_sec`. PATCH replaces the stored `voice` object; omitted keys fall back to the defaults.
- Integration `PUT /v1/integrations/voice {config:{phone_number}, secrets:{account_sid, auth_token, from_number}}`:
  `phone_number` routes inbound calls to the organisation (falls back to `from_number`).
- Provider webhooks (Twilio, `X-Twilio-Signature` required, fail closed): `POST /v1/webhooks/voice/twilio/inbound`
  (number's "A call comes in"), `/status` (number's status callback; `call_id` optional — matched by `CallSid`),
  `/turn?call_id&seq`, `/answer?call_id`, `/dial?call_id`, `/recording?call_id`, `/gather?call_id` (admin IVR).
- Recording privacy (v1.1): a `/recording` callback for a call without recording consent — the call was not set up
  for recording, `recording_enabled` was turned off, or the client refused (`consent_flags.call_recording=false`;
  outbound calls need it `true`) — deletes the recording at the provider (`DELETE …/Recordings/{RecordingSid}.json`)
  and never stores its URL; audit `call.recording_deleted`. If the deletion fails, `call.meta.recording_delete_failed`
  keeps the SID for manual removal.
- Retention (worker `ops.voice_retention`, hourly): besides call transcripts/recordings, `ai_runs` older than the
  tenant's `transcript_retention_days` lose tool inputs/results and `reply_text` (ids, timings, token counts, model,
  tool names and ok/error flags are kept; each tool call gets `redacted:true`).
