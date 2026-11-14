'use client';
import {useState,type ReactNode} from 'react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {I18nProvider} from '@/lib/i18n';
import {SessionProvider} from '@/lib/session';
import {Toast} from '@/components/ui';
export default function Providers({children}:{children:ReactNode}){const [client]=useState(()=>new QueryClient({defaultOptions:{queries:{staleTime:15000,refetchOnWindowFocus:false}}}));return <QueryClientProvider client={client}><I18nProvider><SessionProvider>{children}<Toast/></SessionProvider></I18nProvider></QueryClientProvider>;}
