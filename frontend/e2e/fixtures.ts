import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test';
import type { LoginResult } from '../src/lib/types';

export const apiURL = process.env.E2E_API_URL || 'http://localhost:8000';
export const unique = (prefix = 'e2e') => `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
type TestSession = Omit<LoginResult, 'tenant_id'> & { tenant_id: string; headers: { Authorization: string; 'X-Tenant-Id': string } };
// Reuse API setup sessions per worker; UI login remains covered independently.
// This keeps the full desktop/mobile suite below the real login rate limit.
const sessions = new Map<string, { value: TestSession; until: number }>();
export async function createSession(request: APIRequestContext, role = 'owner') {
  const cached = sessions.get(role);
  if (cached && cached.until > Date.now()) return cached.value;
  const response = await request.post(`${apiURL}/v1/auth/login`, {
    data: { email: `${role}@demo.kz`, password: 'demo12345' },
  });
  expect(response.ok(), `login: ${response.status()} ${await response.text()}`).toBeTruthy();
  const session = await response.json();
  const value: TestSession = { ...session, headers: { Authorization: `Bearer ${session.access_token}`, 'X-Tenant-Id': session.tenant_id } };
  sessions.set(role, { value, until: Date.now() + 5 * 60_000 });
  return value;
}
export async function login(page: Page, request: APIRequestContext, role = 'owner') {
  const session = await createSession(request, role);
  await page.addInitScript(data => {
    localStorage.setItem('crm:token', data.access_token);
    localStorage.setItem('crm:tenant', data.tenant_id);
    localStorage.setItem('crm:tenants', JSON.stringify(data.tenants));
    localStorage.setItem('crm:lang', 'ru');
  }, session);
  return session;
}
export const test = base;
export { expect };
