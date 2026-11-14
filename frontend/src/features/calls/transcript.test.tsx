import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CallTranscript } from './transcript';
import { callDuration, type TranscriptEntry } from './model';

const entries: TranscriptEntry[] = [
  { speaker: 'client', text: 'Хочу записаться', at: '2026-11-14T10:00:00+05:00', confidence: 0.45 },
  { speaker: 'ai', text: 'На какое время?', at: '2026-11-14T10:00:01+05:00', confidence: null },
  { speaker: 'admin', text: 'Помогу вам', at: '2026-11-14T10:00:02+05:00', confidence: 0.95 },
  { speaker: 'system', text: 'Соединение установлено', at: '2026-11-14T10:00:03+05:00', confidence: 0 },
];
describe('voice transcript', () => {
  it('renders speakers in order, preserves text, and highlights low recognition confidence including zero', () => {
    render(<CallTranscript entries={entries} />);
    expect(screen.getByRole('list', { name: 'Расшифровка' })).toBeInTheDocument();
    expect(screen.getAllByRole('article').map(article => article.dataset.speaker)).toEqual(['client', 'ai', 'admin', 'system']);
    expect(screen.getByText('Хочу записаться').closest('article')).toHaveAttribute('data-low-confidence', 'true');
    expect(screen.getByText('Соединение установлено').closest('article')).toHaveAttribute('data-low-confidence', 'true');
    expect(screen.getByText('На какое время?').closest('article')).not.toHaveAttribute('data-low-confidence');
    expect(screen.getByText('Уверенность распознавания: 95%')).toBeInTheDocument();
  });
  it('uses the configured threshold and highlights strictly below it', () => {
    render(<CallTranscript entries={[entries[0]]} lowConfidenceThreshold={45} />);
    expect(screen.getByRole('article')).not.toHaveAttribute('data-low-confidence');
    expect(screen.getByText('Уверенность распознавания: 45%')).toBeInTheDocument();
  });
  it('shows an empty transcript without inventing dialogue', () => {
    render(<CallTranscript entries={[]} />);
    expect(screen.getByText('Расшифровка пока отсутствует')).toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });
  it('formats absent, sub-minute, and longer durations', () => {
    expect(callDuration(null)).toBe('—'); expect(callDuration(0)).toBe('0:00'); expect(callDuration(65.8)).toBe('1:05'); expect(callDuration(3601)).toBe('60:01');
  });
});
