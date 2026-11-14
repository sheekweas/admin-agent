'use client';
import { Card } from '@/components/ui';
import { useApi } from '@/hooks/use-api';
import { useSession } from '@/lib/session';
import { DataState } from '@/features/communication/shared';
import { callDuration, type VoiceReport } from './model';
import { useVoiceMessages } from './messages';
export function VoiceKpis({ dateFrom, dateTo, compact = false }: { dateFrom?: string; dateTo?: string; compact?: boolean }) {
  const { can } = useSession(); const m = useVoiceMessages();
  const params = new URLSearchParams({ ...(dateFrom ? { date_from: dateFrom } : {}), ...(dateTo ? { date_to: dateTo } : {}) });
  const query = useApi<VoiceReport>(can('reports.read') ? `/v1/reports/voice?${params}` : null, 15000);
  if (!can('reports.read')) return null;
  const report = query.data;
  const values = report ? [ [m('calls'), report.calls], [m('answered'), report.answered], [m('avgDuration'), callDuration(report.avg_duration_sec)], [m('avgResponse'), Math.round(report.avg_first_response_ms)], [m('transferRate'), `${report.transfer_rate}%`], [m('bookingRate'), `${report.booking_rate}%`], [m('retryRate'), `${report.recognition_retry_rate}%`] ] : [];
  return <section aria-label={m('voice')}><DataState loading={query.isLoading} error={query.error} retry={query.refetch}><div className={`grid grid-cols-2 gap-3 ${compact ? 'xl:grid-cols-4' : 'xl:grid-cols-7'}`}>{values.map(([label, value]) => <Card key={String(label)} className="!p-4"><p className="muted text-xs">{label}</p><p className="mt-2 text-2xl font-semibold tabular-nums">{value}</p></Card>)}</div></DataState></section>;
}
