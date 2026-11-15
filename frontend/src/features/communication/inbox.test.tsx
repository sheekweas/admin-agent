import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import InboxPage from './inbox';
import { SessionProvider } from '@/lib/session';
import Shell from '@/components/shell';

vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: vi.fn() }), usePathname: () => '/inbox' }));
const conversations = ['first', 'second'].map(id => ({ id, mode: 'human', channel: 'whatsapp', unread_count: 0, contact: { id, name: id } }));
let completeSend: (() => void) | undefined;
let failSend = false;
let delaySession = false;
let failSession = false;
let completeSession: (() => void) | undefined;
afterEach(() => vi.unstubAllGlobals());

beforeEach(() => {
  completeSend = undefined;
  completeSession = undefined;
  delaySession = false;
  failSession = false;
  failSend = false;
  localStorage.setItem('crm:token', 'test');
  Element.prototype.scrollIntoView = vi.fn();
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); } });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input).split('/v1/')[1];
    if (path === 'auth/me') {
      if (delaySession) await new Promise<void>(resolve => { completeSession = resolve; });
      if (failSession) return Response.json({ detail: 'Temporary outage' }, { status: 503 });
      return Response.json({ permissions: ['conversations.read', 'conversations.write'], tenant_id: 'tenant' });
    }
    if (path === 'branches') return Response.json({ items: [] });
    if (path === 'tenant') return Response.json({settings:{voice:{low_confidence_threshold:0.7}}});
    if (path?.startsWith('conversations?')) return Response.json({ items: conversations });
    if (path?.endsWith('/messages')) {
      if (init?.method === 'POST') {
        await new Promise<void>(resolve => { completeSend = resolve; });
        return failSend ? Response.json({ detail: 'Send failed' }, { status: 500 }) : Response.json({ id: 'sent' });
      }
      return Response.json({ items: [] });
    }
    return Response.json(conversations.find(c => path === `conversations/${c.id}`));
  }));
});

async function setup(shell = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SessionProvider>{shell ? <Shell><InboxPage /></Shell> : <InboxPage />}</SessionProvider></QueryClientProvider>);
  fireEvent.click(await screen.findByRole('button', { name: /first/ }));
  return { client, textarea: await screen.findByRole('textbox', { name: 'Ответ клиенту' }) };
}

it('keeps drafts on reselection, switches, polling and delayed session refetch without remounting the editor', async () => {
  const { client, textarea } = await setup();
  fireEvent.change(textarea, { target: { value: 'first draft' } });
  // Reselecting the active row used to unconditionally call setText("").
  fireEvent.click(screen.getByRole('button', { name: /first/ }));
  expect(textarea).toHaveValue('first draft');
  fireEvent.click(screen.getByRole('button', { name: /second/ }));
  fireEvent.change(textarea, { target: { value: 'second draft' } });
  fireEvent.click(screen.getByRole('button', { name: /first/ }));
  expect(textarea).toHaveValue('first draft');
  delaySession = true;
  let refresh: Promise<void>;
  await act(async () => { refresh = client.invalidateQueries(); });
  await waitFor(() => expect(completeSession).toBeDefined());
  expect(screen.getByRole('textbox', { name: 'Ответ клиенту' })).toBe(textarea);
  expect(textarea).toHaveValue('first draft');
  await act(async () => { completeSession!(); await refresh; });
  act(() => { client.setQueryData(['/v1/conversations/first'], { ...conversations[0], mode: 'ai' }); });
  expect(screen.getByRole('textbox', { name: 'Ответ клиенту' })).toBe(textarea);
  expect(textarea).toHaveValue('first draft');
  client.clear();
});

it('preserves newer edits during a slow send and clears only a successfully sent draft', async () => {
  const { client, textarea } = await setup();
  fireEvent.change(textarea, { target: { value: 'sending' } });
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  await waitFor(() => expect(completeSend).toBeDefined());
  fireEvent.change(textarea, { target: { value: 'new draft while sending' } });
  await act(async () => { completeSend!(); });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
  expect(textarea).toHaveValue('new draft while sending');
  completeSend = undefined;
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  await waitFor(() => expect(completeSend).toBeDefined());
  await act(async () => { completeSend!(); });
  await waitFor(() => expect(textarea).toHaveValue(''));
  client.clear();
});

it('retains the draft after a failed send', async () => {
  const { client, textarea } = await setup();
  failSend = true;
  fireEvent.change(textarea, { target: { value: 'retry me' } });
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  await waitFor(() => expect(completeSend).toBeDefined());
  await act(async () => { completeSend!(); });
  expect(textarea).toHaveValue('retry me');
  client.clear();
});

it('keeps the editor mounted when a background session refresh fails with cached permissions', async () => {
  const { client, textarea } = await setup(true);
  fireEvent.change(textarea, { target: { value: 'keep through outage' } });
  failSession = true;
  await act(async () => { await client.invalidateQueries({ queryKey: ['session'] }); });
  expect(screen.getByRole('textbox', { name: 'Ответ клиенту' })).toBe(textarea);
  expect(textarea).toHaveValue('keep through outage');
  client.clear();
});
