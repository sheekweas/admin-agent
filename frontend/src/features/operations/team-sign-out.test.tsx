import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { TeamSignOut } from './team-sign-out';
let manage = true;
vi.mock('@/lib/session',()=>({useSession:()=>({can:(permission:string)=>manage && permission==='team.manage'})}));
beforeEach(()=>{
  manage=true;
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value(){this.setAttribute('open','');}});
  Object.defineProperty(HTMLDialogElement.prototype,'close',{configurable:true,value(){this.removeAttribute('open');}});
});
afterEach(()=>vi.unstubAllGlobals());
it('requires confirmation and reports the revoked count',async()=>{
  const fetchMock=vi.fn(async (_url:string)=>Response.json({revoked:3}));vi.stubGlobal('fetch',fetchMock);
  const toast=vi.fn();window.addEventListener('crm:toast',toast);
  render(<TeamSignOut membershipId="membership" name="Айдана"/>);
  fireEvent.click(screen.getByRole('button',{name:'Выйти за пользователя'}));
  const dialog=screen.getByRole('dialog',{name:'Завершить сеансы пользователя?'});
  expect(within(dialog).getByText(/Айдана/)).toBeInTheDocument();expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button',{name:'Подтвердить'}));
  await vi.waitFor(()=>expect(toast).toHaveBeenCalled());
  expect(fetchMock.mock.calls[0][0]).toContain('/v1/team/membership/sign-out');
  expect((toast.mock.calls[0][0] as CustomEvent).detail.message).toBe('Завершено сеансов: 3');
  window.removeEventListener('crm:toast',toast);
});
it('hides sign-out without team.manage or a membership id',()=>{
  manage=false;const {rerender}=render(<TeamSignOut membershipId="membership" name="User"/>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();manage=true;rerender(<TeamSignOut name="User"/>);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

