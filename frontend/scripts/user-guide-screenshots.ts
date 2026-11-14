/** Demo-only screenshots. Run from frontend: npm run guide:screenshots.
 * Uses real UI and real API responses; never mocks application data.
 * Requires the existing dev dependencies and Next's installed sharp package.
 */
import { chromium, expect, type Page, type Locator } from '@playwright/test';
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const sharp = require('sharp');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(root, 'docs/user-guide/img');
const base = process.env.GUIDE_BASE_URL || 'http://localhost:3000';
if (!['localhost', '127.0.0.1'].includes(new URL(base).hostname)) throw new Error('Use a local demo stack only.');
let activeRole = '';
const report = { completed: false, capturedAt: new Date().toISOString(), screenshots: [] as unknown[], pages: [] as unknown[], interactions: [] as string[] };
await fs.mkdir(out, { recursive: true });
const browser = await chromium.launch({ channel: process.env.GUIDE_CHANNEL || 'chrome', headless: true });
async function ready(page: Page) {
  await page.locator('main').waitFor();
  await expect(page.locator('.skeleton').first()).toBeHidden({ timeout: 30000 });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(700);
}
async function shot(page: Page, name: string) {
  await ready(page);
  // Dismiss transient success messages using their own UI controls.
  if (await page.locator('dialog[open]').count()) await expect(page.locator('.toast:not(.toast-error)')).toHaveCount(0, { timeout: 10000 });
  else while (await page.locator('.toast:not(.toast-error) button').count()) await page.locator('.toast:not(.toast-error) button').first().click();
  if (await page.locator('.toast-error').count()) throw new Error(`UI error while capturing ${name}: ${await page.locator('.toast-error').allTextContents()}`);
  // Password fields stay masked. Integration editors / audit payloads are never expanded.
  const raw = await page.screenshot({ animations: 'disabled', fullPage: false, caret: 'hide' });
  let png: Buffer;
  for (const colours of [256, 128, 64, 32]) {
    png = await sharp(raw).png({ palette: true, colours, compressionLevel: 9, effort: 10 }).toBuffer();
    if (png.length <= 300 * 1024) break;
  }
  if (png!.length > 300 * 1024) throw new Error(`Oversized screenshot: ${name}`);
  await fs.writeFile(path.join(out, `${name}.png`), png!);
  report.screenshots.push({ file: `img/${name}.png`, bytes: png!.length, viewport: page.viewportSize() });
  console.log(`Captured ${name}`);
}
async function visit(page: Page, url: string, name: string) {
  await page.goto(base + url); await ready(page);
  report.pages.push({ role: activeRole, path: url, heading: await page.locator('main h1').allTextContents(), text: (await page.locator('main').innerText()).slice(0, 16000) });
  await shot(page, name);
}
async function close(page: Page) {
  for (let i = 0; i < 4 && await page.locator('dialog[open]').count(); i++) {
    await page.locator('dialog[open]').last().locator(':scope > .dialog-header > button').click();
  }
}
async function dialog(page: Page, button: Locator, name: string) {
  await button.click(); await expect(page.locator('dialog[open]').last()).toBeVisible();
  report.interactions.push(name + ': ' + (await page.locator('dialog[open]').last().innerText()).slice(0, 5000));
  await shot(page, name); await close(page);
}
try {
 for (const role of ['owner', 'admin', 'barber', 'analyst']) {
  activeRole = role;
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ru-RU', timezoneId: 'Asia/Almaty', reducedMotion: 'reduce' });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  await page.goto(base + '/login');
  if (role === 'owner') await shot(page, 'login');
  await page.locator('input[type=email]').fill(`${role}@demo.kz`);
  await page.locator('input[type=password]').fill(process.env.GUIDE_DEMO_PASSWORD || 'demo12345');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await page.waitForURL(base + '/'); await ready(page);
  const links = await page.locator('aside nav a').evaluateAll(nodes => nodes.map(n => ({ url: n.getAttribute('href')!, text: n.textContent })));
  report.pages.push({ role, navigation: links });
  for (const link of links) {
    await visit(page, link.url, `${role}-${link.url === '/' ? 'dashboard' : link.url.slice(1)}`);
    if (role === 'owner') await fs.copyFile(path.join(out, `owner-${link.url === '/' ? 'dashboard' : link.url.slice(1)}.png`), path.join(out, `${link.url === '/' ? 'dashboard' : link.url.slice(1)}.png`));
  }
  if (role === 'owner') {
    // Synthetic numbers/names are deliberately marked as training data. Simulators
    // use demo providers; no outbound production call/message is triggered.
    await page.goto(base + '/inbox'); await ready(page);
    await page.getByRole('button', { name: 'Симулировать WhatsApp', exact: true }).click();
    let modal = page.getByRole('dialog');
    await modal.getByLabel('Имя', { exact: true }).fill('Демо • Учебный клиент');
    await modal.getByLabel('Телефон', { exact: true }).fill('+77000000101');
    await modal.getByLabel('Сообщение', { exact: true }).fill('Здравствуйте! Сколько стоит стрижка?');
    await shot(page, 'inbox-simulator');
    await modal.getByRole('button', { name: 'Отправить в демо-канал' }).click();
    await expect(modal).toBeHidden();
    await page.getByRole('button', { name: /Демо • Учебный клиент/ }).click();
    await shot(page, 'inbox-dialog');
    await dialog(page, page.getByRole('button', { name: 'Взять диалог', exact: true }), 'inbox-takeover');
    await page.getByRole('button', { name: 'Взять диалог', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Подтвердить', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await page.getByLabel('Внутренняя заметка — клиент не увидит', { exact: true }).check();
    await page.getByRole('textbox', { name: 'Текст заметки', exact: true }).fill('Учебный диалог: уточнить удобное время визита.');
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await shot(page, 'inbox-human');
    await page.getByRole('button', { name: 'Вернуть AI', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Подтвердить', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await page.getByRole('button', { name: 'Симулировать WhatsApp', exact: true }).click();
    modal = page.getByRole('dialog');
    await modal.getByLabel('Имя', { exact: true }).fill('Демо • Вопрос администратору');
    await modal.getByLabel('Телефон', { exact: true }).fill('+77000000102');
    await modal.getByLabel('Сообщение', { exact: true }).fill('Можно скидку 30%?');
    await modal.getByRole('button', { name: 'Отправить в демо-канал' }).click(); await expect(modal).toBeHidden();
    await visit(page, '/tasks', 'tasks');
    const task = page.getByRole('article').filter({ has: page.getByRole('link', { name: /Демо • Вопрос администратору/ }) }).first();
    await dialog(page, task.getByRole('button', { name: 'Решить', exact: true }), 'task-detail');
    await task.getByRole('button', { name: 'Взять в работу', exact: true }).click();
    await page.getByRole('combobox', { name: 'Статус задач', exact: true }).selectOption('acked'); await ready(page);
    await task.getByRole('button', { name: 'Решить', exact: true }).click();
    await page.getByLabel('Проверенный ответ', { exact: true }).fill('Учебный ответ: скидка 30% не предусмотрена. Действует опубликованный прайс.');
    await page.getByLabel('Добавить в знания как черновик', { exact: true }).check();
    await shot(page, 'task-answer');
    await page.getByRole('button', { name: 'Подтвердить решение', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeHidden();
    await page.getByRole('combobox', { name: 'Статус задач', exact: true }).selectOption('resolved'); await ready(page);
    await shot(page, 'task-resolved');
    report.interactions.push('Task acknowledged and resolved through UI; simulated client received answer; knowledge draft requested.');
    await page.goto(base + '/calls'); await ready(page);
    await page.getByRole('button', { name: 'Симулировать звонок', exact: true }).click(); modal = page.getByRole('dialog');
    await modal.getByLabel('Телефон', { exact: true }).fill('+77000000103');
    await modal.getByLabel('Имя', { exact: true }).fill('Демо • Голосовой клиент');
    await modal.getByLabel('Реплики клиента', { exact: true }).fill('Здравствуйте!\nСколько стоит стрижка?\nСпасибо!');
    await shot(page, 'call-simulator');
    await modal.getByRole('button', { name: 'Запустить', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Расшифровка', exact: true })).toBeVisible({ timeout: 30000 });
    await shot(page, 'call-detail');
    await page.getByRole('heading', { name: 'Расшифровка', exact: true }).scrollIntoViewIfNeeded(); await shot(page, 'call-transcript');
    await close(page); await shot(page, 'calls');
    await visit(page, '/calendar', 'calendar');
    await page.getByRole('button', { name: 'Новая запись', exact: true }).click();
    const bookingForm = page.getByRole('dialog', { name: 'Новая запись', exact: true });
    await bookingForm.getByRole('button', { name: 'Новый клиент', exact: true }).click();
    await bookingForm.getByLabel('Имя', { exact: true }).fill('Демо • Учебная запись');
    await bookingForm.getByLabel('Телефон', { exact: true }).fill('+77000000104');
    const serviceSelect = bookingForm.getByRole('combobox', { name: /^Услуга/ });
    const serviceValue = await serviceSelect.locator('option').nth(1).getAttribute('value');
    await serviceSelect.selectOption(serviceValue!);
    // Search a few future days without inventing availability.
    for (let days = 1; days <= 7; days++) {
      const day = new Date(Date.now() + days * 86400000).toLocaleDateString('en-CA', { timeZone: 'Asia/Almaty' });
      await bookingForm.getByLabel('Дата', { exact: true }).fill(day); await ready(page);
      if (await bookingForm.locator('.slot-choice').count()) break;
    }
    await expect(bookingForm.locator('.slot-choice').first()).toBeVisible();
    await bookingForm.locator('.slot-choice').first().click();
    await shot(page, 'calendar-create'); await close(page);
    await dialog(page, page.getByRole('button', { name: 'Блокировать время', exact: true }), 'calendar-block');
    if (await page.locator('.calendar-booking').count()) {
      await page.locator('.calendar-booking').first().click(); await shot(page, 'calendar-detail');
      await dialog(page, page.getByRole('button', { name: 'Перенести запись', exact: true }), 'calendar-reschedule');
      await page.goto(base + '/calendar'); await ready(page); await page.locator('.calendar-booking').first().click();
      await page.getByRole('button', { name: 'Добавить оплату', exact: true }).click(); await shot(page, 'calendar-payment'); await close(page);
    }
    await visit(page, '/clients', 'clients');
    await dialog(page, page.getByRole('button', { name: 'Добавить клиента', exact: true }), 'client-create');
    await page.locator('main').getByRole('textbox', { name: 'Поиск клиентов', exact: true }).fill('Демо • Учебный клиент'); await ready(page);
    const clientLink = page.locator('main a[href^="/clients/"]').first(); await clientLink.click(); await ready(page); await shot(page, 'client-detail');
    report.interactions.push('client-detail: ' + (await page.locator('main').innerText()).slice(0,12000));
    for (const tab of ['Записи', 'Оплаты', 'Лиды', 'История']) {
      await page.getByRole('tab', { name: tab, exact: true }).click(); await ready(page);
      report.interactions.push(`client tab ${tab}: ${(await page.locator('main').innerText()).slice(-4000)}`);
    }
    await visit(page, '/leads', 'leads');
    await dialog(page, page.getByRole('button', { name: 'Демо • Учебный клиент', exact: true }), 'lead-stage');
    await visit(page, '/staff', 'staff');
    await dialog(page, page.locator('.card').filter({ has: page.getByRole('heading', { name: /Недельный график/ }) }).getByRole('button', { name: 'Изменить', exact: true }), 'staff-schedule');
    await visit(page, '/services', 'services');
    await dialog(page, page.getByRole('button', { name: 'Изменить', exact: true }).first(), 'service-edit');
    await visit(page, '/finance', 'finance');
    await dialog(page, page.getByRole('button', { name: 'Добавить оплату', exact: true }), 'finance-payment');
    if (await page.getByRole('button', { name: 'Возврат', exact: true }).count()) await dialog(page, page.getByRole('button', { name: 'Возврат', exact: true }).first(), 'finance-refund');
    await visit(page, '/ai', 'ai');
    await dialog(page, page.getByRole('button', { name: 'Новая статья', exact: true }), 'ai-knowledge');
    await page.getByRole('tab', { name: 'Тестировать AI', exact: true }).click();
    await page.getByLabel('Вопрос клиента', { exact: true }).fill('Сколько стоит стрижка?');
    await page.getByRole('button', { name: 'Получить тестовый ответ', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Получить тестовый ответ', exact: true })).toBeVisible(); await ready(page); await shot(page, 'ai-test');
    await page.getByRole('tab', { name: 'Настройки AI', exact: true }).click(); await shot(page, 'ai-settings');
    await visit(page, '/analytics', 'analytics');
    for (const format of ['Экспорт CSV', 'XLSX']) {
      const downloaded = page.waitForEvent('download');
      await page.getByRole('button', { name: format, exact: true }).click();
      const file = await downloaded; if (await file.failure()) throw new Error(`Export ${format} failed`);
      const stream = await file.createReadStream(); let bytes = 0; for await (const chunk of stream!) bytes += chunk.length;
      if (!bytes) throw new Error(`Empty export ${format}`);
      report.interactions.push(`Export ${file.suggestedFilename()}: ${bytes} bytes`);
    }
    await page.getByRole('heading', { name: 'Команда в цифрах', exact: true }).scrollIntoViewIfNeeded(); await shot(page, 'analytics-staff');
    await visit(page, '/integrations', 'integrations');
    await visit(page, '/settings', 'settings');
    await dialog(page, page.getByRole('button', { name: 'Правила записи', exact: true }), 'settings-booking');
    await dialog(page, page.getByRole('button', { name: 'Эскалация вопросов', exact: true }), 'settings-escalation');
    await page.getByRole('heading', { name: 'Команда и роли', exact: true }).scrollIntoViewIfNeeded(); await shot(page, 'settings-team');
    await page.getByRole('heading', { name: 'Журнал аудита', exact: true }).scrollIntoViewIfNeeded(); await shot(page, 'settings-audit');
    await page.goto(base + '/'); await ready(page);
    await page.getByRole('button', { name: /RU: / }).click(); await shot(page, 'dashboard-kz');
    await page.getByRole('button', { name: /KZ: / }).click();
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click(); await shot(page, 'dashboard-dark');
    await page.getByRole('button', { name: 'Сменить тему', exact: true }).click();
  }
  if (role === 'barber') {
    await page.goto(base + '/staff'); await ready(page);
    await dialog(page, page.getByRole('button', { name: 'Запрос на смену', exact: true }), 'barber-shift-request');
  }
  await page.setViewportSize({ width: 390, height: 844 });
  for (const url of role === 'owner' ? ['/', '/calendar', '/inbox', '/tasks'] : role === 'barber' ? ['/staff'] : ['/']) {
    await visit(page, url, `mobile-${role}-${url === '/' ? 'dashboard' : url.slice(1)}`);
    if (role === 'owner' && url === '/') await fs.copyFile(path.join(out, 'mobile-owner-dashboard.png'), path.join(out, 'mobile-dashboard.png'));
  }
  await context.close();
 }
 report.completed = true;
} finally {
 await fs.writeFile(path.join(root, 'docs/user-guide/verification.json'), JSON.stringify(report, null, 2) + '\n');
 await browser.close();
}
