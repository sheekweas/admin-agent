import {afterEach,describe,expect,it,vi} from 'vitest';
import {api,ApiError} from './api';
afterEach(()=>{vi.unstubAllGlobals();sessionStorage.clear();});
describe('typed API client',()=>{
 it('attaches the token and tenant to the request',async()=>{localStorage.setItem('crm:token','token');localStorage.setItem('crm:tenant','tenant');const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({id:'1'})));vi.stubGlobal('fetch',fetch);expect(await api('/v1/contacts')).toEqual({id:'1'});const headers=fetch.mock.calls[0][1].headers as Headers;expect(headers.get('Authorization')).toBe('Bearer token');expect(headers.get('X-Tenant-Id')).toBe('tenant');});
 it('preserves slot conflict message and alternatives',async()=>{const slots=[{slot_id:'next',start_at:'2026-11-14T10:00:00Z'}];vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:{code:'SLOT_UNAVAILABLE',message:'Slot occupied',alternatives:slots}}),{status:409})));await expect(api('/v1/bookings',{method:'POST'})).rejects.toMatchObject({status:409,code:'SLOT_UNAVAILABLE',message:'Slot occupied',alternatives:slots});});
 it('clears expired credentials and broadcasts unauthorized',async()=>{localStorage.setItem('crm:token','expired');const listener=vi.fn();window.addEventListener('crm:unauthorized',listener);vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:'Session expired'}),{status:401})));await expect(api('/v1/auth/me')).rejects.toBeInstanceOf(ApiError);expect(localStorage.getItem('crm:token')).toBeNull();expect(listener).toHaveBeenCalledOnce();window.removeEventListener('crm:unauthorized',listener);});
 it('normalizes FastAPI validation errors and handles no-content responses',async()=>{const fetch=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({detail:[{msg:'Phone required'}]}),{status:422})).mockResolvedValueOnce(new Response(null,{status:204}));vi.stubGlobal('fetch',fetch);await expect(api('/v1/contacts')).rejects.toThrow('Phone required');expect(await api('/v1/services/1',{method:'DELETE'})).toBeUndefined();});
 it('preserves password error codes returned as detail strings',async()=>{vi.stubGlobal('fetch',vi.fn().mockResolvedValue(Response.json({detail:'INVALID_CURRENT_PASSWORD'},{status:400})));await expect(api('/v1/auth/change-password',{method:'POST',silent:true})).rejects.toMatchObject({status:400,code:'INVALID_CURRENT_PASSWORD'});});
 it('broadcasts concurrent unauthorized responses only once without raw error toasts',async()=>{
  localStorage.setItem('crm:token','expired');localStorage.setItem('crm:tenants','[]');
  const unauthorized=vi.fn(),toast=vi.fn();window.addEventListener('crm:unauthorized',unauthorized);window.addEventListener('crm:toast',toast);
  vi.stubGlobal('fetch',vi.fn(async()=>Response.json({detail:'Expired'},{status:401})));
  try{await Promise.allSettled([api('/v1/auth/me'),api('/v1/branches')]);expect(unauthorized).toHaveBeenCalledOnce();expect(toast).not.toHaveBeenCalled();expect(sessionStorage.getItem('crm:session-ended')).toBe('1');expect(localStorage.getItem('crm:tenants')).toBeNull();}
  finally{window.removeEventListener('crm:unauthorized',unauthorized);window.removeEventListener('crm:toast',toast);}
 });
 it('keeps login failures separate from session expiration',async()=>{
  localStorage.setItem('crm:token','existing');vi.stubGlobal('fetch',vi.fn().mockResolvedValue(Response.json({detail:'Invalid credentials'},{status:401})));
  await expect(api('/v1/auth/login',{method:'POST',silent:true})).rejects.toMatchObject({status:401});expect(localStorage.getItem('crm:token')).toBe('existing');expect(sessionStorage.getItem('crm:session-ended')).toBeNull();
 });
 it('allows best-effort logout without dispatching expiration or toast events',async()=>{
  localStorage.setItem('crm:token','current');const toast=vi.fn();window.addEventListener('crm:toast',toast);const fetch=vi.fn().mockRejectedValue(new Error('Offline'));vi.stubGlobal('fetch',fetch);
  try{await expect(api('/v1/auth/logout',{method:'POST',silent:true,handleUnauthorized:false})).rejects.toMatchObject({code:'NETWORK_ERROR'});expect(toast).not.toHaveBeenCalled();expect(fetch.mock.calls[0][1]).not.toHaveProperty('silent');expect(fetch.mock.calls[0][1]).not.toHaveProperty('handleUnauthorized');}
  finally{window.removeEventListener('crm:toast',toast);}
 });
});
