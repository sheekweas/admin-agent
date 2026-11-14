import { formatInTimeZone, fromZonedTime } from 'date-fns-tz';
import { ru, kk } from 'date-fns/locale';
export const DEFAULT_TIMEZONE='Asia/Almaty';
export function formatDate(value:string|Date|number, timezone=DEFAULT_TIMEZONE, pattern='dd.MM.yyyy HH:mm') {try{return formatInTimeZone(value,timezone,pattern,{locale:typeof window!=='undefined'&&localStorage.getItem('crm:lang')==='kk'?kk:ru});}catch{return '—';}}
export function branchDate(timezone=DEFAULT_TIMEZONE) {return formatDate(new Date(),timezone,'yyyy-MM-dd');}
export function toUTC(value:string,timezone=DEFAULT_TIMEZONE) {return fromZonedTime(value,timezone).toISOString();}
export function money(value:string|number|null|undefined) {return new Intl.NumberFormat('ru-KZ',{style:'currency',currency:'KZT',minimumFractionDigits:0,maximumFractionDigits:2}).format(Number(value||0));}
