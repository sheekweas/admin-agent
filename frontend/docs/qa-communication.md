# Communication and reports live QA

Checked 2026-11-14 against http://localhost:8000, authenticated as owner in Barber KZ. Full request/response evidence for 23 operations is in `qa-communication-evidence.json`; no access tokens are stored.

## Live checks

- GET conversations, conversation detail and messages: list responses are `{items,total}`; conversation has nullable `last_message` and `open_task_id`, messages have nullable `media_url` and UTC timestamps.
- POST simulated WhatsApp (`phone`, `name`, `text`) -> 200, new conversation `d3d7888d-60e8-43da-bcbf-d6f192a7161e`.
- POST read, takeover, release -> 200. POST reply `{text:"QA owner reply",internal_note:false}` and internal note -> 201.
- POST task fixture -> 201, GET detail -> 200, POST ack -> 200 (`status:"acked"`), GET status=acked -> 200, POST resolve `{answer:"QA resolved answer",relay_to_client:true,add_to_knowledge:true}` -> 200 and knowledge draft ID returned in payload.
- POST article -> 201, GET detail/PATCH/publish/archive -> 200. POST knowledge test with `question`, `language` -> 200, returning `reply`, `language`, `escalated`, `tool_calls`, `used_articles`. Retested with `include_drafts:true` -> 200.
- GET report overview with date filters -> 200, all arrays/types checked. Money remains decimal strings; percentages are rendered without multiplying by 100. CSV and XLSX export -> 200, correct media types (2346 and 9954 bytes at first check).
- Staff permissions omit conversations/tasks/knowledge/reports. Analyst permits reports, staff, services; report auxiliary queries now check respective permissions.

## Frontend fixes

- Corrected nullable communication types and complete task shape.
- Internal notes no longer imply takeover or trigger reply/takeover confirmation, and no longer show external delivery ticks. Live API confirms a note in AI mode leaves `mode:"ai"`.
- Conversation deep links fetch their detail independently of the current filtered/limited conversation list.
- WhatsApp simulator is available to users with `conversations.write` in the production demo UI; backend mock-mode restrictions still apply and errors surface normally.
- AI sandbox includes drafts, matching its pre-publication testing description.
- Added input maximum lengths matching API constraints (messages 4096, article content 20000, test question 2000, task answer 4000).
- Shared communication DataState shows a friendly localized access-denied message for 403.
- Reports use actual source filter values `site` and `call`; restored broken question-mark labels for chart grouping. Overview booking metric now identifies its today-only date range.
- ESLint passed for both feature directories. Root agent runs final full suite and visual checks.

## Backend bugs / gaps

1. `GET /v1/reports/overview?date_from=2026-10-15&date_to=2026-11-14`, owner Bearer + X-Tenant-Id: HTTP 200, `kpi.conversion_to_booking:286.1`, `new_leads:136`, `bookings:79`. Expected conversion percentage within 0-100 per task contract. UI preserves server result; backend must fix denominator/scope.
2. OpenAPI response schemas for conversations and knowledge are generic dictionaries, and overview/export response schema is `{}`. Expected documented field names, nesting, nullability, decimal-string money and 0-100 percentage bounds (exports should document binary media types). Live response comparison was necessary.

QA fixtures remain in the demo: test conversation, resolved task, generated draft knowledge entry, and archived QA article. Existing seeded records were not modified.
