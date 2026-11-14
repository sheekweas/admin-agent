import { request as playwrightRequest } from '@playwright/test';
import { apiURL, expect, login, test, unique } from './fixtures';
import { bookingJson, bookingPhone, cleanupBookingResources, provisionBookingResources, type TestBooking } from './booking-support';

test('calls: simulate a booking, find the call and open its transcript', async ({ page, request }, testInfo) => {
  const session = await login(page, request);
  const probe = await request.get(`${apiURL}/v1/calls?limit=1`, { headers: session.headers });
  expect(probe.ok(), await probe.text()).toBeTruthy();
  const api = await playwrightRequest.newContext({ baseURL: apiURL, extraHTTPHeaders: session.headers });
  const resources = await provisionBookingResources(api);
  const bookings: TestBooking[] = [];
  const name = unique('Voice');
  try {
    const time = new Intl.DateTimeFormat('ru-RU', { timeZone: resources.branch.timezone, hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(resources.slots[0].start_at));
    const [, month, day] = resources.day.split('-');
    await page.goto('/calls');
    await page.getByRole('button', { name: 'Симулировать звонок', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Телефон', { exact: true }).fill(bookingPhone());
    await dialog.getByLabel('Имя', { exact: true }).fill(name);
    await dialog.getByRole('combobox', { name: /^Язык/ }).selectOption('ru');
    await dialog.getByRole('textbox', { name: /Реплики/ }).fill(`Запишите меня на ${resources.service.title_ru} ${day}.${month} в ${time}\nДа, подтверждаю`);
    const responsePromise = page.waitForResponse(response => response.url().endsWith('/v1/calls/simulate') && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Запустить', exact: true }).click();
    const response = await responsePromise;
    expect(response.ok(), await response.text()).toBeTruthy();
    const call = await response.json();
    await testInfo.attach('live-simulate', { body: JSON.stringify({ request: { method: 'POST', path: '/v1/calls/simulate', body: response.request().postDataJSON() }, status: response.status(), response: call }, null, 2), contentType: 'application/json' });
    expect(call.metrics.booking_created, JSON.stringify(call.transcript)).toBe(true);
    expect(call.booking_id).toBeTruthy();
    bookings.push(await bookingJson<TestBooking>(api, 'GET', `/v1/bookings/${call.booking_id}`));
    await expect(page.getByRole('dialog').getByText('Расшифровка', { exact: true })).toBeVisible();
    for (const entry of call.transcript.filter((entry: { speaker: string }) => entry.speaker === 'client')) {
      await expect(page.getByRole('dialog').getByText(entry.text, { exact: true })).toBeVisible();
    }
    await page.goto('/calls');
    await expect(page.locator('main').getByText(name, { exact: true }).filter({ visible: true })).toBeVisible();
    await page.goto(`/calls?call_id=${call.id}`);
    await expect(page.getByRole('dialog').getByText('Расшифровка', { exact: true })).toBeVisible();
    const bookingLink = page.getByRole('dialog').getByRole('link', { name: /Бронь|Запись/ });
    await expect(bookingLink).toHaveAttribute('href', `/calendar?booking_id=${call.booking_id}`);
    await bookingLink.click();
    await expect(page).toHaveURL(new RegExp(`/calendar\\?booking_id=${call.booking_id}`));
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.goto('/inbox');
    await page.getByRole('textbox', { name: 'Поиск диалогов', exact: true }).fill(name);
    const conversation = page.getByRole('button').filter({ has: page.getByText(name, { exact: true }) });
    await expect(conversation.getByLabel('Голосовой звонок', { exact: true })).toBeVisible();
    await conversation.click();
    for (const entry of call.transcript.filter((entry: { speaker: string }) => entry.speaker === 'client')) {
      await expect(page.getByRole('article').getByText(entry.text, { exact: true })).toBeVisible();
    }
    await expect(page.getByText('Реплика звонка', { exact: true }).first()).toBeVisible();
  } finally {
    await cleanupBookingResources(api, resources, bookings);
    await api.dispose();
  }
});

test('calls: missing consent disables calling; revoked consent returns a friendly CONSENT_REQUIRED error', async ({ page, request }, testInfo) => {
  const session = await login(page, request);
  const response = await request.post(`${apiURL}/v1/contacts`, { headers: session.headers, data: { name: unique('Voice consent'), phone: bookingPhone(), source: 'manual', consent_flags: { outbound_calls: false } } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const contact = await response.json();
  await page.goto(`/clients/${contact.id}`);
  const button = page.getByRole('button', { name: 'Позвонить клиенту', exact: true });
  await expect(button).toBeDisabled();
  await expect(button).toHaveAccessibleDescription('Нет согласия на исходящие звонки');
  const probe = await request.get(`${apiURL}/v1/calls?limit=1`, { headers: session.headers });
  expect(probe.ok(), await probe.text()).toBeTruthy();
  const grant = await request.patch(`${apiURL}/v1/contacts/${contact.id}`, { headers: session.headers, data: { consent_flags: { ...contact.consent_flags, outbound_calls: true } } });
  expect(grant.ok(), await grant.text()).toBeTruthy();
  const tenantResponse = await request.get(`${apiURL}/v1/tenant`, { headers: session.headers });
  expect(tenantResponse.ok(), await tenantResponse.text()).toBeTruthy();
  const tenant = await tenantResponse.json();
  const settings = tenant.settings;
  try {
    const allow = await request.patch(`${apiURL}/v1/tenant`, { headers: session.headers, data: { settings: {
      ...settings,
      voice: { ...settings.voice, outbound_hours: [0, 24], max_outbound_per_day: 10000 },
      escalation: { ...settings.escalation, quiet_hours: [0, 0] },
    } } });
    expect(allow.ok(), await allow.text()).toBeTruthy();
    await page.reload();
    await expect(button).toBeEnabled();
    await button.click();
    const dialog = page.getByRole('dialog', { name: 'Позвонить клиенту?' });
    // Revoke after opening to exercise the server guard against stale consent.
    const revoke = await request.patch(`${apiURL}/v1/contacts/${contact.id}`, { headers: session.headers, data: { consent_flags: { ...contact.consent_flags, outbound_calls: false } } });
    expect(revoke.ok(), await revoke.text()).toBeTruthy();
    const rejected = page.waitForResponse(response => response.url().endsWith('/v1/calls/outbound') && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Подтвердить звонок', exact: true }).click();
    const denied = await rejected;
    expect(denied.status()).toBe(409);
    const denial = await denied.json();
    expect(denial.detail.code).toBe('CONSENT_REQUIRED');
    await testInfo.attach('live-consent-required', { body: JSON.stringify({ request: { method: 'POST', path: '/v1/calls/outbound', body: denied.request().postDataJSON() }, status: denied.status(), response: denial }, null, 2), contentType: 'application/json' });
    await expect(dialog.getByRole('alert')).toContainText('согласи');
    const consent = await request.patch(`${apiURL}/v1/contacts/${contact.id}`, { headers: session.headers, data: { consent_flags: { ...contact.consent_flags, outbound_calls: true } } });
    expect(consent.ok(), await consent.text()).toBeTruthy();
    const placed = page.waitForResponse(response => response.url().endsWith('/v1/calls/outbound') && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Подтвердить звонок', exact: true }).click();
    const accepted = await placed;
    expect(accepted.status(), await accepted.text()).toBe(201);
    const call = await accepted.json();
    await testInfo.attach('live-outbound', { body: JSON.stringify({ request: { method: 'POST', path: '/v1/calls/outbound', body: accepted.request().postDataJSON() }, status: accepted.status(), response: call }, null, 2), contentType: 'application/json' });
    expect(['queued', 'ringing']).toContain(call.status);
    expect(call.contact.id).toBe(contact.id);
    expect(call.direction).toBe('out');
    expect(call.purpose).toBe('callback');
    await dialog.getByRole('link', { name: 'Открыть звонок', exact: true }).click();
    await expect(page.getByRole('dialog').getByText('Расшифровка', { exact: true })).toBeVisible();
    await expect(page.getByRole('dialog').getByRole('link', { name: contact.name, exact: true })).toBeVisible();
    const listed = await request.get(`${apiURL}/v1/calls?contact_id=${contact.id}&direction=out&purpose=callback,reminder&status=queued,ringing`, { headers: session.headers });
    expect(listed.ok(), await listed.text()).toBeTruthy();
    expect((await listed.json()).items.map((item: { id: string }) => item.id)).toContain(call.id);
    await page.goto('/calls');
    await expect(page.locator('main').getByText(contact.name, { exact: true }).filter({ visible: true })).toBeVisible();
  } finally {
    const restored = await request.patch(`${apiURL}/v1/tenant`, { headers: session.headers, data: { settings } });
    expect(restored.ok(), await restored.text()).toBeTruthy();
    await request.patch(`${apiURL}/v1/contacts/${contact.id}`, { headers: session.headers, data: { consent_flags: { ...contact.consent_flags, outbound_calls: false } } });
  }
});

test('calls: barber has no navigation item, page access or API access', async ({ page, request }) => {
  const session = await login(page, request, 'barber');
  await page.goto('/calls');
  await expect(page.locator('nav a[href="/calls"]')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Нет доступа', exact: true })).toBeVisible();
  const response = await request.get(`${apiURL}/v1/calls`, { headers: session.headers });
  expect(response.status()).toBe(403);
  const report = await request.get(`${apiURL}/v1/reports/voice`, { headers: session.headers });
  expect(report.status()).toBe(403);
});


test('calls: analyst sees voice analytics without call-list access', async ({ page, request }) => {
  const session = await login(page, request, 'analyst');
  const report = await request.get(`${apiURL}/v1/reports/voice`, { headers: session.headers });
  expect(report.ok(), await report.text()).toBeTruthy();
  expect(await report.json()).toMatchObject({ calls: expect.any(Number), answered: expect.any(Number) });
  const calls = await request.get(`${apiURL}/v1/calls`, { headers: session.headers });
  expect(calls.status()).toBe(403);
  await page.goto('/analytics');
  await expect(page.getByRole('heading', { name: 'Голосовой канал', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Голосовой канал', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Все звонки', exact: true })).toHaveCount(0);
});
