import type {LoginOut, MeOut, TenantOption, BranchOut, ContactOut, ServiceOut, StaffOut, SlotOut, BookingOut} from './api-schema';
export type ID = string;
export interface Page<T> { items: T[]; total: number }
export type TenantChoice = TenantOption;
export type LoginResult = LoginOut;
export type User = MeOut;
export type Branch = BranchOut;
export type Contact = ContactOut;
export type Service = ServiceOut;
export type Staff = StaffOut;
export type Slot = SlotOut;
export type BookingStatus = 'held'|'pending'|'confirmed'|'checked_in'|'completed'|'cancelled'|'no_show'|'rescheduled';
export type Booking = BookingOut;
export interface Report { date_from:string; date_to:string; currency:string; kpi: Record<string,number|string>; revenue_by_day: {date:string;amount:string}[]; bookings_by_day: {date:string;bookings:number;visits:number}[]; top_services:{service_id:string;title:string;revenue:string;count:number}[];top_staff:{staff_id:string;name:string;revenue:string;visits:number;utilization:number}[];lead_sources:{source:string;count:number}[];funnel:{stage:string;count:number}[];load_by_hour:{hour:number;bookings:number}[];cancellations_by_day:{date:string;cancelled:number;no_show:number}[] }


export interface AuthSession { id:string; created_at:string; last_seen_at:string; expires_at:string; ip:string|null; user_agent:string|null; current:boolean }
export interface RevokedSessions { revoked:number }
export interface PasswordChangeResult { revoked_other_sessions:number }
import type { MessageOut } from './api-schema';

export interface VoiceMessageMeta {
  transcribed?: boolean;
  stt?: string;
  stt_confidence?: number | null;
  stt_language?: 'ru' | 'kk' | null;
  stt_error?: string | null;
  stt_low_confidence?: boolean;
}
export type ConversationMessage = MessageOut & { meta?: VoiceMessageMeta | null };
export interface STTIntegration { provider: string; configured: boolean; model: string | null }
export interface STTTestResult { ok: boolean; provider: string; latency_ms: number; error: string | null }
