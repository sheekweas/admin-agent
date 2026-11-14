import type { Slot } from './types';
import { knownVoiceErrorMessage } from '@/features/calls/errors';
export const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public alternatives: Slot[] = []) { super(message); this.name = 'ApiError'; }
}
export function notify(message: string, kind: 'success'|'error' = 'success') { if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('crm:toast', {detail:{message,kind}})); }
function headersFor(options: RequestInit) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  if (typeof window !== 'undefined') {
    const token=localStorage.getItem('crm:token'), tenant=localStorage.getItem('crm:tenant');
    if(token) headers.set('Authorization',`Bearer ${token}`);
    if(tenant) headers.set('X-Tenant-Id',tenant);
  }
  return headers;
}
async function request(path:string,options:RequestInit={}) {
  let response:Response;
  try { response=await fetch(`${API_URL}${path}`,{...options,headers:headersFor(options)}); }
  catch { const error=new ApiError(0,'NETWORK_ERROR',typeof window!=='undefined' && localStorage.getItem('crm:lang')==='kk'?'Сервермен байланыс жоқ':'Нет соединения с сервером'); notify(error.message,'error'); throw error; }
  if(!response.ok) {
    const payload=await response.json().catch(()=>({}));
    const detail=payload.detail ?? payload;
    const message=typeof detail==='string'?detail:Array.isArray(detail)?detail.map((e:{msg:string})=>e.msg).join('; '):detail.message || response.statusText;
    const code=detail.code||payload.code||(typeof detail==='string'&&knownVoiceErrorMessage(detail)?detail:'API_ERROR');
    const language=typeof window!=='undefined'&&localStorage.getItem('crm:lang')==='kk'?'kk':'ru';
    const error=new ApiError(response.status,code,knownVoiceErrorMessage(code,language)||message,detail.alternatives||payload.alternatives||[]);
    if(response.status===401 && typeof window!=='undefined') {localStorage.removeItem('crm:token');localStorage.removeItem('crm:tenant');window.dispatchEvent(new Event('crm:unauthorized'));}
    notify(error.message,'error');throw error;
  }
  return response;
}
export async function api<T>(path:string,options:RequestInit={}):Promise<T> { const response=await request(path,options);return response.status===204?undefined as T:await response.json() as T; }
export async function download(path:string,filename:string) {const response=await request(path);const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
