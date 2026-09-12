"""Календарная дата колонки-времени в settings.TIMEZONE для группировок по дням.

Голый ``func.date(created_at)`` на ``timestamptz`` PostgreSQL считает в поясе
сессии (у контейнера postgres из compose это UTC, но бывает что угодно), а на
SQLite — по хранимому UTC. Отчёты обязаны считать в настроенной зоне, что бы
ни стояло в сессии (#3136).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.utils.timezone import get_local_timezone


def local_date_expr(column: ColumnElement, db: AsyncSession, tz: ZoneInfo | None = None) -> ColumnElement:
    """SQL-выражение «дата ``column`` в зоне ``tz``» (по умолчанию settings.TIMEZONE).

    PostgreSQL: ``date(timezone('Europe/Moscow', column))`` — через zoneinfo
    сервера, с переводом часов, независимо от ``TimeZone`` сессии.

    SQLite: zoneinfo нет, ближайшее честное — текущее смещение зоны. Для зон
    с переводом часов это точно везде, кроме нескольких часов вокруг самого
    перевода; SQLite у нас только вне docker, на маленьких установках.
    """
    zone = tz or get_local_timezone()
    if _dialect_name(db) == 'postgresql':
        return func.date(func.timezone(_sql_literal(zone.key), column))
    offset_seconds = int((datetime.now(zone).utcoffset() or _ZERO).total_seconds())
    return func.date(column, _sql_literal(f'{offset_seconds:+d} seconds'))


def _sql_literal(value: str) -> ColumnElement:
    """Строка прямо в тексте SQL, а не bind-параметром.

    Выражение строится отдельно для SELECT, GROUP BY и ORDER BY; с параметром
    каждое вхождение получало свой номер ($1, $4, $5), и PostgreSQL отвечал
    «column must appear in the GROUP BY clause» — выражения для него разные.
    Литерал делает их текстуально одинаковыми. Значение — имя зоны из
    настроек (проверено ZoneInfo) или смещение в секундах, не пользовательский ввод.
    """
    return literal(value, literal_execute=True)


def as_date(value: date | datetime | str) -> date:
    """Значение ``local_date_expr`` из строки результата как ``date``.

    PostgreSQL отдаёт ``date``, SQLite — строку ``YYYY-MM-DD``. Потребители
    сравнивают с ``date`` (например, «вчера» в боте), поэтому нормализуем.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _dialect_name(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


_ZERO = datetime.now(ZoneInfo('UTC')).utcoffset()
