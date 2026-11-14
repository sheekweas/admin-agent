import { test, expect, request as playwrightRequest, type APIRequestContext, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { createSession } from './fixtures';
import { bookingJson, bookingPhone, provisionBookingResources, cleanupBookingResources, type BookingSlot, type TestBooking } from './booking-support';

const apiURL = process.env.E2E_API_URL || 'http://localhost:8000';

async function authenticate(page: Page, request: APIRequestContext, email = 'owner@demo.kz') {
  const response = await request.post(`${apiURL}/v1/auth/login`, { data: { email, password: 'demo12345' } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const login = await response.json();
  await page.addInitScript(value => {
    localStorage.setItem('crm:token', value.access_token);
    localStorage.setItem('crm:tenant', value.tenant_id);
    localStorage.setItem('crm:tenants', JSON.stringify(value.tenants));
  }, login);
  return { Authorization: `Bearer ${login.access_token}`, 'X-Tenant-Id': login.tenant_id as string };
}

test('owner logs in, sees the organization, and logs out', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email', { exact: true }).fill('owner@demo.kz');
  await page.getByLabel('Пароль', { exact: true }).fill('demo12345');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  // The workspace identity remains visible on narrow screens as well.
  await expect(page.getByTestId('workspace-name').filter({ visible: true })).toBeVisible();
  await expect(page.getByTestId('workspace-name').filter({ visible: true })).toHaveText('Barber KZ');
  await page.getByRole('button', { name: 'Выйти', exact: true }).filter({ visible: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBeNull();
});

test('wrong password shows an error without signing in', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email', { exact: true }).fill('owner@demo.kz');
  await page.getByLabel('Пароль', { exact: true }).fill('wrong-password');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await expect(page.locator('.form-error')).toHaveText('Invalid email or password');
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBeNull();
});

test('a real API 401 clears the expired session and redirects to login', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('crm:token', 'expired-e2e-token'));
  const unauthorized = page.waitForResponse(response => response.url().endsWith('/v1/auth/me') && response.status() === 401);
  await page.goto('/calendar');
  await unauthorized;
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(() => localStorage.getItem('crm:token'))).toBeNull();
});

test('RU and KZ language and dark theme preferences survive reload', async ({ page, request }) => {
  await authenticate(page, request);
  await page.goto('/');
  await page.getByRole('button', { name: /RU: / }).click();
  await expect(page.getByRole('heading', { name: 'Бизнеске шолу' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Бизнеске шолу' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'kk');
  await page.getByRole('button', { name: /KZ: / }).click();
  await expect(page.getByRole('heading', { name: 'Обзор бизнеса' })).toBeVisible();
  await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.reload();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
  await expect(page.locator('html')).not.toHaveClass(/dark/);
});

test('barber sees only their schedule and bookings and cannot access restricted pages', async ({ page, request }) => {
  const headers = await authenticate(page, request, 'barber@demo.kz');
  const meResponse = await request.get(`${apiURL}/v1/auth/me`, { headers });
  const me = await meResponse.json();
  expect(me.staff_id).toBeTruthy();
  const owner = await createSession(request);
  const ownerAPI = await playwrightRequest.newContext({ baseURL: apiURL, extraHTTPHeaders: owner.headers });
  const resources = await provisionBookingResources(ownerAPI);
  const created: TestBooking[] = [];
  try {
  const forbiddenSchedule = await request.get(`${apiURL}/v1/staff/${resources.staff.id}/shifts`, {headers});
  expect(forbiddenSchedule.status()).toBe(403);
  const staffList = await bookingJson<{items: {id: string; branch_id: string; service_ids: string[]}[]}>(ownerAPI, 'GET', '/v1/staff?limit=200');
  const own = staffList.items.find(person => person.id === me.staff_id)!;
  expect(own.service_ids.length, 'Demo barber must offer a service').toBeGreaterThan(0);
  const ownAvailability = await bookingJson<{items: BookingSlot[]}>(ownerAPI, 'GET', `/v1/availability?${new URLSearchParams({service_id: own.service_ids[0], staff_id: own.id, branch_id: own.branch_id, date_from: resources.day, date_to: new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10)})}`);
  expect(ownAvailability.items.length, 'Demo barber needs a free slot tomorrow').toBeGreaterThan(0);
  const day = new Intl.DateTimeFormat('en-CA', {timeZone: resources.branch.timezone, year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(ownAvailability.items[0].start_at));
  const otherAvailability = await bookingJson<{items: BookingSlot[]}>(ownerAPI, 'GET', `/v1/availability?${new URLSearchParams({service_id: resources.service.id, staff_id: resources.staff.id, branch_id: resources.branch.id, date_from: day, date_to: day})}`);
  for (const [name, serviceId, slot] of [
    [`E2E own ${Date.now()}`, own.service_ids[0], ownAvailability.items[0]],
    [`E2E other ${Date.now()}`, resources.service.id, otherAvailability.items[0]],
  ] as const) {
    created.push(await bookingJson<TestBooking>(ownerAPI, 'POST', '/v1/bookings', {contact: {name, phone: bookingPhone()}, service_id: serviceId, slot_id: slot.slot_id, branch_id: slot.branch_id, staff_id: slot.staff_id}));
  }
  const staffResponse = page.waitForResponse(response => response.url().includes('/v1/staff?') && response.ok());
  const bookingsResponse = page.waitForResponse(response => response.url().includes('/v1/bookings?') && response.ok());
  await page.goto('/calendar');
  const staff = await (await staffResponse).json();
  const bookings = await (await bookingsResponse).json();
  const ownStaff = staff.items.find((person: { id: string }) => person.id === me.staff_id);
  expect(ownStaff).toBeTruthy();
  expect(bookings.items.every((booking: { staff: { id: string } }) => booking.staff.id === me.staff_id)).toBeTruthy();
  await expect(page.locator('.calendar-column-title').filter({ hasText: ownStaff.name })).toHaveCount(1);
  await expect(page.locator('.calendar-column-title')).toHaveCount(2);
  const tomorrowBookings = page.waitForResponse(response => response.url().includes('/v1/bookings?') && response.url().includes(day) && response.ok());
  await page.getByLabel('Дата', {exact: true}).fill(day);
  const tomorrow = await (await tomorrowBookings).json();
  expect(tomorrow.items.some((booking: { id: string }) => booking.id === created[0].id)).toBeTruthy();
  expect(tomorrow.items.some((booking: { id: string }) => booking.id === created[1].id)).toBeFalsy();
  await expect(page.locator('.calendar-booking').filter({ hasText: created[0].contact.name })).toBeVisible();
  await expect(page.locator('.calendar-booking').filter({ hasText: created[1].contact.name })).toHaveCount(0);
  for (const path of ['/finance', '/analytics', '/integrations']) {
    await expect(page.locator(`nav a[href="${path}"]`)).toHaveCount(0);
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Нет доступа', exact: true })).toBeVisible();
  }
  await page.goto('/staff');
  await expect(page.getByRole('heading', { name: 'Мой график', exact: true })).toBeVisible();
  await expect(page.locator('main button').filter({ hasText: ownStaff.name })).toHaveCount(1);
  } finally {
    await cleanupBookingResources(ownerAPI, resources, created);
    await ownerAPI.dispose();
  }
});

test('analyst can view reports but cannot edit clients, staff, services or bookings', async ({ page, request }) => {
  await authenticate(page, request, 'analyst@demo.kz');
  await page.goto('/analytics');
  await expect(page.getByRole('heading', { name: 'Аналитика', exact: true })).toBeVisible();
  await expect(page.locator('.metric-value').first()).toBeVisible();
  for (const path of ['/clients', '/staff', '/services', '/calendar']) {
    await page.goto(path);
    await expect(page.locator('main h1')).toBeVisible();
    await expect(page.locator('main').getByRole('button', { name: /^(Добавить|Изменить|Новая запись|Блокировать время|Клиент|Сотрудник|Услуга)$/ })).toHaveCount(0);
  }
});

for (const route of ['/', '/calendar', '/inbox', '/clients']) {
  test(`accessibility: ${route} has no serious or critical axe violations`, async ({ page, request }, testInfo) => {
    await authenticate(page, request);
    await page.goto(route);
    await expect(page.locator('main h1')).toBeVisible();
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    const results = await new AxeBuilder({ page }).analyze();
    const violations = results.violations.filter(violation => ['serious', 'critical'].includes(violation.impact || ''));
    await testInfo.attach('axe-results', { body: JSON.stringify(results, null, 2), contentType: 'application/json' });
    expect(violations.map(({id, nodes}) => ({id, targets: nodes.map(node => node.target)}))).toEqual([]);
  });
}
