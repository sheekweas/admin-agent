"use client";

import { useState, type ReactNode } from "react";
import { Button, Input, Select, Dialog, Card, Skeleton, EmptyState, Badge } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { ApiError } from "@/lib/api";

export type List<T> = { items: T[]; total: number };
export type Field = { key: string; ru: string; kk: string; type?: string; required?: boolean; options?: { value: string; label: string }[]; min?: number; max?: number; step?: number; minLength?: number; maxLength?: number };
export function DataState({ query, children }: { query: { isPending: boolean; error: Error | null; data?: unknown; refetch: () => unknown }; children: ReactNode }) {
  const { t } = useI18n();
  if (query.isPending) return <Skeleton className="h-48 w-full" />;
  if (query.error instanceof ApiError && query.error.status === 403) return <Card><p role="alert">{t("Нет доступа. Обратитесь к администратору, если вам нужен доступ к этому разделу.", "Қолжетімділік жоқ. Бұл бөлімге кіру қажет болса, әкімшіге хабарласыңыз.")}</p></Card>;
  if (query.error) return <Card><p role="alert">{query.error.message}</p><Button onClick={() => void query.refetch()}>{t("Повторить", "Қайталау")}</Button></Card>;
  return <>{children}</>;
}
export function Editor({ title, fields, initial = {}, onClose, onSave }: { title: string; fields: Field[]; initial?: Record<string, unknown>; onClose: () => void; onSave: (values: Record<string, unknown>) => Promise<unknown> }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [values, setValues] = useState<Record<string, unknown>>(initial);
  const [error, setError] = useState("");
  return <Dialog open onClose={onClose} title={title}><form className="space-y-4" onSubmit={async event => { event.preventDefault(); setBusy(true); setError(""); try { await onSave(Object.fromEntries(fields.map(field => [field.key, values[field.key]]).filter(([, value]) => value !== undefined))); onClose(); } catch (e) { setError(e instanceof Error ? e.message : t("Ошибка", "Қате")); } finally { setBusy(false); } }}>
    {fields.map(field => <label key={field.key} className="field block space-y-1"><span>{t(field.ru, field.kk)}</span>{field.type === "checkbox" ? <input type="checkbox" className="ml-3 h-4 w-4" checked={Boolean(values[field.key])} onChange={event => setValues({ ...values, [field.key]: event.target.checked })} /> : field.type === "multiselect" ? <select multiple className="min-h-24 w-full rounded-xl border bg-transparent p-2" value={Array.isArray(values[field.key]) ? values[field.key] as string[] : []} onChange={event => setValues({ ...values, [field.key]: Array.from(event.target.selectedOptions, o => o.value) })}>{field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select> : field.options ? <Select required={field.required} value={String(values[field.key] ?? "")} onChange={event => setValues({ ...values, [field.key]: event.target.value })}><option value="">{t("Выберите", "Таңдаңыз")}</option>{field.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</Select> : field.type === "textarea" ? <textarea className="min-h-28 w-full rounded-xl border border-[var(--border)] bg-transparent p-3" required={field.required} value={String(values[field.key] ?? "")} onChange={event => setValues({ ...values, [field.key]: event.target.value })} /> : <Input type={field.type ?? "text"} required={field.required} min={field.min} max={field.max} minLength={field.minLength} maxLength={field.maxLength} step={field.type === "number" ? field.step ?? 1 : undefined} value={String(values[field.key] ?? "")} onChange={event => setValues({ ...values, [field.key]: field.type === "number" ? Number(event.target.value) : event.target.value })} />}</label>)}
    {error && <p role="alert" className="text-red-600">{error}</p>}<div className="flex justify-end gap-2"><Button type="button" onClick={onClose}>{t("Закрыть", "Жабу")}</Button><Button type="submit" disabled={busy}>{busy ? t("Сохраняем…", "Сақталуда…") : t("Сохранить", "Сақтау")}</Button></div>
  </form></Dialog>;
}
export function None({ count }: { count: number }) { const { t } = useI18n(); return count ? null : <EmptyState title={t("Пока нет данных", "Әзірге деректер жоқ")} />; }
export function asItems<T>(value?: { items: T[]; total?: number } | T[]): T[] { return Array.isArray(value) ? value : value?.items ?? []; }
export function OperationStatus({status}:{status:string}) { const {t}=useI18n();const labels:Record<string,[string,string,string]>={dead:["Ошибка доставки","Жеткізу қатесі","danger"],partially_refunded:["Частично возвращено","Ішінара қайтарылды","warning"],pending:["Ожидает","Күтуде","warning"],confirmed:["Подтверждён","Расталған","success"],paid:["Оплачено","Төленді","success"],completed:["Завершено","Аяқталды","success"],refunded:["Возвращено","Қайтарылды","warning"],cancelled:["Отменено","Болдырылмады","danger"],approved:["Одобрено","Мақұлданды","success"],rejected:["Отклонено","Қабылданбады","danger"],failed:["Ошибка доставки","Жеткізу қатесі","danger"],sent:["Отправлено","Жіберілді","blue"],delivered:["Доставлено","Жеткізілді","success"],connected:["Подключено","Қосылған","success"],disconnected:["Отключено","Ажыратылған","neutral"],unknown:["Не проверено","Тексерілмеген","neutral"],active:["Активно","Белсенді","success"],error:["Ошибка","Қате","danger"],ok:["Работает","Жұмыс істейді","success"]};const label=labels[status];return <Badge variant={label?.[2]??"neutral"}>{label?t(label[0],label[1]):status}</Badge>;}
