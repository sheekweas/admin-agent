# Stage v1.1 frontend

The frontend consumes the API contract in `../../docs/STAGE_v1.1_CONTRACT.md`.

- `/profile/security`: active sessions, revocation, confirmed sign-out everywhere, password change and localized validation errors.
- User menu: profile security link and best-effort server logout before local credentials are removed.
- A protected request returning 401 redirects to login and shows one localized session-ended toast. Login failures and voluntary logout do not create that notice.
- Settings team section: confirmed member sign-out requires `team.manage`; the result reports the revoked session count.
- Inbox: microphone badge, transcript, confidence using the tenant threshold, and a localized unrecognized state. Call turns retain their existing presentation.
- Integrations: STT provider, model, configuration status and test result. Testing requires `integrations.write`.

## Verification

Run from `frontend/`:

```sh
npm run lint
npx tsc --noEmit
npm test
npm run build
npx playwright test --config=playwright.v11.config.ts
```

On PowerShell installations that block `.ps1` shims, use `npm.cmd` and `npx.cmd`.

The v1.1 Playwright configuration starts only the Next.js frontend on port 3100, using `.next-v11` as its build directory. All API responses are intercepted by routes in `e2e/v11-support.ts` and the specs. No backend or Docker is required. Chromium must be installed for Playwright. The suite covers security, logout, 401 handling, team permissions/sign-out, voice-note states, STT testing and Kazakh translations on desktop and mobile, including axe and horizontal-overflow checks.

## Results (2026-11-15)

- `npm.cmd run lint`: passed.
- `npx.cmd tsc --noEmit`: passed.
- `npm.cmd test`: 63 tests passed in 16 files.
- `npm.cmd run build`: passed, including `/profile/security`.
- `$env:E2E_CHANNEL = 'chrome'; npx.cmd playwright test --config playwright.v11.config.ts`: 20 passed on desktop/mobile. The strengthened session-revocation test also passed separately on both projects (2 tests).
- Axe found zero violations; the scanned pages had no horizontal overflow.
- `git diff --check -- frontend`: passed.

Implementation files: `src/features/profile/security.tsx`, `src/app/(panel)/profile/security/page.tsx`, `src/lib/{api.ts,session.tsx,types.ts}`, `src/app/login/page.tsx`, `src/components/shell.tsx`, `src/features/communication/{inbox.tsx,voice-note.tsx}`, `src/features/operations/{settings.tsx,team-sign-out.tsx,integrations.tsx,stt-card.tsx}`, and both RU/KK dictionaries. Corresponding Vitest tests, `e2e/v11-*.ts`, `playwright.v11.config.ts`, frontend build/lint exclusions and E2E documentation were added or updated.

All required frontend work is complete. Live backend integration remains for the coordinator's verification gate; these new browser tests intentionally use the shared contract and mocked API responses. No Docker operations or commit/push were performed.
