import { request as playwrightRequest, type Locator, type Page } from '@playwright/test';
import { apiURL, expect, login, test, unique } from './fixtures';
import { bookingJson, bookingPhone, cleanupBookingResources, provisionBookingResources, type TestBooking } from './booking-support';

async function confirm(page: Page, action: Locator) {
  await action.click();
  await page.getByRole('dialog').last().getByRole('button', {name: 'Подтвердить', exact: true}).click();
}

async function openNewBooking(page: Page, resources: Awaited<ReturnType<typeof provisionBookingResources>>, name: string, phone: string) {
  await page.goto('/calendar');
  await page.getByRole('combobox', {name: 'Мастер', exact: true}).selectOption(resources.staff.id);
  await page.getByLabel('Дата', {exact: true}).fill(resources.day);
  await page.getByRole('button', {name: 'Новая запись', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: 'Новая запись', exact: true});
  await dialog.getByRole('button', {name: 'Новый клиент', exact: true}).click();
  await dialog.getByLabel('Имя', {exact: true}).fill(name);
  await dialog.getByLabel('Телефон', {exact: true}).fill(phone);
  await dialog.getByRole('combobox', {name: /^Услуга/}).selectOption(resources.service.id);
  await expect(dialog.locator('.slot-choice').first()).toBeVisible();
  await dialog.locator('.slot-choice').first().click();
  return dialog;
}

test('calendar: new client booking, confirmed color, visit and payment increase dashboard revenue', async ({page, request}) => {
  const session = await login(page, request);
  const api = await playwrightRequest.newContext({baseURL: apiURL, extraHTTPHeaders: session.headers});
  const resources = await provisionBookingResources(api);
  const bookings: TestBooking[] = [], paymentIds: string[] = [];
  try {
    await page.goto('/');
    const revenue = page.locator('.metric-card').filter({hasText: 'Оплаченная выручка'}).locator('.metric-value');
    await expect(revenue).toBeVisible();
    const before = Number((await revenue.innerText()).replace(/[^\d,.-]/g, '').replace(',', '.'));
    const name = unique('E2E visit');
    const dialog = await openNewBooking(page, resources, name, bookingPhone());
    const created = page.waitForResponse(r => r.url().endsWith('/v1/bookings') && r.request().method() === 'POST');
    await dialog.getByRole('button', {name: 'Создать запись', exact: true}).click();
    const response = await created;
    expect(response.status(), await response.text()).toBe(201);
    const booking = await response.json() as TestBooking;
    bookings.push(booking);
    const tile = page.locator('.calendar-booking').filter({hasText: name});
    await expect(tile).toHaveClass(/status-confirmed/);
    await expect(tile).toHaveCSS('background-color', 'rgb(204, 251, 241)');
    await tile.click();
    const drawer = page.getByRole('dialog').first();
    await confirm(page, drawer.getByRole('button', {name: 'Клиент пришёл', exact: true}));
    await expect(drawer.locator('.booking-status')).toContainText('пришёл');
    await confirm(page, drawer.getByRole('button', {name: 'Завершить', exact: true}));
    await expect(drawer.locator('.booking-status')).toContainText('Заверш');
    await drawer.getByRole('button', {name: 'Добавить оплату', exact: true}).click();
    const paid = page.waitForResponse(r => r.url().endsWith('/v1/payments') && r.request().method() === 'POST');
    await confirm(page, drawer.getByRole('button', {name: 'Подтвердить оплату', exact: true}));
    const paymentResponse = await paid;
    expect(paymentResponse.status(), await paymentResponse.text()).toBe(201);
    paymentIds.push((await paymentResponse.json()).id);
    await page.goto('/');
    await expect(revenue).toBeVisible();
    await expect.poll(async () => Number((await revenue.innerText()).replace(/[^\d,.-]/g, '').replace(',', '.'))).toBe(before + 1234);
  } finally {
    await cleanupBookingResources(api, resources, bookings, paymentIds);
    await api.dispose();
  }
});

test('calendar: real slot conflict shows 409 alternatives and books a selected alternative', async ({page, request}) => {
  const session = await login(page, request);
  const api = await playwrightRequest.newContext({baseURL: apiURL, extraHTTPHeaders: session.headers});
  const resources = await provisionBookingResources(api);
  const bookings: TestBooking[] = [];
  try {
    const name = unique('E2E conflict');
    const dialog = await openNewBooking(page, resources, name, bookingPhone());
    // Occupy the selected slot after the UI fetched it: no network mocking, a real stale-slot race.
    bookings.push(await bookingJson<TestBooking>(api, 'POST', '/v1/bookings', {
      contact: {name: unique('E2E occupied'), phone: bookingPhone()}, service_id: resources.service.id,
      slot_id: resources.slots[0].slot_id, branch_id: resources.branch.id, staff_id: resources.staff.id,
    }));
    const conflict = page.waitForResponse(r => r.url().endsWith('/v1/bookings') && r.request().method() === 'POST');
    await dialog.getByRole('button', {name: 'Создать запись', exact: true}).click();
    const response = await conflict;
    expect(response.status(), await response.text()).toBe(409);
    await expect(dialog.getByRole('heading', {name: /Доступные альтернативы/})).toBeVisible();
    await dialog.locator('.slot-choice').first().click();
    const created = page.waitForResponse(r => r.url().endsWith('/v1/bookings') && r.request().method() === 'POST');
    await dialog.getByRole('button', {name: 'Создать запись', exact: true}).click();
    const alternative = await created;
    expect(alternative.status(), await alternative.text()).toBe(201);
    bookings.push(await alternative.json());
    await expect(page.locator('.calendar-booking').filter({hasText: name})).toBeVisible();
  } finally {
    await cleanupBookingResources(api, resources, bookings);
    await api.dispose();
  }
});
