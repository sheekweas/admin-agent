import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import Security from './security';
import {ApiError} from '@/lib/api';
const mocks=vi.hoisted(()=>({api:vi.fn(),notify:vi.fn(),clearSession:vi.fn()}));
vi.mock('@/lib/api',async importOriginal=>({...await importOriginal<typeof import('@/lib/api')>(),api:mocks.api,notify:mocks.notify}));
vi.mock('@/lib/session',()=>({useSession:()=>({timezone:'Asia/Almaty',clearSession:mocks.clearSession})}));
vi.mock('@/hooks/use-api',()=>({useApi:()=>({data:{items:[],total:0},isPending:false,isError:false,error:null,refetch:vi.fn()})}));
afterEach(()=>vi.clearAllMocks());
function form(){render(<QueryClientProvider client={new QueryClient()}><Security/></QueryClientProvider>);fireEvent.change(screen.getByLabelText('Текущий пароль'),{target:{value:'old-password'}});}
function passwords(password:string,confirmation=password){fireEvent.change(screen.getByLabelText('Новый пароль'),{target:{value:password}});fireEvent.change(screen.getByLabelText('Подтвердите новый пароль'),{target:{value:confirmation}});fireEvent.submit(screen.getByRole('button',{name:'Изменить пароль'}).closest('form')!);}
it('rejects weak and mismatched passwords before calling the API',()=>{form();passwords('1234567890');expect(screen.getByRole('alert')).toHaveTextContent('10 символов');passwords('strong-password','different-password');expect(screen.getByRole('alert')).toHaveTextContent('Пароли не совпадают');expect(mocks.api).not.toHaveBeenCalled();});
it.each(['WEAK_PASSWORD','INVALID_CURRENT_PASSWORD'])('translates server password code %s',async code=>{mocks.api.mockRejectedValueOnce(new ApiError(400,code,code));form();passwords('strong-password');expect(await screen.findByRole('alert')).toHaveTextContent(code==='WEAK_PASSWORD'?'10 символов':'Текущий пароль неверен');});
it('shows revoked count, clears password fields and retains current session',async()=>{mocks.api.mockResolvedValueOnce({revoked_other_sessions:3});form();passwords('strong-password');await waitFor(()=>expect(mocks.notify).toHaveBeenCalledWith('Пароль изменён. Завершено других сессий: 3'));expect(screen.getByLabelText('Новый пароль')).toHaveValue('');expect(mocks.clearSession).not.toHaveBeenCalled();expect(mocks.api).toHaveBeenCalledWith('/v1/auth/change-password',expect.objectContaining({body:JSON.stringify({current_password:'old-password',new_password:'strong-password'})}));});
