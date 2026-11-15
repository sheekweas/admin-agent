import { defineConfig } from '@playwright/test';

// Every API call in these specs is intercepted; only a local frontend is needed.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'v11-*.spec.ts',
  workers: 1,
  outputDir: 'test-results-v11',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: { baseURL: 'http://localhost:3100', browserName: 'chromium', channel: process.env.E2E_CHANNEL, trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: 'node node_modules/next/dist/bin/next dev -p 3100',
    url: 'http://localhost:3100/login',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_PUBLIC_API_URL: 'http://localhost:8000' },
  },
});
