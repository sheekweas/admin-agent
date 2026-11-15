# Security

Scope: the backend (FastAPI + PostgreSQL + Celery/Redis) and its integrations. Maps to ТЗ §20 (security, personal data),
§17.1 (tenant-scoped keys/queries), §7.3/§20.3 (AI protection), §22.1 (security tests).

## 1. Threat model (summary)

| Asset | Threat | Main controls |
|---|---|---|
| Client PII (names, phones, chats), bookings, payments | Another tenant / a lower role reads or changes them (IDOR, tenant switching) | `tenant_id` only from the verified JWT + active membership; every lookup is tenant-scoped (`get_or_404`); foreign ids behave exactly like missing ones (404); RBAC matrix `app.deps.PERMISSIONS`; branch scopes; staff see only their own calendar; analysts get masked phones |
| Inbound channels (WhatsApp, Telegram, Twilio) | Forged or replayed webhooks inject messages, ack tasks, fake call results | HMAC signatures (Meta `X-Hub-Signature-256`, per-tenant Telegram secret token, `X-Twilio-Signature`), fail-closed when a secret is missing, idempotency on `provider_message_id`, 1 MB body limit, per-IP rate limit |
| AI agent | Prompt injection makes the model act for another client/tenant, invent discounts, leak data | Tools are the boundary: identity comes from the conversation, never from the model; strict schemas (`extra="forbid"`); ownership checks; booking claims without a successful tool call are blocked; client-controlled text is marked untrusted and escaped in the prompt; every call (incl. rejections) is audited in `ai_runs` |
| Integration secrets, JWTs, passwords | Leak through API responses, audit, logs, provider errors | Secrets write-only (masked previews), encrypted at rest, webhook secret shown once; log redaction for all loggers; `httpx` request logging off; provider errors reduced to class/status |
| Accounts | Credential stuffing, token forgery | PBKDF2-SHA256 (240k), login rate limits (per IP+path and per IP+e-mail failures), HS256 JWT with mandatory `exp`/`iat`/`sub`, production refuses default secrets |
| Outbound webhooks (Zapier) | SSRF, tampering, replay at the receiver | https-only and no private/loopback hosts outside dev, no redirects, HMAC over `timestamp.body`, receivers check a 5-minute window and `X-CRM-Delivery`; PII stripped unless `include_pii` |

Out of scope here (infrastructure): TLS termination, WAF, backups encryption, DB network isolation — see the checklist.

## 2. Controls implemented (→ ТЗ §20.1)

**RBAC, tenant isolation, branch scopes**
- `app/deps.py`: token → user → active membership of the requested tenant (`X-Tenant-Id` or token `tid`). A non-member gets 403
  and an `access.denied` audit row in the target tenant (AT-11).
- Every router uses `require("<permission>")`; the full matrix is enforced by `tests/test_security_isolation.py::test_role_matrix_enforces_permissions`
  (every route × every role, discovered from `app.main.app`).
- IDOR sweep (`test_idor_sweep_foreign_ids_are_404`): every route with an id path parameter is called with another tenant's real ids
  (GET/POST/PATCH/PUT/DELETE, valid bodies) → 403/404, no foreign data in the body, the other tenant's rows byte-identical afterwards.
  Ids inside request bodies (contact, service, staff, branch, booking, visit, conversation, user, branch scopes) are covered too.
- Shared user accounts: an existing account is linked to an organization only with that account's password, and a user that also
  belongs to another organization cannot have their profile (phone, Telegram chat id, name) changed by this organization's owner.

**Sessions & token revocation** (v1.1.0, `app/services/auth_sessions.py`, table `auth_sessions`, migration `0003_auth_sessions`)
- `POST /v1/auth/login` creates a session row (user, tenant, ip, user agent ≤ 300 chars, `expires_at` = token expiry) and the JWT
  carries its id as claim `sid`. `app/deps.py:get_auth` rejects with 401 any token whose session is missing, revoked, expired or
  belongs to another user (primary-key lookup per request). Tokens issued by v1.0 (no `sid`) are rejected → users log in once.
  `last_seen_at` is written at most once per 5 minutes per session.
- Revocation (`revoked_at`): `POST /v1/auth/logout` (current), `POST /v1/auth/logout-all` (all of the user's), `DELETE
  /v1/auth/sessions/{id}` (own sessions only; anyone else's id → 404), `POST /v1/auth/change-password` (all *other* sessions;
  min 10 chars, not only digits; 10 wrong current passwords / 5 min → 429), `POST /v1/team/{membership_id}/sign-out` (perm
  `team.manage` = owner/admin, same tenant; an admin cannot sign out an owner; the caller's current session is kept).
  Deactivating a user (`users.active=false`) revokes all their sessions; deactivating a membership revokes the user's sessions
  started in that organization — done by an ORM `after_flush` hook in `app/models.py`, so every code path (API, CLI, scripts) is covered.
- Audit: `auth.login` (with `session_id`), `auth.logout`, `auth.logout_all`, `auth.session_revoked`, `auth.password_changed`,
  `auth.user_signed_out`. The worker task `ops.purge_auth_sessions` (hourly) deletes sessions expired or revoked > 30 days ago.
- Tests: `tests/test_auth_sessions.py`; the IDOR sweep covers `DELETE /v1/auth/sessions/{session_id}` and the sign-out route.

**Webhook signatures & replay** (`app/api/webhooks_*.py`)
- WhatsApp: platform app secret, else per-tenant `app_secret`; with per-tenant secrets *every* addressed `phone_number_id` must verify
  with its own tenant's secret; an empty secret never verifies. Duplicate deliveries are no-ops (unique `provider_message_id`).
- Telegram: `X-Telegram-Bot-Api-Secret-Token` = HMAC(platform secret, tenant id) — compared in constant time; actions require an active
  owner/admin of that tenant (by `telegram_chat_id`) with branch access; other chats are refused and audited.
- Twilio: `X-Twilio-Signature` is mandatory (no auth token ⇒ 403), `CallSid` must match; the `mock` provider is disabled outside mock mode.
- Voice AI calls (stage 4): the same check on `inbound`/`turn`/`answer`/`dial`/`recording`/`status`; `inbound` resolves the tenant
  from the called number first and verifies with *that* tenant's token (a number configured for two tenants is refused). Turns
  are idempotent per `seq` (a replayed webhook gets the stored TwiML, the AI is not run twice). TwiML contains only static
  phrases, the AI reply and what the caller said — never CRM data (admin IVR speaks no client data at all, §26.4).
- Calls/recordings (§20.1): outbound AI calls need `consent_flags.outbound_calls` (and no opt-out), run only in
  `settings.voice.outbound_hours` outside quiet hours, capped per tenant per day; recording is announced, skipped for clients who
  refused (`call_recording=false`; outbound needs `true`), and a recording URL is stored only for such consented calls;
  transcripts, voice-turn messages and recording URLs are erased after `transcript_retention_days` (`ops.voice_retention`).
  Saying «не звоните» during a call sets the opt-out (and withdraws `outbound_calls`).
- Outbound: `X-CRM-Signature: sha256=hex(HMAC-SHA256(secret, "{X-CRM-Timestamp}." + raw_body))`. Reference receiver check:
  `app.security.verify_crm_webhook(secret, raw_body, signature, timestamp, tolerance_s=300)`:

  ```python
  ts = request.headers["X-CRM-Timestamp"]
  if abs(time.time() - int(ts)) > 300: reject()
  expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
  if not hmac.compare_digest(expected, request.headers["X-CRM-Signature"]): reject()
  # then de-duplicate by X-CRM-Delivery
  ```

**HTTP hardening** (`app/middleware.py`, configurable in `app/config.py`)
- Headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy`, `Permissions-Policy`, CSP `default-src 'none'; frame-ancestors 'none'` (not on `/docs`, `/redoc`),
  `Cache-Control: no-store` on `/v1/*`, HSTS when `ENV=production`.
- Body limits: `MAX_WEBHOOK_BODY_BYTES` (1 MB) for `/v1/webhooks/*`, `MAX_REQUEST_BODY_BYTES` (10 MB) elsewhere → 413 (also for
  chunked uploads).
- Rate limits per client IP + path (60 s window): `/v1/auth/login` (`RATE_LIMIT_LOGIN_PER_MINUTE`, 20) and `/v1/webhooks/*`
  (`RATE_LIMIT_WEBHOOK_PER_MINUTE`, 600) → 429 + `Retry-After`. Backend `RATE_LIMIT_BACKEND=auto` uses Redis when reachable (shared
  by all API processes), otherwise in-process memory. Behind a proxy run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>`.
- Start-up validation (`app.config.validate_settings`, API and Celery): with `ENV=production`/`prod` the process refuses to start if
  `JWT_SECRET` is the default/a placeholder/shorter than 32 chars, `TELEGRAM_WEBHOOK_SECRET` is a placeholder, or (WhatsApp live)
  the Meta app secret / verify token are placeholders.

**Secrets & logs**
- Integration secrets: encrypted at rest (authenticated keystream, `app/security.py`), never returned (masked `••••abcd`), audit stores
  only key names. Webhook subscription secret: returned once at creation.
- Logging (`app/logging_setup.py`): a LogRecord factory redacts Telegram bot tokens, JWTs, `Bearer/Basic` credentials and
  `password|secret|token|api_key|signature=…` pairs in every message and traceback, for all loggers (uvicorn, celery, httpx).
  `httpx`/`httpcore` INFO request logs (which contain bot-token URLs) are disabled. Unhandled errors return a generic 500 with a
  request id.
- Tests: `tests/test_security_secrets.py` (login, team, integration PUT/test, webhooks; responses + audit + captured logs).

**AI protection (§20.3)** — `app/ai/tools.py`, `app/ai/prompts.py`, `tests/test_prompt_injection.py`
- Tool inputs never contain tenant/contact/conversation ids (strict JSON schemas + `extra="forbid"`); bookings are looked up only
  among the conversation contact's bookings; services/staff/branches are tenant-scoped; tags from an allow-list; names sanitized.
- Mutations run in a SAVEPOINT; the sandbox (`/knowledge/test`) stubs every mutating tool and rolls back.
- The reply guard blocks "вы записаны"-style claims without a successful booking tool call (`ai_runs.error = unverified_booking_claim`).
- `ai_runs.tool_calls` records every call with `ok/error`; model-supplied input is bounded (strings ≤ 500 chars, ≤ 20 keys).
- Client-controlled values in the system context (profile name, dialogue state) are JSON with `<`/`>` escaped, labelled untrusted.

**Audit** — `audit_logs` for admin actions (bookings, finance, settings, team, integrations, exports, access denials); owner-only API.

## 3. Rotating secrets

| Secret | How |
|---|---|
| `JWT_SECRET` | Set a new value and restart API + workers. All sessions are invalidated (users log in again). If `SECRETS_ENCRYPTION_KEY` is **not** set, integration secrets are encrypted with a key derived from `JWT_SECRET`: first move to a dedicated key (next row), otherwise rotating the JWT secret makes stored integration secrets unreadable. |
| `SECRETS_ENCRYPTION_KEY` (integration secrets at rest) | 1) `SECRETS_ENCRYPTION_KEY=<new>` and `SECRETS_ENCRYPTION_OLD_KEYS=<previous key>` (the JWT secret is tried automatically when moving off the derived key); restart. 2) `python -m app.rotate_secrets` (idempotent, `--dry-run` available). 3) Remove the old key from `SECRETS_ENCRYPTION_OLD_KEYS`; restart. |
| `TELEGRAM_WEBHOOK_SECRET` | Change it, restart, then re-register every tenant's webhook: «Интеграции → Telegram → Проверить» (calls `setWebhook` with the new per-tenant secret). Updates are refused until then. |
| `WHATSAPP_APP_SECRET` / per-tenant `app_secret` | Reset the app secret in Meta, update `.env` (or the tenant's integration) immediately — signatures with the old secret are refused. |
| Per-tenant tokens (WhatsApp access token, Telegram bot token, Twilio auth token) | Paste the new value in «Интеграции» (write-only), press «Проверить»; revoke the old one at the provider. |
| Outbound webhook secret | Create a new subscription (secret shown once), switch the receiver, delete the old subscription. |
| Database / Redis passwords | Standard rotation via `DATABASE_URL` / `REDIS_URL`; restart API, workers, beat. |

## 4. Production checklist

- [ ] `ENV=production`; `JWT_SECRET` ≥ 32 random chars; `SECRETS_ENCRYPTION_KEY` set (≥ 32 chars) and stored separately from DB backups.
- [ ] `TELEGRAM_WEBHOOK_SECRET`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` random, non-default (start-up check enforces it).
- [ ] TLS everywhere (`PUBLIC_BASE_URL=https://…`, HSTS is sent); uvicorn `--proxy-headers --forwarded-allow-ips=<proxy ip>`.
- [ ] `CORS_ORIGINS` = the admin panel origin only.
- [ ] Rate limits on (`RATE_LIMIT_ENABLED=true`), Redis reachable so limits are shared; tune per-minute limits to traffic.
- [ ] Reverse proxy body limit ≥ app limits; `/docs` and `/openapi.json` restricted if not public.
- [ ] Twilio: auth token configured (callbacks are refused otherwise); WhatsApp: per-tenant numbers unique (enforced).
- [ ] PostgreSQL not exposed publicly; separate DB user without superuser; encrypted, tested backups (§20.1).
- [ ] Log shipping keeps redaction (JSON logs from the app); no request-body logging at the proxy for `/v1/auth/login` and webhooks.
- [ ] Mock adapters off for live tenants (`LLM_PROVIDER`, `WHATSAPP_MODE`, `TELEGRAM_MODE`, `VOICE_PROVIDER`); `/v1/conversations/simulate` is disabled outside WhatsApp mock mode.
- [ ] Monitor `access.denied` audit rows, 401/403/429 rates on webhooks, `ai_runs` with `unverified_booking_claim` / repeated tool rejections.
- [ ] `/metrics` is not reachable from the internet (Caddy answers 404); set `METRICS_TOKEN` (≥ 24 chars) if Prometheus scrapes from outside the docker network.
- [ ] Deployment via `deploy/docker-compose.prod.yml` (docs/OPERATIONS.md): DB/Redis without published ports, Redis password, encrypted off-site backups with a tested restore.
- [ ] Run the security suites before each release: `pytest tests/test_security_*.py tests/test_prompt_injection.py tests/test_openapi.py`.

## 5. Known limitations / follow-ups

- ~~JWTs are stateless, a stolen token stays valid until expiry~~ — fixed in v1.1.0: server-side sessions (see §2 "Sessions &
  token revocation"); logout, logout-all, ending a session, a password change, deactivation and an admin sign-out revoke tokens
  immediately. Remaining: there is no refresh token (the 12 h `JWT_TTL_MINUTES` still bounds a session); a session is a login of
  the *user*, not of one organization (the UI switches organizations with `X-Tenant-Id` on the same token), so an admin
  "sign out user" ends all of that user's sessions, including ones used in their other organizations.
- ~~`GET /v1/team` shows colleagues' phones to all roles~~ — fixed in v0.3.0: only owner/admin see colleagues' phones/Telegram ids.
- Login rate limiting is per IP; distributed credential stuffing needs an upstream WAF / captcha.
- ~~Voice: a non-consented Twilio recording is not deleted at Twilio; `ai_runs` keep tool inputs beyond the transcript
  retention period~~ — fixed in v1.1.0: such recordings are deleted via the Twilio REST API (`call.recording_deleted` audit;
  a failed deletion leaves `call.meta.recording_delete_failed` for manual removal), and `ops.voice_retention` redacts
  `ai_runs` tool inputs/results and reply text after `transcript_retention_days`. Remaining: recordings of *consented*
  calls are only unlinked (URL erased) after the retention period, not deleted at Twilio — set a Twilio retention policy.
- `/v1/calls` respects the member's `branch_scope` since v1.1.0 (calls linked to a contact/booking/task of another branch).
- Telegram webhook answers 401 (not 403) to a wrong secret token — kept for backward compatibility with existing clients/tests.
