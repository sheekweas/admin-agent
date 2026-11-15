# Stage v1.1.0 — closing technical gaps (contract shared by backend and frontend)

Source of gaps: docs/ACCEPTANCE_REPORT.md §6 (#5, #9), docs/SECURITY.md §5, memory "known gaps".
Work split: Claude sub-agent A = auth sessions (backend), Claude sub-agent B = STT + voice/privacy fixes (backend),
Codex = frontend/. Nobody commits/pushes; the coordinator runs the verification gate and tags v1.1.0.

## A. Server-side sessions and JWT revocation (backend owner: agent A, migration `0003_auth_sessions`)

- New table `auth_sessions`: id (uuid), user_id, tenant_id (nullable), created_at, last_seen_at, expires_at,
  revoked_at (nullable), ip, user_agent (truncated 300). Only agent A writes Alembic migrations in this stage.
- Login creates a session; the JWT gets claim `sid`. Every authenticated request rejects (401) a token whose session
  is missing, revoked or expired. Tokens without `sid` (issued by v1.0) are rejected → users re-login once.
  `last_seen_at` updated at most once per 5 minutes per session (no write on every request).
- Revocation triggers: logout, logout-all, DELETE session, password change (all other sessions), user deactivated,
  membership deactivated (sessions of that tenant), admin "sign out user" (owner/admin of the tenant).
- Endpoints (all under `/v1/auth`, JSON):
  - `POST /logout` → 204 (revokes current session).
  - `POST /logout-all` → `{"revoked": N}` (all sessions of the user, including the current one).
  - `GET /sessions` → `{"items":[{id, created_at, last_seen_at, expires_at, ip, user_agent, current: bool}], "total": N}`
    (only active sessions of the current user).
  - `DELETE /sessions/{id}` → 204; 404 if not the user's session.
  - `POST /change-password {current_password, new_password}` → `{"revoked_other_sessions": N}`;
    400 `WEAK_PASSWORD` (min 10 chars, not only digits) | 400 `INVALID_CURRENT_PASSWORD`. Current session stays valid.
  - `POST /v1/team/{membership_id}/sign-out` (perm `team.manage`, same tenant) → `{"revoked": N}`.
- Error bodies keep the existing `{"detail": ...}` shape; codes above are in `detail`.
- Expired sessions purged by the existing retention/cleanup worker after 30 days. Audit events: `auth.logout`,
  `auth.logout_all`, `auth.session_revoked`, `auth.password_changed`, `auth.user_signed_out`.

## B. Speech-to-text and voice/privacy fixes (backend owner: agent B, NO migrations)

- `app.ai.stt.OpenAICompatibleSTT`: POST multipart to `{STT_BASE_URL}/audio/transcriptions` (OpenAI Whisper, Groq,
  self-hosted faster-whisper servers all speak it), `model=STT_MODEL` (default `whisper-1`), `language` = ru|kk hint,
  `response_format=verbose_json`; confidence = derived from segments' `avg_logprob` (exp, mean) or `null`.
  Env: `STT_PROVIDER=openai_compatible|mock|none`, `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`, `STT_TIMEOUT_SEC=30`,
  max audio 25 MB. Timeouts / 5xx → `STTError` → existing human fallback. Key never logged.
- Low confidence (< tenant `settings.voice.low_confidence_threshold`) → AI asks the client to repeat or type
  (RU/KK), message meta `stt_low_confidence: true`.
- Message `meta` for voice notes: `{"transcribed": bool, "stt": provider, "stt_confidence": float|null,
  "stt_language": "ru"|"kk"|null, "stt_error": str|null}` — exposed as-is in conversation message API.
- `GET /v1/integrations/stt` → `{provider, configured: bool, model|null}`; `POST /v1/integrations/stt/test`
  (perm `integrations.write`, owner) → `{ok: bool, provider, latency_ms, error|null}` (transcribes a bundled 1 s sample;
  mock returns ok).
- `/v1/calls` list/detail/report respect the caller's `branch_scope` (calls linked to a contact/booking of an
  out-of-scope branch are hidden; detail → 404). Add tests in the IDOR suite.
- Recording privacy: when a Twilio recording arrives for a call without recording consent (or recording disabled),
  delete it at Twilio via `VoiceProvider.delete_recording(sid)` (Twilio REST DELETE; mock records the call).
- Retention: the transcript-retention job also redacts `ai_runs` tool inputs/outputs older than the tenant
  transcript retention period (keep ids, timings, token counts).

## C. Frontend (owner: Codex, edit ONLY frontend/)

1. **Profile → Security** page (`/profile/security`, link in the user menu): active sessions table (device/UA,
   IP, created, last seen, "this device" badge), "End session" per row, "Sign out everywhere" (confirm dialog →
   then local logout), change-password form (current/new/confirm, client-side min 10 chars, server error codes →
   RU/KZ messages; success toast shows count of other sessions ended).
2. Logout button calls `POST /v1/auth/logout` before clearing local storage (ignore network errors).
3. Team page: "Sign out user" action per member (only with `team.manage`), confirm dialog, toast with count.
4. 401 handling stays (`crm:unauthorized`) but show a one-time toast "Сессия завершена — войдите снова / Сессия
   аяқталды — қайта кіріңіз" on the login page when redirected because of 401.
5. Inbox: voice-note messages show a mic badge, transcript text, confidence hint (low → amber "низкая точность"),
   and "не распознано" state when `transcribed=false`/`stt_error`.
6. Integrations: "Распознавание речи / Сөйлеуді тану" card: provider, model, configured status, Test button.
7. RU/KZ i18n for all strings, loading/empty/error states, mobile layout, axe-clean; unit tests (vitest) and
   Playwright specs for the new pages (mock the API where the backend is not up yet).
