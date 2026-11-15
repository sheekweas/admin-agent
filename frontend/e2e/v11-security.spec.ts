import { test, expect } from '@playwright/test';
import { at, checkAccessibility, mockSession } from './v11-support';

test.beforeEach(async ({ page }) => {
  await mockSession(page);
  await page.route('**/v1/auth/sessions', route => route.fulfill({ json: { total: 2, items: [
    { id: 'current', user_agent: 'Chrome / Windows', ip: '127.0.0.1', created_at: at, last_seen_at: at, expires_at: at, current: true },
    { id: 'other', user_agent: 'Safari / iPhone', ip: '192.0.2.10', created_at: at, last_seen_at: at, expires_at: at, current: false },
  ] } }));
});

test('security lists devices, changes password, and revokes another session', async ({ page }) => {
  await page.goto('/profile/security');
  await expect(page.getByRole('heading', { name: 'Безопасность' })).toBeVisible();
  await expect(page.getByText('Это устройство', { exact: true })).toBeVisible();
  await expect(page.getByText('192.0.2.10', { exact: true })).toBeVisible();
  await checkAccessibility(page);
  await page.getByLabel('Текущий пароль', { exact: true }).fill('old-password');
  await page.getByLabel('Новый пароль', { exact: true }).fill('new-password-2026');
  await page.getByLabel('Подтвердите новый пароль', { exact: true }).fill('different-password');
  await page.getByRole('button', { name: 'Изменить пароль', exact: true }).click();
  await expect(page.getByRole('main').getByRole('alert')).toContainText('Пароли не совпадают');
  await page.getByLabel('Подтвердите новый пароль', { exact: true }).fill('new-password-2026');
  await page.route('**/v1/auth/change-password', async route => {
    expect(route.request().postDataJSON()).toEqual({ current_password: 'old-password', new_password: 'new-password-2026' });
    await route.fulfill({ json: { revoked_other_sessions: 3 } });
  });
  await page.getByRole('button', { name: 'Изменить пароль', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Завершено других сессий: 3');
  await page.route('**/v1/auth/sessions/other', route => {
    expect(route.request().method()).toBe('DELETE');
    return route.fulfill({ status: 204 });
  });
  await page.route('**/v1/auth/sessions', route => route.fulfill({ json: { total: 1, items: [
    { id: 'current', user_agent: 'Chrome / Windows', ip: '127.0.0.1', created_at: at, last_seen_at: at, expires_at: at, current: true },
  ] } }));
  await page.getByRole('row').filter({ hasText: 'Safari / iPhone' }).getByRole('button').click();
  await expect(page.getByRole('status').filter({ hasText: 'Сессия завершена' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'Safari / iPhone' })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBe('mock-session-token');
});

test('sign out everywhere requires confirmation and clears local credentials', async ({ page }) => {
  let calls = 0;
  await page.route('**/v1/auth/logout-all', route => { calls++; return route.fulfill({ json: { revoked: 2 } }); });
  await page.goto('/profile/security');
  await page.getByRole('button', { name: 'Выйти на всех устройствах', exact: true }).click();
  expect(calls).toBe(0);
  await checkAccessibility(page);
  await page.getByRole('dialog').getByRole('button', { name: 'Подтвердить', exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(calls).toBe(1);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBeNull();
});

test('401 shows the session ended notice once on the login page', async ({ page }) => {
  await page.route('**/v1/auth/me', route => route.fulfill({ status: 401, json: { detail: 'SESSION_REVOKED' } }));
  await page.goto('/profile/security');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText('Сессия завершена — войдите снова', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Сессия завершена — войдите снова', { exact: true })).toHaveCount(0);
});

test('logout calls the server before clearing credentials, even when it fails', async ({ page }) => {
  await page.route('**/v1/auth/logout', async route => {
    expect(route.request().headers().authorization).toBe('Bearer mock-session-token');
    expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBe('mock-session-token');
    await route.abort('failed');
  });
  await page.goto('/profile/security');
  await page.getByRole('button', { name: 'Выйти', exact: true }).filter({ visible: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBeNull();
});
