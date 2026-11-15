import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { SttCard } from './stt-card';

let manage = true;
vi.mock('@/lib/session', () => ({useSession:()=>({can:(permission:string)=>manage && permission==='integrations.write'})}));
afterEach(()=>vi.unstubAllGlobals());
function setup(){const client=new QueryClient({defaultOptions:{queries:{retry:false}}});render(<QueryClientProvider client={client}><SttCard/></QueryClientProvider>);return client;}
it('loads STT settings and reports mocked test results', async()=>{
  manage=true;
  const fetchMock=vi.fn(async (url:string)=>Response.json(url.endsWith('/test')?{ok:true,provider:'mock',latency_ms:12,error:null}:{provider:'mock',configured:true,model:'whisper-1'}));
  vi.stubGlobal('fetch',fetchMock);
  const client=setup();
  expect(await screen.findByText('whisper-1')).toBeInTheDocument();
  expect(screen.getByText('Настроено')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Проверить распознавание'}));
  expect(await screen.findByRole('status')).toHaveTextContent('Распознавание работает');
  expect(fetchMock.mock.calls.some(call=>call[0].endsWith('/v1/integrations/stt/test'))).toBe(true);
  client.clear();
});
it('hides the test action without integrations.write',async()=>{
  manage=false;vi.stubGlobal('fetch',vi.fn(async()=>Response.json({provider:'none',configured:false,model:null})));
  const client=setup();await screen.findByText('Не настроено');
  expect(screen.queryByRole('button',{name:'Проверить распознавание'})).not.toBeInTheDocument();client.clear();
});
it('shows test failure without claiming recognition works',async()=>{
  manage=true;vi.stubGlobal('fetch',vi.fn(async(url:string)=>Response.json(url.endsWith('/test')?{ok:false,provider:'mock',latency_ms:3,error:'timeout'}:{provider:'mock',configured:true,model:null})));
  const client=setup();fireEvent.click(await screen.findByRole('button',{name:'Проверить распознавание'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Ошибка распознавания');client.clear();
});
