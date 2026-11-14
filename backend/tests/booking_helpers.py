"""Time helpers for booking/schedule tests (World branch is Asia/Almaty, shifts 10:00-21:00, break 14-15)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ALMATY = ZoneInfo("Asia/Almaty")


def future_day(days: int = 3) -> date:
    """A local date safely beyond min_lead_time and inside the booking horizon."""
    return datetime.now(ALMATY).date() + timedelta(days=days)


def at(day: date, hh: int, mm: int = 0) -> datetime:
    """Local Almaty wall-clock time as tz-aware UTC."""
    return datetime.combine(day, time(hh, mm), tzinfo=ALMATY).astimezone(UTC)


def local_hhmm(moment: datetime) -> str:
    return moment.astimezone(ALMATY).strftime("%H:%M")
