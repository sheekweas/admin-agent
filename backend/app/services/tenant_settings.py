"""Per-tenant configurable business rules (ТЗ §15: configuration instead of code forks)."""
from __future__ import annotations

import copy
from typing import Any

from app.models import Tenant

DEFAULT_SETTINGS: dict[str, Any] = {
    "slot_step_min": 15,
    "hold_ttl_min": 3,
    "min_lead_time_min": 30,  # earliest bookable slot from now
    "booking_horizon_days": 60,
    "cancel_deadline_hours": 3,
    "requires_admin_confirmation": False,  # True -> new AI bookings are "pending"
    "reminder_hours": [24, 2],
    "daily_summary_hour": 21,
    "escalation": {
        "repeat_after_min": 5,
        "call_after_min": 10,
        "max_call_attempts": 2,
        "calls_enabled": True,
        "quiet_hours": [22, 8],  # local time window when no calls are placed
        "sla_min": 30,
    },
    "ai": {
        "enabled": True,
        "tone": "neutral",
        "allow_other_staff": True,
        "allow_other_branch": True,
        "forbidden_topics": [],
        "fallback_message_ru": "Спасибо за сообщение! Администратор скоро ответит вам.",
        "fallback_message_kk": "Хабарламаңызға рақмет! Әкімші жақын арада жауап береді.",
        "max_history_messages": 20,
        "voice_replies": False,
    },
    "niche": {
        "medical_safe_mode": False,  # dental: only organisational answers
        "required_fields": ["name"],
    },
    "status_colors": {
        "held": "#a3a3a3",
        "pending": "#f59e0b",
        "confirmed": "#2563eb",
        "checked_in": "#7c3aed",
        "completed": "#16a34a",
        "cancelled": "#ef4444",
        "no_show": "#78350f",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def merged_settings(tenant: Tenant) -> dict[str, Any]:
    return _deep_merge(DEFAULT_SETTINGS, tenant.settings or {})
