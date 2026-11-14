'use client';
import {Badge} from './index';
import {useI18n} from '@/lib/i18n';
const statuses:Record<string,[string,string,string]>={ai:['AI отвечает','AI жауап беруде','accent'],human:['человек отвечает','Адам жауап беруде','blue'],waiting_admin:['ждём админа','Әкімшіні күтеміз','warning'],failed:['ошибка доставки','Жеткізу қатесі','danger'],confirmed:['бронь подтверждена','Бронь расталды','success'],checked_in:['Прибыл','Келді','blue'],completed:['Завершена','Аяқталды','success'],cancelled:['Отменена','Бас тартылды','danger'],no_show:['Неявка','Келмеді','danger'],hold:['Ожидает','Күтуде','warning'],pending:['Ожидает','Күтуде','warning'],pending_confirmation:['Ожидает подтверждения','Растауды күтуде','warning'],expired:['Истекла','Мерзімі өтті','neutral']};
statuses.held=statuses.hold;
statuses.rescheduled=['Перенесена','Ауыстырылды','blue'];
export function StatusBadge({status}:{status:string}){const {t}=useI18n();const entry=statuses[status];return <Badge variant={entry?.[2]||'neutral'}><span className="status-dot"/>{entry?t(entry[0],entry[1]):status}</Badge>;}
