import { test, expect } from '@playwright/test';
import { at, checkAccessibility, mockSession, permissions } from './v11-support';

test('team sign-out confirms and reports the revoked count', async ({ page }) => {
  await mockSession(page);
  let calls = 0;
  await page.route('**/v1/team/member-1/sign-out', route => { calls++; return route.fulfill({ json: { revoked: 4 } }); });
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Выйти за пользователя', exact: true }).click();
  expect(calls).toBe(0);
  await page.getByRole('dialog').getByRole('button', { name: 'Подтвердить', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('4');
  expect(calls).toBe(1);
  await checkAccessibility(page);
});

test('team sign-out is hidden without team.manage', async ({ page }) => {
  await mockSession(page, { permissions: permissions.filter(p => p !== 'team.manage') });
  await page.goto('/settings');
  await expect(page.getByText('Администратор Алия', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Выйти за пользователя', exact: true })).toHaveCount(0);
});

test('voice notes render transcript, confidence, low accuracy, and failure states', async ({ page }) => {
  await mockSession(page);
  await page.route('**/v1/conversations/conversation-1/messages', route => route.fulfill({ json: { total: 3, items: [
    { id: 'voice-1', text: 'Запишите меня на завтра', meta: { transcribed: true, stt: 'mock', stt_confidence: 0.95, stt_language: 'ru' } },
    { id: 'voice-2', text: 'Возможно, в пять', meta: { transcribed: true, stt: 'mock', stt_confidence: 0.3, stt_low_confidence: true } },
    { id: 'voice-3', text: '', meta: { transcribed: false, stt: 'mock', stt_confidence: null, stt_error: 'timeout' } },
  ].map(m => ({ ...m, message_type: 'audio', author: 'client', direction: 'in', media_url: null, provider_status: 'received', is_internal_note: false, received_at: at })) } }));
  await page.goto('/inbox?conversation_id=conversation-1');
  await expect(page.getByText('Голосовое сообщение', { exact: true })).toHaveCount(3);
  await expect(page.getByText('Запишите меня на завтра', { exact: true })).toBeVisible();
  await expect(page.getByText('Точность распознавания: 95%', { exact: true })).toBeVisible();
  await expect(page.getByText('Низкая точность: 30%', { exact: true })).toBeVisible();
  await expect(page.getByText('Не распознано', { exact: true })).toBeVisible();
  await checkAccessibility(page);
});

test('STT card displays configuration and runs its test', async ({ page }) => {
  await mockSession(page);
  await page.route('**/v1/integrations/stt/test', route => route.fulfill({ json: { ok: true, provider: 'openai_compatible', latency_ms: 87, error: null } }));
  await page.goto('/integrations');
  const card = page.getByTestId('stt-card');
  await expect(card.getByRole('heading', { name: 'Распознавание речи' })).toBeVisible();
  await expect(card.getByText('whisper-1', { exact: true })).toBeVisible();
  await expect(card.getByText('Настроено', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: 'Проверить распознавание', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('Распознавание работает');
  await expect(card.getByRole('status')).toContainText('87');
  await checkAccessibility(page);
});

test('STT test action is hidden without integrations.write', async ({ page }) => {
  await mockSession(page, { permissions: ['integrations.read'] });
  await page.goto('/integrations');
  const card = page.getByTestId('stt-card');
  await expect(card.getByText('whisper-1', { exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Проверить распознавание', exact: true })).toHaveCount(0);
});

test('Kazakh STT labels render from the saved language preference', async ({ page }) => {
  await mockSession(page, { lang: 'kk' });
  await page.goto('/integrations');
  const card = page.getByTestId('stt-card');
  await expect(card.getByRole('heading', { name: 'Сөйлеуді тану', exact: true })).toBeVisible();
  await expect(card.getByRole('button', { name: 'Тануды тексеру', exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'kk');
  await checkAccessibility(page);
});
