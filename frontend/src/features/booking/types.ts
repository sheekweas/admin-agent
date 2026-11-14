import type {ContactRef, ContactOut, LeadOut} from '@/lib/api-schema';
export interface List<T> { items: T[]; total: number }
export type { Service, Staff, Slot, Booking } from '@/lib/types';
export type Contact = ContactRef & Partial<Omit<ContactOut,keyof ContactRef>>;
export interface TimelineEntry { id?: string; ref_id?: string; title?: string; payload?: Record<string,unknown>; kind?: string; type?: string; action?: string; text?: string; description?: string; created_at?: string; at?: string; amount?: string|null; status?: string }
export type Lead = LeadOut;
export const statuses = ['held', 'pending', 'confirmed', 'checked_in', 'completed', 'cancelled', 'no_show', 'rescheduled'];
export const statusLabels: Record<string, [string,string]> = {held:['Удержание','Ұстап тұру'],pending:['Ожидает подтверждения','Растауды күтуде'],confirmed:['Бронь подтверждена','Бронь расталды'],checked_in:['Клиент пришёл','Клиент келді'],completed:['Завершена','Аяқталды'],cancelled:['Отменена','Бас тартылды'],no_show:['Неявка','Келмеді'],rescheduled:['Перенесена','Ауыстырылды']};
export function qs(values: Record<string,string|number|undefined>) { const result = new URLSearchParams(); Object.entries(values).forEach(([key,value]) => { if (value !== undefined && value !== '') result.set(key,String(value)); }); return result.toString(); }

export const bookingTransitions: Record<string,string[]> = {held:['confirmed','cancelled'],pending:['confirmed','cancelled'],confirmed:['checked_in','completed','no_show','cancelled'],checked_in:['completed']};
export function canReschedule(status:string) { return ['held','pending','confirmed'].includes(status); }
