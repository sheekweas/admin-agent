import { apiURL, expect, login, test } from './fixtures';

test('voice settings: saving merges the latest complete voice object', async ({ page, request }) => {
  const session = await login(page, request);
  const original = await request.get(`${apiURL}/v1/tenant`, { headers: session.headers });
  expect(original.ok(), await original.text()).toBeTruthy();
  const { settings } = await original.json();
  try {
    await page.goto('/settings');
    await page.getByRole('button', { name: 'Настроить голосовой канал', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Голосовой канал', exact: true });
    // Change a hidden value after opening: save must fetch and preserve the latest object.
    const voice = { ...settings.voice, max_turns: settings.voice.max_turns === 31 ? 32 : 31 };
    const concurrent = await request.patch(`${apiURL}/v1/tenant`, { headers: session.headers, data: { settings: { voice } } });
    expect(concurrent.ok(), await concurrent.text()).toBeTruthy();
    const days = settings.voice.transcript_retention_days === 31 ? 32 : 31;
    await dialog.getByRole('spinbutton', { name: 'Хранение расшифровок, дней', exact: true }).fill(String(days));
    const saved = page.waitForResponse(response => response.url().endsWith('/v1/tenant') && response.request().method() === 'PATCH');
    await dialog.getByRole('button', { name: 'Сохранить', exact: true }).click();
    const response = await saved;
    expect(response.ok(), await response.text()).toBeTruthy();
    expect(response.request().postDataJSON().settings.voice).toEqual({ ...voice, transcript_retention_days: days });
    await expect(dialog).not.toBeVisible();
    const latest = await request.get(`${apiURL}/v1/tenant`, { headers: session.headers });
    expect(latest.ok(), await latest.text()).toBeTruthy();
    expect((await latest.json()).settings.voice).toEqual({ ...voice, transcript_retention_days: days });
  } finally {
    const restored = await request.patch(`${apiURL}/v1/tenant`, { headers: session.headers, data: { settings } });
    expect(restored.ok(), await restored.text()).toBeTruthy();
  }
});
