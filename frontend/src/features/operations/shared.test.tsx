import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { DataState, Editor } from './shared';

describe('operations API boundaries', () => {
  it('shows friendly denied access without retrying a forbidden request', () => {
    render(<DataState query={{ isPending: false, error: new ApiError(403, 'FORBIDDEN', 'Missing permission'), refetch: vi.fn() }}><p>Private content</p></DataState>);
    expect(screen.getByRole('alert')).toHaveTextContent('Нет доступа');
    expect(screen.queryByText('Private content')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('submits editable fields without leaking response metadata or hidden settings', async () => {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
    Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); } });
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onClose = vi.fn();
    render(<Editor title="Edit" fields={[{ key: 'name', ru: 'Name', kk: 'Name', required: true }]} initial={{ name: 'Barber KZ', id: 'server-id', escalation: { calls_enabled: true } }} onSave={onSave} onClose={onClose} />);
    fireEvent.submit(screen.getByRole('textbox').closest('form')!);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ name: 'Barber KZ' }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
