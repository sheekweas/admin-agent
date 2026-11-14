export type CallPurpose = 'client_ai' | 'admin_escalation' | 'reminder' | 'callback' | 'booking_confirmation' | 'reschedule';
export type CallStatus = 'queued' | 'ringing' | 'in_progress' | 'answered' | 'completed' | 'no_answer' | 'busy' | 'failed' | 'cancelled';
export type { CallDetail, TranscriptEntry, VoiceReport } from '@/lib/api-schema';
export type { CallOut as VoiceCall } from '@/lib/api-schema';
export const callPurposes: CallPurpose[] = ['client_ai', 'admin_escalation', 'reminder', 'callback', 'booking_confirmation', 'reschedule'];
export const callStatuses: CallStatus[] = ['queued', 'ringing', 'in_progress', 'answered', 'completed', 'no_answer', 'busy', 'failed', 'cancelled'];
export function callDuration(seconds: number | null | undefined) { if (seconds == null) return '—'; const value = Math.max(0, Math.floor(seconds)); return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`; }
