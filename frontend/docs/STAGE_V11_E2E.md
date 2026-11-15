# Stage v1.1 frontend checks

Run from `frontend/`:

```powershell
npm.cmd run lint
npx.cmd tsc --noEmit
npm.cmd test
npm.cmd run build
npx.cmd playwright test --config playwright.v11.config.ts
```

The v1.1 Playwright configuration starts only Next.js on port 3100, with an isolated `.next-v11` build directory. Every API response is mocked using Playwright routes, so no backend or Docker service is required. Chromium must already be installed (`npx.cmd playwright install chromium` when needed).

The desktop and mobile projects cover sessions and password changes, confirmation before logout everywhere, logout after a network failure, a one-time notice after an HTTP 401, team sign-out permissions, voice transcripts and recognition failures, STT configuration/testing permissions, and Kazakh labels. Axe checks cover the security page, its logout confirmation, team settings, inbox voice messages, and STT integration page. Reports and traces are written to `test-results-v11/`.
