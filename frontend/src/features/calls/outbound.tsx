'use client';
import Link from 'next/link';
import { useId, useState } from 'react';
import { Phone } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { api, notify } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import { useSession } from '@/lib/session';
import { useApi } from '@/hooks/use-api';
import { Button, Dialog, Select } from '@/components/ui';
import { voiceErrorMessage } from './errors';
import type { VoiceCall } from './model';

type Contact = { id: string; name: string; phone_e164?: string | null; consent_flags?: Record<string, unknown> };
type Purpose = 'booking_confirmation' | 'reminder' | 'callback' | 'reschedule';

export function CallClientButton({ contact, bookingId, adminTaskId }: { contact: Contact; bookingId?: string; adminTaskId?: string }) {
  const { t, lang } = useI18n();
  const { can } = useSession();
  const client = useQueryClient();
  const hintId = useId();
  const [open, setOpen] = useState(false);
  const [purpose, setPurpose] = useState<Purpose>(bookingId ? 'booking_confirmation' : 'callback');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [callId, setCallId] = useState('');
  const [failed, setFailed] = useState(false);
  const fullContact = useApi<Contact>(can('conversations.write') && can('contacts.read') && contact.consent_flags === undefined ? `/v1/contacts/${contact.id}` : null);
  const details = fullContact.data || contact;
  const consent = details.consent_flags?.outbound_calls === true;
  const hint = fullContact.isFetching ? t('Проверяем согласие клиента…', 'Клиент келісімі тексерілуде…') : fullContact.isError ? t('Не удалось проверить согласие клиента', 'Клиент келісімін тексеру мүмкін болмады') : !consent ? t('Нет согласия на исходящие звонки', 'Шығыс қоңырауларға келісім жоқ') : !details.phone_e164 ? t('У клиента нет номера телефона', 'Клиенттің телефон нөмірі жоқ') : '';
  if (!can('conversations.write')) return null;

  async function submit() {
    if (pending || !consent || !details.phone_e164) return;
    setPending(true);
    setError('');
    try {
      const call = await api<VoiceCall>('/v1/calls/outbound', { method: 'POST', body: JSON.stringify({ contact_id: contact.id, purpose, ...(bookingId ? { booking_id: bookingId } : {}), ...(adminTaskId ? { admin_task_id: adminTaskId } : {}) }) });
      setCallId(call.id);
      setFailed(call.status === 'failed');
      await client.invalidateQueries();
      notify(call.status === 'failed' ? t('Провайдер не смог начать звонок. Подробности доступны в карточке звонка.', 'Провайдер қоңырауды бастай алмады. Толық ақпарат қоңырау карточкасында.') : t('Звонок поставлен в очередь', 'Қоңырау кезекке қойылды'), call.status === 'failed' ? 'error' : 'success');
    } catch (failure) {
      setError(voiceErrorMessage(failure, lang));
    } finally {
      setPending(false);
    }
  }

  return <>
    <span className="inline-flex" title={hint || undefined} tabIndex={hint ? 0 : undefined} aria-describedby={hint ? hintId : undefined}>
      <Button variant="secondary" disabled={!!hint} aria-describedby={hint ? hintId : undefined} onClick={() => { setError(''); setCallId(''); setOpen(true); }}><Phone size={16} aria-hidden="true" />{t('Позвонить клиенту', 'Клиентке қоңырау шалу')}</Button>
      {hint && <span id={hintId} className="sr-only">{hint}</span>}
    </span>
    <Dialog open={open} title={t('Позвонить клиенту?', 'Клиентке қоңырау шаласыз ба?')} onClose={() => { if (!pending) setOpen(false); }}>
      {callId ? <div className="space-y-4"><p role={failed ? 'alert' : 'status'}>{failed ? t('Провайдер не смог начать звонок. Подробности доступны в карточке звонка.', 'Провайдер қоңырауды бастай алмады. Толық ақпарат қоңырау карточкасында.') : t('Звонок поставлен в очередь', 'Қоңырау кезекке қойылды')}</p><Link className="button button-primary" href={`/calls?call_id=${encodeURIComponent(callId)}`} onClick={() => setOpen(false)}>{t('Открыть звонок', 'Қоңырауды ашу')}</Link><Button variant="secondary" onClick={() => setOpen(false)}>{t('Закрыть', 'Жабу')}</Button></div> : <div className="space-y-4">
        <p>{details.name} · {details.phone_e164}</p>
        <label className="field">{t('Цель звонка', 'Қоңырау мақсаты')}<Select value={purpose} disabled={pending} onChange={event => setPurpose(event.target.value as Purpose)}>
          <option value="booking_confirmation">{t('Подтверждение записи', 'Жазылуды растау')}</option>
          <option value="reminder">{t('Напоминание', 'Еске салу')}</option>
          <option value="callback">{t('Обратный звонок', 'Кері қоңырау')}</option>
          <option value="reschedule">{t('Перенос записи', 'Жазылуды ауыстыру')}</option>
        </Select></label>
        <p className="muted">{t('После подтверждения клиенту будет отправлен исходящий звонок.', 'Растағаннан кейін клиентке шығыс қоңырау шалынады.')}</p>
        {error && <p role="alert" className="text-red-600">{error}</p>}
        <div className="flex justify-end gap-2"><Button variant="secondary" disabled={pending} onClick={() => setOpen(false)}>{t('Отмена', 'Бас тарту')}</Button><Button disabled={pending} onClick={() => void submit()}>{pending ? t('Отправка…', 'Жіберілуде…') : t('Подтвердить звонок', 'Қоңырауды растау')}</Button></div>
      </div>}
    </Dialog>
  </>;
}

