import type { APIRequestContext, Page } from '@playwright/test';
import { apiURL, expect, login, test, unique } from './fixtures';

type Conversation = { id: string; contact: { id: string; name: string }; mode: string };
type Task = { id: string; conversation_id: string; status: string };
type Lead = { id: string; contact: { id: string }; stage: string; lost_reason: string };

async function setup(page: Page, request: APIRequestContext) {
  const session = await login(page, request);
  const name = unique('E2E chat');
  const phone = `+77${Date.now().toString().slice(-7)}${Math.floor(Math.random() * 100).toString().padStart(2, '0')}`;
  const response = await request.post(`${apiURL}/v1/contacts`, {
    headers: session.headers, data: { name, phone, language: 'ru', source: 'manual' },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const contact = await response.json();
  return { session, name, phone, contact };
}

async function simulate(page: Page, name: string, phone: string, text: string) {
  await page.goto('/inbox');
  await page.getByRole('button', { name: 'Симулировать WhatsApp' }).click();
  const dialog = page.getByRole('dialog', { name: 'Тестовое входящее сообщение' });
  await dialog.getByLabel('Имя', { exact: true }).fill(name);
  await dialog.getByLabel('Телефон', { exact: true }).fill(phone);
  await dialog.getByLabel('Сообщение', { exact: true }).fill(text);
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/v1/conversations/simulate') && response.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Отправить в демо-канал' }).click();
  const response = await responsePromise;
  expect(response.ok(), await response.text()).toBeTruthy();
  const result = await response.json();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button', { name: new RegExp(name) }).click();
  await expect(page.getByRole('link', { name, exact: true })).toBeVisible();
  return result as { conversation_id: string; replies: { text: string; author: string }[] };
}

test('inbox: simulator AI reply, human takeover and return to AI', async ({ page, request }) => {
  const { session, name, phone } = await setup(page, request);
  const result = await simulate(page, name, phone, 'Здравствуйте!');
  try {
    const reply = result.replies.find(message => message.author === 'ai');
    expect(reply, 'simulator must produce a real AI response').toBeDefined();
    await expect(page.getByRole('article').getByText(reply!.text, { exact: true })).toBeVisible();
    await expect(page.getByText('AI отвечает', { exact: true }).last()).toBeVisible();
    const composer = page.getByRole('textbox', { name: 'Ответ клиенту' });
    const draft = unique('Черновик до смены режима');
    await composer.fill(draft);
    // A mode update can arrive while the confirmation dialog is still modal.
    // Delay the real permission refresh to exercise that ordering every run.
    await page.route('**/v1/auth/me', async route => {
      const response = await route.fetch();
      await new Promise(resolve => setTimeout(resolve, 500));
      await route.fulfill({ response });
    });
    await page.getByRole('button', { name: 'Взять диалог', exact: true }).click();
    const takeoverResponse = page.waitForResponse(response => response.url().endsWith(`/v1/conversations/${result.conversation_id}/takeover`) && response.request().method() === 'POST');
    const confirmation = page.getByRole('dialog');
    await confirmation.getByRole('button', { name: 'Подтвердить', exact: true }).click();
    const takeover = await takeoverResponse;
    expect(takeover.ok(), await takeover.text()).toBeTruthy();
    await expect(confirmation).not.toBeVisible();
    await page.unroute('**/v1/auth/me');
    await expect(page.getByRole('button', { name: 'Вернуть AI', exact: true })).toBeVisible();
    await expect(composer).toHaveValue(draft);
    const answer = unique('Ответ администратора');
    await composer.fill(answer);
    await expect(composer).toHaveValue(answer);
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expect(page.getByText(answer, { exact: true }).last()).toBeVisible();
    await expect(page.getByText('Человек отвечает', { exact: true }).last()).toBeVisible();
    await page.getByRole('button', { name: 'Вернуть AI', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Подтвердить', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Взять диалог', exact: true })).toBeVisible();
    const response = await request.get(`${apiURL}/v1/conversations/${result.conversation_id}`, { headers: session.headers });
    expect((await response.json() as Conversation).mode).toBe('ai');
  } finally {
    // No conversation/contact delete endpoint exists; restore AI mode even after a failed assertion.
    await request.post(`${apiURL}/v1/conversations/${result.conversation_id}/release`, { headers: session.headers });
  }
});

test('tasks: 30% discount escalation, acknowledge, resolve and relay to client chat', async ({ page, request }) => {
  const { session, name, phone } = await setup(page, request);
  const result = await simulate(page, name, phone, 'Можно скидку 30%?');
  let taskId: string | undefined;
  try {
    const response = await request.get(`${apiURL}/v1/admin-tasks?limit=200`, { headers: session.headers });
    expect(response.ok(), await response.text()).toBeTruthy();
    const task = (await response.json() as { items: Task[] }).items.find(item => item.conversation_id === result.conversation_id);
    expect(task, 'discount question must create an administrator task').toBeDefined();
    taskId = task!.id;
    await page.goto('/tasks');
    const card = page.getByRole('article').filter({ has: page.getByRole('link', { name: new RegExp(name) }) });
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: 'Взять в работу' }).click();
    await page.getByRole('combobox', { name: 'Статус задач' }).selectOption('acked');
    await expect(card.getByText('В работе', { exact: true })).toBeVisible();
    await card.getByRole('button', { name: 'Решить', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Ответить и решить задачу' });
    const answer = unique('Скидку 30% согласовали только для вашего визита');
    await dialog.getByLabel('Проверенный ответ', { exact: true }).fill(answer);
    await dialog.getByLabel('Передать ответ клиенту', { exact: true }).check();
    await dialog.getByRole('button', { name: 'Подтвердить решение' }).click();
    await expect(dialog).not.toBeVisible();
    await page.getByRole('combobox', { name: 'Статус задач' }).selectOption('resolved');
    await expect(card.getByText(answer, { exact: true })).toBeVisible();
    await card.getByRole('link', { name: 'Открыть чат', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/inbox\\?conversation_id=${result.conversation_id}`));
    await expect(page.getByRole('article').getByText(`Администратор уточнил: ${answer}`, { exact: true })).toBeVisible();
    const messages = await request.get(`${apiURL}/v1/conversations/${result.conversation_id}/messages`, { headers: session.headers });
    const outgoing = (await messages.json() as { items: { text: string; direction: string; is_internal_note: boolean }[] }).items;
    expect(outgoing).toEqual(expect.arrayContaining([expect.objectContaining({ text: expect.stringContaining(answer), direction: 'out', is_internal_note: false })]));
  } finally {
    if (taskId) {
      const taskResponse = await request.get(`${apiURL}/v1/admin-tasks/${taskId}`, { headers: session.headers });
      if ((await taskResponse.json() as Task).status !== 'resolved') {
        await request.post(`${apiURL}/v1/admin-tasks/${taskId}/resolve`, { headers: session.headers, data: { answer: 'E2E cleanup: scenario finished', relay_to_client: false } });
      }
    }
    await request.post(`${apiURL}/v1/conversations/${result.conversation_id}/release`, { headers: session.headers });
  }
});

test('leads: moving a unique lead to lost requires a reason', async ({ page, request }) => {
  const { session, name, phone, contact } = await setup(page, request);
  const result = await request.post(`${apiURL}/v1/conversations/simulate`, {
    headers: session.headers, data: { name, phone, text: 'Здравствуйте!' },
  });
  expect(result.ok(), await result.text()).toBeTruthy();
  const response = await request.get(`${apiURL}/v1/leads?limit=200`, { headers: session.headers });
  const lead = (await response.json() as { items: Lead[] }).items.find(item => item.contact.id === contact.id);
  expect(lead).toBeDefined();
  await page.goto('/leads');
  const button = page.getByRole('button', { name, exact: true });
  await expect(button).toBeVisible();
  // The card editor is the supported move path for both mouse and touch users.
  await button.click();
  const dialog = page.getByRole('dialog', { name: 'Изменить этап' });
  await expect(dialog.getByText(name, { exact: true })).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Этап', exact: true }).selectOption('lost');
  const reason = dialog.getByLabel('Причина потери (обязательно)', { exact: true });
  await expect(reason).toHaveAttribute('required', '');
  const submit = dialog.getByRole('button', { name: 'Подтвердить изменение' });
  await expect(submit).toBeDisabled();
  const value = unique('Клиент выбрал другое время');
  await reason.fill(value);
  await submit.click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('region', { name: 'Потеряны', exact: true }).getByRole('button', { name, exact: true })).toBeVisible();
  const updatedResponse = await request.get(`${apiURL}/v1/leads?limit=200`, { headers: session.headers });
  const updated = (await updatedResponse.json() as { items: Lead[] }).items.find(item => item.id === lead!.id);
  expect(updated).toMatchObject({ stage: 'lost', lost_reason: value });
});
