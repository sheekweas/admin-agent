'use client';
import Link from 'next/link';
import { useApi } from '@/hooks/use-api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { formatDate } from '@/lib/time';
import { Badge, Card, QueryState } from '@/components/ui';
import type { Lead, List } from './types';
export function ClientLeads({contactId}:{contactId:string}) {
 const {t}=useI18n();const {timezone}=useSession();
 const query=useApi<List<Lead>>(`/v1/leads?contact_id=${encodeURIComponent(contactId)}&limit=200`);
 const leads=query.data?.items.filter(lead=>lead.contact?.id===contactId)||[];
 const names:Record<string,[string,string]>={new:['Новое обращение','Жаңа өтініш'],info_requested:['Запрос информации','Ақпарат сұрауы'],qualified:['Квалифицирован','Іріктелген'],booking_pending:['Выбирает время','Уақыт таңдауда'],booked:['Записан','Жазылды'],visited:['Посетил','Келді'],lost:['Потерян','Жоғалған'],cancelled:['Отменён','Бас тартқан'],repeat:['Повторное обращение','Қайталама өтініш']};
 return <QueryState query={query}><div className="space-y-3">{leads.map(lead=><Card key={lead.id}><div className="flex justify-between gap-3"><strong>{names[lead.stage]?t(...names[lead.stage]):lead.stage}</strong><Badge>{lead.source}</Badge></div>{lead.created_at&&<p className="muted text-sm mt-2">{formatDate(lead.created_at,timezone)}</p>}{lead.lost_reason&&<p>{lead.lost_reason}</p>}</Card>)}{!leads.length&&<Card><p className="muted">{t('Обращений пока нет','Өтініштер жоқ')}</p></Card>}{(query.data?.total||0)>200&&<p className="muted text-sm">{t('Показаны обращения из последних 200 лидов. Полная воронка доступна в разделе лидов.','Соңғы 200 лидтің өтініштері көрсетілген. Толық кезеңдер лидтер бөлімінде.')} <Link href="/leads">{t('Открыть лиды','Лидтерді ашу')} →</Link></p>}</div></QueryState>;
}
