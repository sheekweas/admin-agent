import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {SessionProvider,useSession} from './session';
const mocks=vi.hoisted(()=>({api:vi.fn(),replace:vi.fn()}));
vi.mock('./api',()=>({api:mocks.api}));
vi.mock('next/navigation',()=>({usePathname:()=>'/profile/security',useRouter:()=>({replace:mocks.replace})}));
afterEach(()=>vi.clearAllMocks());
function Probe(){const {user,logout}=useSession();return <><span>{user?.full_name}</span><button onClick={()=>void logout()}>logout</button></>;}
it('revokes the server session before clearing credentials, even if revocation fails',async()=>{
 localStorage.setItem('crm:token','valid');localStorage.setItem('crm:tenant','tenant');localStorage.setItem('crm:tenants','[]');
 let fail:(error:Error)=>void=()=>{};
 mocks.api.mockImplementation((path:string)=>path==='/v1/auth/me'?Promise.resolve({full_name:'Test User',permissions:[]}):path==='/v1/branches'?Promise.resolve({items:[]}):new Promise((_resolve,reject)=>{fail=reject;}));
 render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><SessionProvider><Probe/></SessionProvider></QueryClientProvider>);
 await screen.findByText('Test User');fireEvent.click(screen.getByRole('button',{name:'logout'}));
 expect(mocks.api).toHaveBeenCalledWith('/v1/auth/logout',{method:'POST',silent:true,handleUnauthorized:false});expect(localStorage.getItem('crm:token')).toBe('valid');
 fail(new Error('offline'));await waitFor(()=>expect(localStorage.getItem('crm:token')).toBeNull());expect(localStorage.getItem('crm:tenant')).toBeNull();expect(localStorage.getItem('crm:tenants')).toBeNull();expect(mocks.replace).toHaveBeenCalledWith('/login');
});
