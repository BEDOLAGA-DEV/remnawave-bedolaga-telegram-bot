"""Timezone utilities for consistent local time handling."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_local_timezone() -> ZoneInfo:
    """Return the configured local timezone.

    Falls back to UTC if the configured timezone cannot be loaded. The
    fallback is logged once and cached for subsequent calls.
    """

    tz_name = settings.TIMEZONE

    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - defensive branch
        logger.warning("⚠️ Не удалось загрузить временную зону '': . Используем UTC.", tz_name=tz_name, exc=exc)
        return ZoneInfo('UTC')


def panel_datetime_to_utc(dt: datetime) -> datetime:
    """Convert a panel datetime to aware UTC.

    Panel API returns local time with a misleading UTC offset (+00:00 / Z).
    This strips the offset, interprets the raw value as panel-local time,
    then converts to aware UTC for database storage.
    """
    naive = dt
    localized = naive.replace(tzinfo=get_local_timezone())
    return localized.astimezone(ZoneInfo('UTC'))


def to_local_datetime(dt: datetime | None) -> datetime | None:
    """Convert a datetime value to the configured local timezone."""

    if dt is None:
        return None

    aware_dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware_dt.astimezone(get_local_timezone())


def format_local_datetime(
    dt: datetime | None,
    fmt: str = '%Y-%m-%d %H:%M:%S %Z',
    na_placeholder: str = 'N/A',
) -> str:
    """Format a datetime value in the configured local timezone."""

    localized = to_local_datetime(dt)
    if localized is None:
        return na_placeholder
    return localized.strftime(fmt)


def format_email_datetime(
    dt: datetime | str | None,
    *,
    fmt: str | None = None,
    na_placeholder: str = '',
) -> str:
    """Format a datetime for email-template substitution.

    Replaces the historical ``str(datetime)`` calls in
    ``notification_delivery_service`` which leaked raw ISO with
    microseconds and offset (``2026-05-20 07:32:13.837000+00:00``)
    straight into the rendered email body.

    Resolution order for the format string:

      1. Explicit ``fmt`` argument (when caller wants a specific shape,
         e.g. compact date-only for a subject line).
      2. ``settings.EMAIL_DATE_FORMAT`` (admin-overridable via .env or
         system_settings UI).
      3. ``%d.%m.%Y, %H:%M`` as a locale-independent fallback that
         renders identically on every locale ('20.05.2026, 10:32').

    The input is accepted as ``datetime``, an ISO string (parsed best-
    effort), or ``None`` (returns ``na_placeholder``). Strings already
    in a non-ISO shape pass through unchanged — that lets callers
    pre-format and trust the helper to leave it alone.

    Localization to the configured ``settings.TIMEZONE`` happens
    automatically so users see local time, not UTC.
    """
    if dt is None or dt == '':
        return na_placeholder

    chosen_fmt = fmt or _resolve_email_date_format()

    if isinstance(dt, datetime):
        return format_local_datetime(dt, fmt=chosen_fmt, na_placeholder=na_placeholder)

    if isinstance(dt, str):
        # Best-effort ISO parse — if it fails, return the string as-is
        # (caller pre-formatted, don't fight them).
        try:
            parsed = datetime.fromisoformat(dt)
        except ValueError:
            return dt
        return format_local_datetime(parsed, fmt=chosen_fmt, na_placeholder=na_placeholder)

    return na_placeholder


def _resolve_email_date_format() -> str:
    """Read ``settings.EMAIL_DATE_FORMAT`` with a safe fallback.

    Kept private so callers don't pass the resolved string around —
    every email-formatting site should go through
    ``format_email_datetime`` and let resolution happen there. That
    way an admin who updates ``EMAIL_DATE_FORMAT`` via system_settings
    sees the new format immediately on next notification, without a
    bot restart.
    """
    value = getattr(settings, 'EMAIL_DATE_FORMAT', None)
    if not isinstance(value, str) or not value.strip():
        return '%d.%m.%Y, %H:%M'
    return value
