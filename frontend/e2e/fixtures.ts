import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test';

export const apiURL = process.env.E2E_API_URL || 'http://localhost:8000';
export const unique = (prefix = 'e2e') => `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
export async function createSession(request: APIRequestContext, role = 'owner') {
  const response = await request.post(`${apiURL}/v1/auth/login`, {
    data: { email: `${role}@demo.kz`, password: 'demo12345' },
  });
  expect(response.ok(), `login: ${response.status()} ${await response.text()}`).toBeTruthy();
  const session = await response.json();
  return { ...session, headers: { Authorization: `Bearer ${session.access_token}`, 'X-Tenant-Id': session.tenant_id } };
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
