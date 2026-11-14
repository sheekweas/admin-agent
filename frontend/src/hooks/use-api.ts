'use client';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';
import {api,notify} from '@/lib/api';
import {useI18n} from '@/lib/i18n';
export function useApi<T>(path:string|null, interval?:number) {return useQuery<T>({queryKey:[path],queryFn:()=>api<T>(path!),enabled:!!path,refetchInterval:interval,retry:(count,error)=>count<1 && !('status' in error && [401,403].includes(Number(error.status)))});}
export function useAction(){const client=useQueryClient();const [pending,setPending]=useState(false);const {t}=useI18n();async function run<T>(path:string,body?:unknown,method='POST'):Promise<T>{setPending(true);try{const result=await api<T>(path,{method,body:body===undefined?undefined:JSON.stringify(body)});await client.invalidateQueries();notify(t('Действие выполнено'));return result;}finally{setPending(false);}}return {run,pending};}
