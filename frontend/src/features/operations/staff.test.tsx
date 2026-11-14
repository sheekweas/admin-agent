import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import StaffPage from './staff';

vi.mock('@/lib/session', () => ({
  useSession: () => ({ user: { role: 'staff', staff_id: 'own-barber' }, can: () => false, timezone: 'Asia/Almaty' }),
}));

it('loads the barber profile even when accumulated staff push it beyond the first directory page', async () => {
  const own = { id: 'own-barber', branch_id: 'branch', name: 'Арман', specialization: '', color: '#0d9488', active: true, service_ids: [] };
  const fetch = vi.fn(async (url: string) => {
    const path = new URL(url).pathname;
    const data = path === '/v1/staff/own-barber' ? own
      : path === '/v1/staff' ? { items: Array.from({ length: 50 }, (_, i) => ({ ...own, id: `other-${i}`, name: `E2E barber ${i}` })), total: 51 }
      : { items: [], total: 0 };
    return new Response(JSON.stringify(data), { status: 200 });
  });
  vi.stubGlobal('fetch', fetch);
  try {
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><StaffPage /></QueryClientProvider>);
    expect(await screen.findByRole('button', { name: 'Арман' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Мой график' })).toBeVisible();
    expect(fetch.mock.calls.some(([url]) => new URL(url).pathname === '/v1/staff')).toBe(false);
  } finally {
    vi.unstubAllGlobals();
  }
});
