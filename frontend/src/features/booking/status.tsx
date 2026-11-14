'use client';
import { useI18n } from '@/lib/i18n';
import { statusLabels } from './types';
export function BookingStatus({status}: {status: string}) { const {t}=useI18n(); const labels=statusLabels[status]; return <span className={`booking-status booking-status-${status}`}><span aria-hidden="true">●</span> {labels ? t(...labels) : status}</span>; }
