// Diagnostic for the original E2E race; not part of the suite.
// Run: node scripts/repro-inbox.mjs (E2E_BASE_URL/API_URL/CHANNEL optional).
// fill() does not check whether a native modal makes the textarea inert.
import assert from 'node:assert/strict';
import { chromium, request } from '@playwright/test';
(async () => {
 const api = await request.newContext({ baseURL: process.env.E2E_API_URL || 'http://localhost:8000' });
 const login = await (await api.post('/v1/auth/login', { data: { email: 'owner@demo.kz', password: 'demo12345' } })).json();
 const headers = { Authorization: `Bearer ${login.access_token}`, 'X-Tenant-Id': login.tenant_id };
 const name = `E2E reproduce ${Date.now()}`;
 const result = await (await api.post('/v1/conversations/simulate', { headers, data: { phone: `+77${Date.now().toString().slice(-9)}`, name, text: 'Hello' } })).json();
 const browser = await chromium.launch({ channel: process.env.E2E_CHANNEL || 'chrome' });
 try {
  const page = await browser.newPage();
  await page.addInitScript(value => {
   localStorage.setItem('crm:token', value.access_token);
   localStorage.setItem('crm:tenant', value.tenant_id);
  }, login);
  await page.goto(`${process.env.E2E_BASE_URL || 'http://localhost:3000'}/inbox?conversation_id=${result.conversation_id}`);
  await page.getByRole('button', { name: '\u0412\u0437\u044f\u0442\u044c \u0434\u0438\u0430\u043b\u043e\u0433', exact: true }).click();
  await page.route('**/v1/auth/me', async route => {
   const response = await route.fetch();
   await new Promise(resolve => setTimeout(resolve, 2500));
   await route.fulfill({ response });
  });
  await page.getByRole('dialog').getByRole('button', { name: '\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c', exact: true }).click();
  await page.getByRole('button', { name: '\u0412\u0435\u0440\u043d\u0443\u0442\u044c AI', exact: true }).waitFor();
  const textbox = page.getByRole('textbox', { name: '\u041e\u0442\u0432\u0435\u0442 \u043a\u043b\u0438\u0435\u043d\u0442\u0443' });
  assert.equal(await page.locator('dialog[open]').count(), 1);
  await textbox.fill('reproduction draft');
  assert.equal(await textbox.inputValue(), '');
  await page.locator('dialog[open]').waitFor({ state: 'hidden' });
  assert.equal(await textbox.inputValue(), '');
  console.log('Reproduced: fill ran behind the still-open native modal; no draft was accepted.');
 } finally {
  await api.post(`/v1/conversations/${result.conversation_id}/release`, { headers });
  await browser.close();
  await api.dispose();
 }
})().catch(error => { console.error(error); process.exit(1); });

