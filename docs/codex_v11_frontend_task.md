# Task for Codex: stage v1.1.0 frontend (sessions/security, STT UI)

Read `docs/STAGE_v1.1_CONTRACT.md` section C (your scope) and sections A/B (API contract you consume).
Two Claude agents are implementing the backend endpoints right now — they may not exist yet. Build against the
contract, mock API responses in vitest and Playwright (route mocking), keep existing tests green.

Rules:
- Edit ONLY files under `frontend/`. Do not git commit/push. Do not start/stop docker.
- Follow existing structure: `src/lib/api.ts`, `src/lib/types.ts`, `src/features/*`, RU/KZ dictionaries.
- Use your sub-agents (multi_agent_v2) to parallelize: e.g. (1) profile/security page + logout + 401 toast,
  (2) team sign-out + inbox voice-note UI + integrations STT card, (3) tests/i18n/a11y review.
- Done when: `npm run lint`, `npx tsc --noEmit`, `npm test` (vitest) and `npm run build` all pass, and new
  Playwright specs exist for the security page, team sign-out, voice-note rendering and STT card (route-mocked,
  they must pass without the backend: `npx playwright test <new specs>` if the config allows running without a
  server; otherwise report how they are meant to run).
- Finish with a short report: files changed, commands run with results, anything not done.
