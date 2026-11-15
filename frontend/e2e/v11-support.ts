import { expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

export const permissions = ['settings.write', 'users.write', 'team.manage', 'integrations.read', 'integrations.write', 'conversations.read'];
export const at = '2026-11-15T08:00:00Z';
export const conversation = { id: 'conversation-1', contact: { id: 'client-1', name: 'Алия', phone_e164: '+77001234567', opted_out: false }, channel: 'whatsapp', mode: 'human', status: 'open', language: 'ru', branch_id: null, owner_user_id: null, last_message: null, last_message_at: at, unread_count: 0, open_task_id: null };

export async function mockSession(page: Page, options: { permissions?: string[]; lang?: 'ru' | 'kk' } = {}) {
  await page.addInitScript(({ lang }) => {
    // Once only: a reload after logout must not restore the token.
    if (!sessionStorage.getItem('v11:initialized')) {
      localStorage.setItem('crm:token', 'mock-session-token');
      localStorage.setItem('crm:tenant', 'tenant-1');
      localStorage.setItem('crm:lang', lang);
      sessionStorage.setItem('v11:initialized', 'true');
    }
  }, { lang: options.lang ?? 'ru' });
  await page.route('**/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const responses: Record<string, unknown> = {
      '/v1/auth/me': { user_id: 'user-1', email: 'owner@example.test', full_name: 'Владелец', ui_language: 'ru', tenant_id: 'tenant-1', tenant_name: 'Тест CRM', role: 'owner', staff_id: null, branch_scope: [], permissions: options.permissions ?? permissions },
      '/v1/branches': { items: [], total: 0 },
      '/v1/tenant': { id: 'tenant-1', name: 'Тест CRM', niche: 'Салон', default_language: 'ru', settings: { voice: { low_confidence_threshold: 0.7 } } },
      '/v1/team': [{ membership_id: 'member-1', id: 'member-1', email: 'admin@example.test', full_name: 'Администратор Алия', phone_e164: null, telegram_chat_id: null, role: 'admin', is_escalation_primary: false, is_escalation_backup: false }],
      '/v1/integrations': { items: [] },
      '/v1/integrations/webhooks': { items: [], total: 0 },
      '/v1/integrations/events': { items: [], total: 0 },
      '/v1/integrations/stt': { provider: 'openai_compatible', configured: true, model: 'whisper-1' },
      '/v1/conversations': { items: [conversation], total: 1 },
      '/v1/conversations/conversation-1': conversation,
    };
    if (path in responses) await route.fulfill({ json: responses[path] });
    else await route.fulfill({ status: 501, json: { detail: `Unmocked endpoint: ${path}` } });
  });
}

export async function checkAccessibility(page: Page) {
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
