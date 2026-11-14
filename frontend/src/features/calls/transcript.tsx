'use client';
import { Bot, CircleAlert, UserRound } from 'lucide-react';
import { EmptyState } from '@/components/ui';
import { useSession } from '@/lib/session';
import { formatDate } from '@/lib/time';
import { useVoiceMessages } from './messages';
import type { TranscriptEntry } from './model';
export function CallTranscript({ entries, lowConfidenceThreshold = 70 }: { entries: TranscriptEntry[]; lowConfidenceThreshold?: number }) {
  const m = useVoiceMessages(); const { timezone } = useSession();
  if (!entries.length) return <EmptyState title={m('emptyTranscript')} />;
  return <ol aria-label={m('transcript')} className="space-y-4">{entries.map((entry, index) => {
    const confidence = entry.confidence == null ? null : entry.confidence * 100;
    const low = confidence != null && confidence < lowConfidenceThreshold;
    return <li key={`${entry.at}-${index}`} className={`flex ${entry.speaker === 'client' ? 'justify-start' : entry.speaker === 'system' ? 'justify-center' : 'justify-end'}`}><article data-speaker={entry.speaker} data-low-confidence={low || undefined} className={`max-w-[92%] rounded-2xl border p-4 sm:max-w-[85%] ${low ? 'border-amber-500 bg-amber-500/10' : entry.speaker === 'ai' ? 'border-[var(--accent)]/30 bg-[var(--accent-soft)]' : entry.speaker === 'admin' ? 'border-blue-400/40 bg-blue-500/10' : 'bg-slate-500/5'}`}><div className="muted mb-2 flex items-center gap-2 text-xs">{entry.speaker === 'ai' ? <Bot size={14} /> : <UserRound size={14} />}<strong>{entry.speaker === 'ai' ? 'AI' : m(entry.speaker === 'admin' ? 'admin' : entry.speaker === 'system' ? 'system' : 'client')}</strong>{entry.at && <time dateTime={entry.at}>{formatDate(entry.at, timezone, 'dd.MM HH:mm:ss')}</time>}</div><p className="whitespace-pre-wrap break-words">{entry.text}</p>{entry.confidence != null && <p className={`mt-2 flex items-center gap-1 text-xs ${low ? 'text-amber-800 dark:text-amber-200' : 'muted'}`} title={m('confidence')}>{low && <CircleAlert size={13} />}{low ? m('lowConfidence') : m('confidence')}: {confidence == null ? null : Math.round(confidence * 10) / 10}%</p>}</article></li>;
  })}</ol>;
}
