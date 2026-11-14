import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from '@/lib/api';
import { CallClientButton } from './outbound';
import { knownVoiceErrorMessage, voiceErrorMessage } from './errors';

const session = vi.hoisted(() => ({ can: (permission: string) => ['conversations.write', 'contacts.read'].includes(permission) }));
vi.mock('@/lib/session', () => ({ useSession: () => session }));

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); } });
});

function renderButton(consent: boolean) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CallClientButton contact={{ id: 'client', name: 'Алия', phone_e164: '+77010000000', consent_flags: { outbound_calls: consent } }} bookingId="booking" /></QueryClientProvider>);
}

describe('outbound voice errors', () => {
  it.each(['CONSENT_REQUIRED', 'OUTSIDE_HOURS', 'DAILY_CAP_REACHED', 'BOOKING_REQUIRED', 'BOOKING_CONTACT_MISMATCH', 'NO_PHONE', 'VOICE_NOT_CONFIGURED'])('maps %s into Russian and Kazakh messages', code => {
    const error = new ApiError(409, code, 'Server message');
    expect(voiceErrorMessage(error, 'ru')).toBe(knownVoiceErrorMessage(code, 'ru'));
    expect(voiceErrorMessage(error, 'kk')).toBe(knownVoiceErrorMessage(code, 'kk'));
    expect(voiceErrorMessage(error, 'kk')).not.toBe(voiceErrorMessage(error, 'ru'));
  });

  it('preserves an unknown server message', () => {
    expect(voiceErrorMessage(new ApiError(503, 'PROVIDER_UNAVAILABLE', 'Provider unavailable'))).toBe('Provider unavailable');
  });

  it('disables calls without consent and exposes an accessible explanation', () => {
    renderButton(false);
    expect(screen.getByRole('button', { name: 'Позвонить клиенту' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Позвонить клиенту' })).toHaveAccessibleDescription('Нет согласия на исходящие звонки');
  });

  it('hides outbound controls without conversations.write permission', () => {
    const previous = session.can;
    session.can = () => false;
    try {
      renderButton(true);
      expect(screen.queryByRole('button', { name: 'Позвонить клиенту' })).not.toBeInTheDocument();
    } finally {
      session.can = previous;
    }
  });

  it('shows the returned call only after the server accepts the request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'new-call' }), { status: 201 })));
    try {
      renderButton(true);
      fireEvent.click(screen.getByRole('button', { name: 'Позвонить клиенту' }));
      expect(screen.queryByRole('link', { name: 'Открыть звонок' })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Подтвердить звонок' }));
      await waitFor(() => expect(screen.getByRole('link', { name: 'Открыть звонок' })).toHaveAttribute('href', '/calls?call_id=new-call'));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('shows a provider failure returned with HTTP 201 without claiming the call is queued', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 'failed-call', status: 'failed' }), { status: 201 })));
    try {
      renderButton(true);
      fireEvent.click(screen.getByRole('button', { name: 'Позвонить клиенту' }));
      fireEvent.click(screen.getByRole('button', { name: 'Подтвердить звонок' }));
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Провайдер не смог начать звонок'));
      expect(screen.queryByText('Звонок поставлен в очередь')).not.toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Открыть звонок' })).toHaveAttribute('href', '/calls?call_id=failed-call');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('requires confirmation, sends purpose and booking, and displays a server-side consent rejection', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'CONSENT_REQUIRED', message: 'Consent required' } }), { status: 409 }));
    vi.stubGlobal('fetch', fetch);
    try {
      renderButton(true);
      fireEvent.click(screen.getByRole('button', { name: 'Позвонить клиенту' }));
      expect(fetch).not.toHaveBeenCalled();
      fireEvent.change(screen.getByRole('combobox', { name: 'Цель звонка' }), { target: { value: 'reminder' } });
      fireEvent.click(screen.getByRole('button', { name: 'Подтвердить звонок' }));
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Клиент не дал согласие'));
      expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ contact_id: 'client', purpose: 'reminder', booking_id: 'booking' });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('normalizes string-form backend errors and localizes the API message before the toast', async () => {
    localStorage.setItem('crm:lang', 'kk');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'OUTSIDE_HOURS' }), { status: 409 })));
    try {
      await expect(api('/v1/calls/outbound', { method: 'POST' })).rejects.toMatchObject({ code: 'OUTSIDE_HOURS', message: knownVoiceErrorMessage('OUTSIDE_HOURS', 'kk') });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
