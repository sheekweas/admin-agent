# REST API (v1) — agreed contract between backend modules and the admin panel

Base: `http://localhost:8000`. Auth: `Authorization: Bearer <jwt>` (from `POST /v1/auth/login`).
Optional `X-Tenant-Id` header to switch organization. Errors: `{"detail": {"code": "...", "message": "..."}}`
or FastAPI validation `{"detail": [...]}`. All datetimes are ISO-8601 with offset (UTC from server);
UI renders them in the branch time zone (`branch.timezone`). Money is a decimal string. IDs are UUID strings.
List endpoints return `{"items": [...], "total": N}` and accept `limit` (default 50, max 200) & `offset`.

## Auth / tenant (done)
- `POST /v1/auth/login {email,password,tenant_id?}` → `{access_token, tenant_id, role, tenants:[{tenant_id,name,role}]}`
- `GET /v1/auth/me` → `{user_id,email,full_name,ui_language,tenant_id,tenant_name,role,staff_id,branch_scope,permissions[]}`
- `GET/PATCH /v1/tenant` → `{id,name,niche,plan,default_language,currency,settings}`
- `GET/POST /v1/team`, `PATCH /v1/team/{membership_id}` (members: email, full_name, phone_e164, telegram_chat_id, role, is_escalation_primary/backup)

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
- `GET /v1/conversations/{id}/messages` → `[{id,direction,author,message_type,text,media_url,provider_status,is_internal_note,received_at}]`
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
- `GET/POST /v1/integrations/webhooks`, `DELETE /v1/integrations/webhooks/{id}` ; `GET /v1/integrations/events?status` ; `POST /v1/integrations/events/{id}/retry`
- `GET /v1/audit?entity&entity_id&action&date_from&date_to`
- Webhooks: `POST /v1/webhooks/telegram/{tenant_id}` ; `POST /v1/webhooks/voice/{provider}/status`, `/gather`
