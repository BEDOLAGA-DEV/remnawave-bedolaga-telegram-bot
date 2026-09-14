"""Timezone utilities for consistent local time handling."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
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
        logger.warning('⚠️ Не удалось загрузить временную зону, используем UTC', tz_name=tz_name, exc=exc)
        return ZoneInfo('UTC')


def _as_aware_utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def local_date(moment: datetime | None = None, tz: ZoneInfo | None = None) -> date:
    """Календарная дата момента ``moment`` (по умолчанию — сейчас) в зоне ``tz``.

    Зона по умолчанию — ``settings.TIMEZONE``. Это единственное определение
    «сегодня» для отчётов: раньше каждый экран брал дату по UTC, и при
    Europe/Moscow платежи с 00:00 до 02:59 МСК уезжали во «вчера» (#3136).
    """
    zone = tz or get_local_timezone()
    return _as_aware_utc(moment).astimezone(zone).date()


def local_day_start(
    moment: datetime | None = None,
    tz: ZoneInfo | None = None,
    *,
    days_back: int = 0,
) -> datetime:
    """Полночь локального дня, к которому относится ``moment``, как момент в UTC.

    ``days_back`` отсчитывает календарные дни назад (не «минус 24 часа»):
    через перевод часов длина дня 23 или 25 часов, и вычитание ``timedelta``
    из UTC-момента давало бы сдвиг на час.
    """
    zone = tz or get_local_timezone()
    day = local_date(moment, zone) - timedelta(days=days_back)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def local_day_bounds(moment: datetime | None = None, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """Полуинтервал ``[начало, конец)`` локального дня в UTC: ``created_at >= start`` и ``< end``."""
    zone = tz or get_local_timezone()
    day = local_date(moment, zone)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def local_month_start(moment: datetime | None = None, tz: ZoneInfo | None = None) -> datetime:
    """Полночь первого числа локального месяца, к которому относится ``moment``, как момент в UTC."""
    zone = tz or get_local_timezone()
    first_day = local_date(moment, zone).replace(day=1)
    return datetime.combine(first_day, time.min, tzinfo=zone).astimezone(UTC)


def next_local_wall_clock(
    times: Iterable[time],
    reference: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> datetime:
    """Ближайший момент (в UTC), когда часы в зоне ``tz`` покажут одно из ``times``.

    Для расписаний из настроек (синхронизация с панелью, суточная проверка
    трафика): HH:MM там — локальное время оператора, как и BACKUP_TIME после
    #3030. Раньше часы подставлялись в UTC-«сейчас», и при Europe/Moscow
    запуск уезжал на три часа. Через перевод часов локальный час сохраняется.
    """
    zone = tz or get_local_timezone()
    slots = sorted({slot.replace(tzinfo=None) for slot in times})
    if not slots:
        raise ValueError('расписание пустое')
    moment = _as_aware_utc(reference)
    today = moment.astimezone(zone).date()
    for day in (today, today + timedelta(days=1)):
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=zone).astimezone(UTC)
            if candidate > moment:
                return candidate
    raise RuntimeError('расписание не дало ни одного момента за два дня')  # pragma: no cover


def panel_datetime_to_utc(dt: datetime) -> datetime:
    """Normalize a RemnaWave panel datetime to aware UTC.

    The panel always returns time in UTC (ISO with a trailing ``Z`` / ``+00:00``),
    matching how the bot pushes ``subscription.end_date`` (aware UTC) to the panel.
    So we simply ensure the value is aware UTC — naive values are assumed UTC,
    aware values are converted to UTC.

    NOTE: an earlier version wrongly assumed the panel returned panel-local time
    mislabeled as UTC and re-stamped the wall-clock with ``get_local_timezone()``.
    That shifted every cabinet sync by the local offset (e.g. -3h for Europe/Moscow),
    corrupting subscription end dates. This mirrors
    ``remnawave_service._parse_remnawave_date`` ("Панель RemnaWave всегда отдаёт
    время в UTC"), keeping the whole system consistent on UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
