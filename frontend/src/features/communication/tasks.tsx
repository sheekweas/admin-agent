"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BellRing, CheckCircle2, Clock3, PhoneCall } from "lucide-react";
import { Badge, Button, Card, Dialog, Select } from "@/components/ui";
import { useApi, useAction } from "@/hooks/use-api";
import { useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { formatDate } from "@/lib/time";
import { Collection, DataState, items, Person } from "./shared";

type Task = { id: string; number: number; kind: string; question: string; priority: string; status: string; assignee: { id: string; full_name: string } | null; contact: Person | null; conversation_id: string | null; branch_id: string | null; escalation_level: number; next_escalation_at: string | null; sla_due_at: string | null; created_at: string; ack_at: string | null; resolved_at: string | null; answer: string; add_to_knowledge: boolean; payload: Record<string, unknown> };

export default function TasksPage() {
  const { t } = useI18n();
  const { timezone, can } = useSession();
  const [status, setStatus] = useState("open");
  const [selected, setSelected] = useState<Task | null>(null);
  const [answer, setAnswer] = useState("");
  const [relay, setRelay] = useState(true);
  const [knowledge, setKnowledge] = useState(false);
  const [linkedTaskId, setLinkedTaskId] = useState("");
  useEffect(() => { setLinkedTaskId(new URLSearchParams(window.location.search).get("task_id") || ""); }, []);
  const linkedTask = useApi<Task>(linkedTaskId && can("tasks.read") ? `/v1/admin-tasks/${encodeURIComponent(linkedTaskId)}` : null, 5000);
  const listQuery = useApi<Collection<Task>>(can("tasks.read") && !linkedTaskId ? `/v1/admin-tasks?${new URLSearchParams({ ...(status ? { status } : {}), limit: "200" })}` : null, 5000);
  const query = linkedTaskId ? linkedTask : listQuery;
  const action = useAction();
  const tasks = linkedTaskId ? (linkedTask.data ? [linkedTask.data] : []) : items(listQuery.data);
  const priorityLabel = (value: string) => ({ low: t("Низкий", "Төмен"), normal: t("Обычный", "Қалыпты"), high: t("Высокий", "Жоғары"), urgent: t("Срочный", "Шұғыл") })[value] ?? value;
  const statusLabel = (value: string) => ({ open: t("Ожидает ответа", "Жауап күтуде"), acknowledged: t("В работе", "Орындалуда"), acked: t("В работе", "Орындалуда"), resolved: t("Решено", "Шешілді"), cancelled: t("Отменено", "Бас тартылды") })[value] ?? value;
  async function acknowledge(id: string) { try { await action.run(`/v1/admin-tasks/${id}/ack`); await query.refetch(); } catch { /* useAction displays server errors */ } }
  return <div className="space-y-6">
    <div className="page-header"><div><p className="muted text-sm">{t("Контроль сервиса", "Қызметті бақылау")}</p><h1>{t("Задачи администратора", "Әкімші тапсырмалары")}</h1><p className="muted">{t("Помогите AI ответить точно. Взятие задачи останавливает эскалацию.", "AI-ға нақты жауап беруге көмектесіңіз. Тапсырманы қабылдау эскалацияны тоқтатады.")}</p></div><Select aria-label={t("Статус задач", "Тапсырма күйі")} value={status} onChange={e => setStatus(e.target.value)}><option value="open">{t("Открытые", "Ашық")}</option><option value="acked">{t("В работе", "Орындалуда")}</option><option value="resolved">{t("Решённые", "Шешілген")}</option><option value="">{t("Все задачи", "Барлық тапсырмалар")}</option></Select></div>
    <div className="grid gap-4 sm:grid-cols-3">{[{ icon: Clock3, label: t("В текущем списке", "Ағымдағы тізімде"), value: tasks.length }, { icon: BellRing, label: t("Повторное уведомление", "Қайталанған хабарлама"), value: tasks.filter(task => task.escalation_level === 1).length }, { icon: PhoneCall, label: t("Эскалация звонком", "Қоңыраумен эскалация"), value: tasks.filter(task => task.escalation_level >= 2).length }].map(metric => <Card className="!p-0" key={metric.label}><div className="flex items-center justify-between p-5"><div><p className="muted text-sm">{metric.label}</p><p className="mt-2 text-3xl font-semibold">{metric.value}</p></div><metric.icon className="text-[var(--accent)]" size={24} /></div></Card>)}</div>
    <DataState loading={query.isLoading} error={query.error} empty={!tasks.length} retry={query.refetch}><div className="space-y-4">{tasks.map(task => <Card className="!p-0" key={task.id}><article className="space-y-4 p-5"><div className="flex flex-wrap items-center justify-between gap-3"><div className="flex flex-wrap items-center gap-2"><span className="muted text-sm">#{task.number}</span><Badge variant={task.priority === "urgent" ? "danger" : task.priority === "high" ? "warning" : "neutral"}>{priorityLabel(task.priority)}</Badge><Badge>{statusLabel(task.status)}</Badge><span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-xs ${task.escalation_level >= 2 ? "bg-red-500/10 text-red-700 dark:text-red-300" : task.escalation_level === 1 ? "bg-amber-500/10 text-amber-700 dark:text-amber-300" : "bg-[var(--accent-soft)] text-[var(--accent)]"}`}>{task.escalation_level >= 2 ? <PhoneCall size={12} /> : <BellRing size={12} />}{task.escalation_level >= 2 ? t("Звоним", "Қоңырау шалуда") : task.escalation_level === 1 ? t("Повторно", "Қайталанды") : t("Отправлено", "Жіберілді")}</span></div><time className="muted text-xs">{formatDate(task.created_at, timezone)}</time></div><h2 className="text-lg font-semibold">{task.question}</h2>{task.contact && <Link href={`/clients/${task.contact.id}`} className="inline-block text-sm text-[var(--accent)] underline">{task.contact.name} · {task.contact.phone_e164}</Link>}{task.answer && <div className="rounded-xl bg-[var(--accent-soft)] p-4"><p className="mb-1 text-xs font-semibold text-[var(--accent)]">{t("Ответ администратора", "Әкімші жауабы")}</p><p className="whitespace-pre-wrap text-sm">{task.answer}</p></div>}<div className="flex flex-wrap items-center justify-between gap-3"><p className="muted text-xs">{task.ack_at ? `${t("Принято", "Қабылданды")}: ${formatDate(task.ack_at, timezone)}` : t("Подтвердите задачу, чтобы остановить эскалацию", "Эскалацияны тоқтату үшін тапсырманы қабылдаңыз")}</p><div className="flex gap-2">{task.conversation_id && <Link href={`/inbox?conversation_id=${task.conversation_id}`} className="rounded-lg border px-3 py-2 text-sm">{t("Открыть чат", "Чатты ашу")}</Link>}{can("tasks.write") && task.status !== "resolved" && task.status !== "cancelled" && <>{!task.ack_at && <Button disabled={action.pending} onClick={() => void acknowledge(task.id)}>{t("Взять в работу", "Қабылдау")}</Button>}<Button onClick={() => { setSelected(task); setAnswer(task.answer ?? ""); }}><CheckCircle2 size={15} />{t("Решить", "Шешу")}</Button></>}</div></div></article></Card>)}</div></DataState>
    <Dialog open={!!selected} onClose={() => setSelected(null)} title={t("Ответить и решить задачу", "Жауап беру және тапсырманы шешу")}><form className="space-y-4" onSubmit={async e => { e.preventDefault(); if (!selected) return; try { await action.run(`/v1/admin-tasks/${selected.id}/resolve`, { answer: answer.trim(), relay_to_client: relay, add_to_knowledge: knowledge }); setSelected(null); await query.refetch(); } catch { /* API error is shown by useAction */ } }}><p className="rounded-xl bg-slate-500/5 p-3 text-sm">{selected?.question}</p><label className="field">{t("Проверенный ответ", "Тексерілген жауап")}<textarea className="min-h-36 rounded-xl border bg-transparent p-3" maxLength={4000} value={answer} onChange={e => setAnswer(e.target.value)} required /></label><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={relay} onChange={e => setRelay(e.target.checked)} />{t("Передать ответ клиенту", "Жауапты клиентке жіберу")}</label><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={knowledge} onChange={e => setKnowledge(e.target.checked)} />{t("Добавить в знания как черновик", "Білім қорына жоба ретінде қосу")}</label><Button type="submit" disabled={action.pending || !answer.trim()}>{t("Подтвердить решение", "Шешімді растау")}</Button></form></Dialog>
  </div>;
}




