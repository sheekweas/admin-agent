import {render,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import Login from './page';
const mocks=vi.hoisted(()=>({notify:vi.fn(),replace:vi.fn()}));
vi.mock('@/lib/api',()=>({api:vi.fn(),notify:mocks.notify}));
vi.mock('next/navigation',()=>({useRouter:()=>({replace:mocks.replace})}));
afterEach(()=>{sessionStorage.clear();vi.clearAllMocks();});
it('consumes the expired-session notice once across repeated login mounts',async()=>{
 sessionStorage.setItem('crm:session-ended','1');
 const page=render(<Login/>);await waitFor(()=>expect(mocks.notify).toHaveBeenCalledWith('Сессия завершена — войдите снова','error'));expect(sessionStorage.getItem('crm:session-ended')).toBeNull();page.unmount();render(<Login/>);await new Promise(resolve=>setTimeout(resolve,10));expect(mocks.notify).toHaveBeenCalledTimes(1);
});
it('uses the saved Kazakh language for the expired-session notice',async()=>{
 localStorage.setItem('crm:lang','kk');sessionStorage.setItem('crm:session-ended','1');render(<Login/>);await waitFor(()=>expect(mocks.notify).toHaveBeenCalledWith('Сессия аяқталды — қайта кіріңіз','error'));
});
