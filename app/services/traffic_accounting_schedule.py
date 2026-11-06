"""Calendar dates for managed base grants, matching Remnawave reset anchors.

Reference: remnawave/backend src/common/utils/get-next-traffic-reset-at.util.ts.
The caller must provide the panel calendar timezone; the bot's display timezone
is not evidence of the panel's timezone. This module does not switch strategies.
"""

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.traffic_accounting import AccountingConflict
from app.utils.timezone import local_day_start


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def next_base_refresh(
    strategy: str,
    *,
    now: datetime,
    anchor: datetime,
    timezone: ZoneInfo,
) -> datetime | None:
    """Next boundary strictly after now, without drifting a month-end anchor."""
    if now.tzinfo is None or anchor.tzinfo is None:
        raise AccountingConflict('Calendar timestamps must be timezone-aware')
    local_now, local_anchor = now.astimezone(timezone), anchor.astimezone(timezone)
    midnight = local_day_start(now, tz=timezone).astimezone(timezone)
    if strategy == 'NO_RESET':
        return None
    if strategy == 'DAY':
        candidate = midnight + timedelta(minutes=5)
        if candidate <= local_now:
            candidate += timedelta(days=1)
    elif strategy == 'WEEK':
        candidate = midnight + timedelta(days=(-midnight.weekday()) % 7, minutes=15)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif strategy in {'MONTH', 'MONTH_ROLLING'}:
        day = local_anchor.day if strategy == 'MONTH_ROLLING' else 1
        minute = 10 if strategy == 'MONTH_ROLLING' else 20

        def in_month(year: int, month: int) -> datetime:
            return datetime(year, month, min(day, monthrange(year, month)[1]), 0, minute, tzinfo=timezone)

        candidate = in_month(local_now.year, local_now.month)
        if candidate <= local_now:
            candidate = in_month(*_month_after(local_now.year, local_now.month))
        if strategy == 'MONTH_ROLLING':
            first = in_month(*_month_after(local_anchor.year, local_anchor.month))
            candidate = max(first, candidate)
    else:
        raise AccountingConflict('Unknown traffic reset strategy')
    return candidate.astimezone(UTC)
