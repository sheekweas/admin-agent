'use client';
import Link from 'next/link';
import { Badge, Drawer } from '@/components/ui';
import { DataState } from '@/features/communication/shared';
import { useApi } from '@/hooks/use-api';
import { useSession } from '@/lib/session';
import { formatDate } from '@/lib/time';
import { useCallLabels, useVoiceMessages } from './messages';
import { callDuration, type CallDetail } from './model';
import { CallTranscript } from './transcript';
export function CallDetailDrawer({ id, onClose, initial }: { id: string; onClose: () => void; initial?: CallDetail | null }) {
  const m = useVoiceMessages(); const { purposeLabel, statusLabel } = useCallLabels(); const { can, timezone } = useSession();
  const query = useApi<CallDetail>(id && can('conversations.read') ? `/v1/calls/${encodeURIComponent(id)}` : null, 5000);
  const tenant = useApi<{ settings?: { voice?: { low_confidence_threshold?: number } } }>(id && can('conversations.read') ? '/v1/tenant' : null);
  const call = query.data ?? (initial?.id === id ? initial : null);
  const configuredThreshold = tenant.data?.settings?.voice?.low_confidence_threshold;
  const threshold = configuredThreshold == null ? 70 : configuredThreshold <= 1 ? configuredThreshold * 100 : configuredThreshold;
  return <Drawer open={!!id} onClose={onClose} title={m('details')}><DataState loading={query.isLoading && !call} error={query.error} retry={query.refetch}>{call && <div className="space-y-6"><div className="flex flex-wrap gap-2"><Badge>{statusLabel(call.status)}</Badge><Badge>{call.direction === 'in' ? m('inbound') : m('outbound')}</Badge><Badge>{purposeLabel(call.purpose)}</Badge>{call.transferred_to_human && <Badge variant="warning">{m('transferred')}</Badge>}</div><dl className="grid grid-cols-2 gap-4 text-sm">{[[m('from'), call.from_number], [m('to'), call.to_number], [m('started'), call.started_at ? formatDate(call.started_at, timezone) : '—'], [m('ended'), call.ended_at ? formatDate(call.ended_at, timezone) : '—'], [m('duration'), callDuration(call.duration_sec)]].map(([key, value]) => <div key={key}><dt className="muted">{key}</dt><dd className="mt-1 break-words font-medium">{value}</dd></div>)}</dl><div className="flex flex-wrap gap-3 text-sm text-[var(--accent)]">{call.contact && can('contacts.read') && <Link className="underline" href={`/clients/${call.contact.id}`}>{call.contact.name || call.contact.phone_e164}</Link>}{call.booking_id && can('bookings.read') && <Link className="underline" href={`/calendar?booking_id=${encodeURIComponent(call.booking_id)}`}>{m('booking')}</Link>}{call.admin_task_id && can('tasks.read') && <Link className="underline" href={`/tasks?task_id=${encodeURIComponent(call.admin_task_id)}`}>{m('task')}</Link>}</div><p className="muted text-sm">{call.recording_available ? m('recording') : m('noRecording')}</p><section><h3 className="mb-3 font-semibold">{m('metrics')}</h3><dl className="grid grid-cols-2 gap-3 text-sm">{[[m('firstResponse'), call.metrics.first_response_ms ?? '—'], [m('turns'), call.metrics.turns], [m('retries'), call.metrics.recognition_retries], [m('transferred'), call.metrics.transferred_to_human ? m('yes') : m('no')], [m('bookingCreated'), call.metrics.booking_created ? m('yes') : m('no')]].map(([key, value]) => <div key={key}><dt className="muted">{key}</dt><dd className="font-semibold">{value}</dd></div>)}</dl></section><section><h3 className="mb-4 font-semibold">{m('transcript')}</h3><CallTranscript entries={call.transcript} lowConfidenceThreshold={threshold} /></section></div>}</DataState></Drawer>;
}
