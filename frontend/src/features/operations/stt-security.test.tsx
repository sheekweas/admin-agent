import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SttCard } from './stt-card';
import { TeamSignOut } from './team-sign-out';
import { Toast } from '@/components/ui';

let allowed = true;
vi.mock('@/lib/session', () => ({useSession:()=>({can:()=>allowed})}));
beforeEach(() => {
  allowed = true;
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {configurable:true,value(){this.setAttribute('open','');}});
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {configurable:true,value(){this.removeAttribute('open');}});
});
afterEach(()=>vi.unstubAllGlobals());
function setup(child: React.ReactNode) { const client = new QueryClient({defaultOptions:{queries:{retry:false}}}); render(<QueryClientProvider client={client}>{child}<Toast/></QueryClientProvider>); return client; }

it('shows provider/model/configuration and reports a route-mocked STT test result', async () => {
  const fetch = vi.fn(async (_url:string, options?:RequestInit)=>Response.json(options?.method==='POST'?{ok:true,provider:'mock',latency_ms:12,error:null}:{provider:'mock',configured:true,model:'whisper-1'}));
  vi.stubGlobal('fetch',fetch);
  const client = setup(<SttCard/>);
  expect(await screen.findByText('whisper-1')).toBeInTheDocument();
  expect(screen.getByText('Настроено')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Проверить распознавание'}));
  expect(await screen.findByRole('status')).toHaveTextContent('Распознавание работает · mock · 12 мс');
  expect(fetch.mock.calls.some(([url,options])=>url.endsWith('/v1/integrations/stt/test')&&options?.method==='POST')).toBe(true);
  client.clear();
});

it('limits testing and team sign-out to the required permissions', async () => {
  allowed = false;
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({provider:'none',configured:false,model:null})));
  const client=setup(<><SttCard/><TeamSignOut membershipId="member" name="Alice"/></>);
  expect(await screen.findByText('Не настроено')).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Проверить распознавание'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Выйти за пользователя'})).not.toBeInTheDocument();
  client.clear();
});

it('only signs out after confirmation and shows the revoked count', async () => {
  const fetch=vi.fn(async()=>Response.json({revoked:3}));vi.stubGlobal('fetch',fetch);
  const client=setup(<TeamSignOut membershipId="membership-1" name="Alice"/>);
  fireEvent.click(screen.getByRole('button',{name:'Выйти за пользователя'}));
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'Подтвердить'}));
  await waitFor(()=>expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/v1/team/membership-1/sign-out'),expect.objectContaining({method:'POST'})));
  expect(await screen.findByRole('status')).toHaveTextContent('Завершено сеансов: 3');
  client.clear();
});
