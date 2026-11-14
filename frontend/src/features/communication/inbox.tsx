"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Bot, Check, CheckCheck, CircleAlert, MessageSquare, Plus, Send, StickyNote, UserRound, WandSparkles } from "lucide-react";
import { Badge, Button, Card, ConfirmButton, Dialog, Input, Select } from "@/components/ui";
import { useApi, useAction } from "@/hooks/use-api";
import { useI18n } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { formatDate } from "@/lib/time";
import { Collection, DataState, items, Person } from "./shared";

type Conversation = { id: string; contact: Person; channel: string; mode: string; status: string; language: string; last_message: { text: string; author: string; at: string } | null; unread_count: number; open_task_id: string | null };
type Message = { id: string; direction: string; author: string; message_type: string; text: string; media_url: string | null; provider_status: string; is_internal_note: boolean; received_at: string };

export default function InboxPage() {
  const { t } = useI18n();
  const { can, timezone } = useSession();
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState("");
  const [unread, setUnread] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [selectedConversation, setSelectedConversation] = useState<Conversation | null>(null);
  useEffect(() => { setSelectedId(new URLSearchParams(window.location.search).get("conversation_id") ?? ""); }, []);
  const [text, setText] = useState("");
  const [note, setNote] = useState(false);
  const [confirmReply, setConfirmReply] = useState(false);
  const [simulate, setSimulate] = useState(false);
  const [simPhone, setSimPhone] = useState("");
  const [simName, setSimName] = useState("");
  const [simText, setSimText] = useState("");
  const conversations = useApi<Collection<Conversation>>(can("conversations.read") ? `/v1/conversations?${new URLSearchParams({ ...(search ? { q: search } : {}), ...(mode ? { mode } : {}), limit: "200" })}` : null, 5000);
  const allConversations = items(conversations.data);
  const list = allConversations.filter(c => !unread || c.unread_count > 0);
  const detail = useApi<Conversation>(selectedId && can("conversations.read") ? `/v1/conversations/${selectedId}` : null);
  const selected = detail.data ?? allConversations.find(c => c.id === selectedId) ?? (selectedConversation?.id === selectedId ? selectedConversation : null);
  const messages = useApi<Message[] | Collection<Message>>(selectedId && can("conversations.read") ? `/v1/conversations/${selectedId}/messages` : null, 5000);
  const action = useAction();
  const endRef = useRef<HTMLDivElement>(null);
  const messageList = items(messages.data);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [selectedId, messageList.length]);
  async function selectConversation(conversation: Conversation) {
    setSelectedId(conversation.id);
    setSelectedConversation(conversation);
    setText("");
    if (conversation.unread_count && can("conversations.write")) {
      try { await action.run(`/v1/conversations/${conversation.id}/read`); await conversations.refetch(); } catch { /* useAction reports API errors */ }
    }
  }
  async function sendMessage() {
    if (!text.trim() || !selectedId) return;
    try {
      await action.run(`/v1/conversations/${selectedId}/messages`, { text: text.trim(), internal_note: note });
      if (selected && !note) setSelectedConversation({ ...selected, mode: "human" });
      setConfirmReply(false);
      setText(""); await Promise.all([messages.refetch(), conversations.refetch()]);
    } catch { /* useAction reports API errors */ }
  }
  async function changeMode(target: "takeover" | "release") {
    const result = await action.run<Partial<Conversation> | undefined>(`/v1/conversations/${selectedId}/${target}`);
    if (selected) setSelectedConversation({ ...selected, ...result, mode: result?.mode ?? (target === "takeover" ? "human" : "ai") });
    await conversations.refetch();
  }
  const modeLabel = (value: string) => value === "human" ? t("Человек отвечает", "Адам жауап беруде") : value === "waiting_admin" ? t("Ждём админа", "Әкімшіні күтудеміз") : t("AI отвечает", "AI жауап беруде");
  const deliveryLabel = (value: string) => ({ received: t("Получено", "Қабылданды"), queued: t("В очереди", "Кезекте"), sent: t("Отправлено", "Жіберілді"), delivered: t("Доставлено", "Жеткізілді"), read: t("Прочитано", "Оқылды"), failed: t("Ошибка доставки", "Жеткізу қатесі") })[value] ?? t("Статус неизвестен", "Күйі белгісіз");
  const safeMediaUrl = (url?: string | null) => { try { const parsed = new URL(url ?? ""); return ["https:", "http:"].includes(parsed.protocol) ? parsed.href : null; } catch { return null; } };
  if (detail.error) return <DataState loading={false} error={detail.error} retry={detail.refetch}><></></DataState>;
  return <div className="space-y-6">
    <div className="page-header"><div><p className="muted text-sm">{t("Коммуникации", "Байланыс")}</p><h1>{t("Входящие", "Кіріс хабарламалар")}</h1><p className="muted">{t("Каждый диалог под контролем. Обновление каждые 5 секунд.", "Әр диалог бақылауда. Әр 5 секунд сайын жаңарады.")}</p></div>{can("conversations.write") && <Button onClick={() => setSimulate(true)}><WandSparkles size={16} />{t("Симулировать WhatsApp", "WhatsApp хабарын сынау")}</Button>}</div>
    <div className="grid gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
      <Card className={`!p-0 ${selected ? "hidden lg:block" : ""}`}><div className="space-y-3 border-b p-4"><Input aria-label={t("Поиск диалогов", "Диалогтарды іздеу")} placeholder={t("Имя или телефон", "Аты немесе телефоны")} value={search} onChange={e => setSearch(e.target.value)} /><Select aria-label={t("Режим диалога", "Диалог режимі")} value={mode} onChange={e => setMode(e.target.value)}><option value="">{t("Все диалоги", "Барлық диалогтар")}</option><option value="ai">{modeLabel("ai")}</option><option value="human">{modeLabel("human")}</option><option value="waiting_admin">{modeLabel("waiting_admin")}</option></Select><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={unread} onChange={e => setUnread(e.target.checked)} />{t("Только непрочитанные", "Тек оқылмағандар")}</label></div>
        <DataState loading={conversations.isLoading} error={conversations.error} empty={!list.length} retry={conversations.refetch}><div className="max-h-[650px] overflow-y-auto">{list.map(c => <button type="button" key={c.id} disabled={action.pending} aria-pressed={selectedId === c.id} onClick={() => void selectConversation(c)} className={`w-full border-b p-4 text-left transition hover:bg-[var(--accent-soft)] ${selectedId === c.id ? "bg-[var(--accent-soft)]" : ""}`}><div className="flex items-start gap-3"><span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-[var(--accent-soft)] font-semibold text-[var(--accent)]">{c.contact.name?.slice(0, 2).toUpperCase() || <UserRound size={18} />}</span><div className="min-w-0 flex-1"><div className="flex items-center justify-between gap-2"><span className="truncate font-semibold">{c.contact.name || c.contact.phone_e164}</span>{c.unread_count > 0 && <span className="rounded-full bg-[var(--accent)] px-2 text-xs text-white">{c.unread_count}</span>}</div><p className="muted my-1 truncate text-sm">{c.last_message?.text || c.contact.phone_e164}</p><div className="flex items-center justify-between gap-1"><span className={`text-xs ${c.mode === "waiting_admin" ? "text-amber-700 dark:text-amber-300" : "muted"}`}>{modeLabel(c.mode)}</span><span className="muted text-xs">{c.last_message?.at ? formatDate(c.last_message.at, timezone, "HH:mm") : c.channel === "whatsapp" ? "WhatsApp" : c.channel === "telegram" ? "Telegram" : t("Телефония", "Телефония")}</span></div></div></div></button>)}</div></DataState>
      </Card>
      <Card className={`!p-0 ${!selected ? "hidden lg:block" : ""}`}>{!selected ? <div className="flex min-h-[540px] flex-col items-center justify-center gap-4 p-6 text-center"><div className="rounded-2xl bg-[var(--accent-soft)] p-5 text-[var(--accent)]"><MessageSquare size={32} /></div><h2 className="text-xl font-semibold">{t("Начните с диалога", "Диалогты таңдаңыз")}</h2><p className="muted max-w-sm">{t("Откройте переписку, чтобы увидеть сообщения и помочь клиенту.", "Хабарламаларды көру және клиентке көмектесу үшін диалогты ашыңыз.")}</p></div> : <div className="flex min-h-[600px] min-w-0 flex-col">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4"><button type="button" className="inline-flex items-center gap-2 text-sm lg:hidden" onClick={() => { setSelectedId(""); setSelectedConversation(null); }}><ArrowLeft size={16} />{t("К диалогам", "Диалогтарға")}</button><div><Link href={`/clients/${selected.contact.id}`} className="font-semibold hover:text-[var(--accent)]">{selected.contact.name || selected.contact.phone_e164}</Link><p className="muted mt-1 text-xs">{selected.contact.phone_e164} · {selected.channel === "whatsapp" ? "WhatsApp" : selected.channel === "telegram" ? "Telegram" : t("Телефония", "Телефония")} · {(selected.language === "kk" ? "KZ" : "RU")}</p></div><div className="flex flex-wrap items-center gap-2"><Badge>{modeLabel(selected.mode)}</Badge>{can("bookings.write") && <Link href={`/calendar?contact_id=${selected.contact.id}`} className="inline-flex items-center gap-1 rounded-lg border px-3 py-2 text-sm"><Plus size={14} />{t("Записать", "Жазу")}</Link>}{can("conversations.write") && <ConfirmButton title={selected.mode === "human" ? t("Вернуть диалог AI?", "Диалогты AI-ға қайтару керек пе?") : t("Взять диалог на себя?", "Диалогты қабылдау керек пе?")} onConfirm={() => changeMode(selected.mode === "human" ? "release" : "takeover")}>{selected.mode === "human" ? t("Вернуть AI", "AI-ға қайтару") : t("Взять диалог", "Диалогты қабылдау")}</ConfirmButton>}</div></div>
        <div className="max-h-[530px] min-h-[330px] flex-1 space-y-4 overflow-y-auto p-4 sm:p-6" aria-live="polite"><DataState loading={messages.isLoading || detail.isLoading} error={messages.error || detail.error} empty={!messageList.length} retry={messages.refetch}>{messageList.map(m => <div key={m.id} className={`flex ${m.author === "client" ? "justify-start" : m.author === "system" ? "justify-center" : "justify-end"}`}><article className={`max-w-[90%] rounded-2xl border px-4 py-3 sm:max-w-[78%] ${m.is_internal_note ? "border-amber-300 bg-amber-500/10" : m.author === "ai" ? "border-[var(--accent)]/30 bg-[var(--accent-soft)]" : m.author === "human" ? "border-blue-400/30 bg-blue-500/10" : m.author === "system" ? "bg-slate-500/5 text-sm" : "bg-slate-500/5"}`}><div className="muted mb-2 flex items-center gap-2 text-xs">{m.is_internal_note ? <StickyNote size={13} /> : m.author === "ai" ? <Bot size={13} /> : <UserRound size={13} />}{m.is_internal_note ? t("Внутренняя заметка", "Ішкі ескертпе") : m.author === "ai" ? "AI" : m.author === "human" ? t("Администратор", "Әкімші") : m.author === "system" ? t("Система", "Жүйе") : t("Клиент", "Клиент")}</div><p className="whitespace-pre-wrap break-words">{m.text}</p>{safeMediaUrl(m.media_url) && <a href={safeMediaUrl(m.media_url)!} target="_blank" rel="noreferrer" className="mt-2 inline-block text-sm text-[var(--accent)] underline">{t("Открыть вложение", "Тіркемені ашу")}</a>}<div className="muted mt-2 flex items-center justify-end gap-1 text-xs"><time>{formatDate(m.received_at, timezone, "dd.MM HH:mm")}</time>{m.direction === "out" && !m.is_internal_note && (m.provider_status === "failed" ? <span className="flex items-center gap-1 text-red-600 dark:text-red-300"><CircleAlert size={13} />{t("Ошибка доставки", "Жеткізу қатесі")}</span> : m.provider_status === "read" || m.provider_status === "delivered" ? <CheckCheck size={14} aria-label={deliveryLabel(m.provider_status)} /> : <Check size={13} aria-label={deliveryLabel(m.provider_status)} />)}</div></article></div>)}<div ref={endRef} /></DataState></div>
        {selected.open_task_id && <Link href="/tasks" className="mx-4 mb-3 rounded-lg bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">{t("Этот диалог ждёт решения администратора →", "Бұл диалог әкімшінің шешімін күтуде →")}</Link>}
        {can("conversations.write") && <form className="space-y-3 border-t p-4" onSubmit={e => { e.preventDefault(); if (!note && selected.mode !== "human") setConfirmReply(true); else void sendMessage(); }}><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={note} onChange={e => setNote(e.target.checked)} />{t("Внутренняя заметка — клиент не увидит", "Ішкі ескертпе — клиентке көрінбейді")}</label><textarea aria-label={note ? t("Текст заметки", "Ескертпе мәтіні") : t("Ответ клиенту", "Клиентке жауап")} className="min-h-24 w-full rounded-xl border bg-transparent p-3" maxLength={4096} value={text} onChange={e => setText(e.target.value)} placeholder={note ? t("Заметка для команды…", "Топқа арналған ескертпе…") : t("Напишите сообщение…", "Хабарлама жазыңыз…")} required /><div className="flex items-center justify-between gap-3"><span className="muted text-xs">{note ? t("Только для вашей команды", "Тек сіздің тобыңыз үшін") : t("Отправка переключит диалог на человека", "Жіберу диалогты адамға ауыстырады")}</span><Button type="submit" disabled={action.pending || !text.trim()}><Send size={15} />{t("Отправить", "Жіберу")}</Button></div></form>}
      </div>}</Card>
    </div>
    <Dialog open={confirmReply} onClose={() => { if (!action.pending) setConfirmReply(false); }} title={t("Отправить и взять диалог?", "Жіберіп, диалогты қабылдау керек пе?")}><p className="mb-5 muted">{t("После отправки ответа диалог перейдёт в режим человека.", "Жауап жіберілгеннен кейін диалог адам режиміне ауысады.")}</p><div className="flex justify-end gap-3"><Button variant="secondary" disabled={action.pending} onClick={() => setConfirmReply(false)}>{t("Отмена", "Бас тарту")}</Button><Button disabled={action.pending || !text.trim()} onClick={() => void sendMessage()}>{t("Подтвердить отправку", "Жіберуді растау")}</Button></div></Dialog>
    <Dialog open={simulate} onClose={() => setSimulate(false)} title={t("Тестовое входящее сообщение", "Сынақ кіріс хабарламасы")}><form className="space-y-4" onSubmit={async e => { e.preventDefault(); try { await action.run("/v1/conversations/simulate", { phone: simPhone, name: simName, text: simText }); setSimulate(false); setSimText(""); await conversations.refetch(); } catch { /* API error is shown by useAction */ } }}><label className="field">{t("Имя", "Аты")}<Input maxLength={200} value={simName} onChange={e => setSimName(e.target.value)} required /></label><label className="field">{t("Телефон", "Телефон")}<Input type="tel" placeholder="+77001234567" value={simPhone} onChange={e => setSimPhone(e.target.value)} pattern="\+[0-9]{8,15}" required /></label><label className="field">{t("Сообщение", "Хабарлама")}<textarea className="min-h-28 rounded-lg border bg-transparent p-3" maxLength={4096} value={simText} onChange={e => setSimText(e.target.value)} required /></label><Button type="submit" disabled={action.pending}>{t("Отправить в демо-канал", "Демо арнаға жіберу")}</Button></form></Dialog>
  </div>;
}





