"""Группировка по дням на настоящем PostgreSQL не зависит от часового пояса сессии (#3136).

``date(created_at)`` на ``timestamptz`` PostgreSQL считает в поясе сессии
(у контейнера postgres из compose это UTC, а бывает и что угодно). Отчёты
обязаны считать в settings.TIMEZONE, что бы ни стояло в сессии, и уважать
переход на летнее время — через ZoneInfo, а не через фиксированное смещение.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from app.database.crud.transaction import REAL_PAYMENT_METHODS, get_revenue_by_period
from app.database.local_date import as_date, local_date_expr
from app.database.models import Transaction, TransactionType, User
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = [User.__table__, Transaction.__table__]
GATEWAY = REAL_PAYMENT_METHODS[0]


async def _seed(db, moments: list[tuple[int, datetime]]) -> None:
    user = User(telegram_id=1, username='payer', first_name='Payer', language='ru')
    db.add(user)
    await db.flush()
    db.add_all(
        [
            Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount_kopeks=amount,
                payment_method=GATEWAY,
                is_completed=True,
                created_at=at,
            )
            for amount, at in moments
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_day_buckets_ignore_session_timezone(postgres_database, monkeypatch, reset_local_timezone_cache):
    tz = use_timezone(monkeypatch, 'Europe/Moscow')
    today_local = datetime.now(tz).date()
    midnight_utc = datetime.combine(today_local, time.min, tzinfo=tz).astimezone(UTC)

    async with postgres_session(postgres_database, TABLES) as db:
        await db.execute(text("SET TIME ZONE 'America/New_York'"))
        await _seed(
            db,
            [
                (100, midnight_utc + timedelta(minutes=30)),  # 00:30 МСК сегодня
                (200, midnight_utc - timedelta(minutes=30)),  # 23:30 МСК вчера
                (300, datetime.now(UTC)),
            ],
        )

        rows = await get_revenue_by_period(db, days=2)

    assert [(row['date'], row['amount_kopeks']) for row in rows] == [
        (today_local - timedelta(days=1), 200),
        (today_local, 100 + 300),
    ]


@pytest.mark.asyncio
async def test_local_date_expr_respects_dst_transitions(postgres_database, monkeypatch, reset_local_timezone_cache):
    berlin = ZoneInfo('Europe/Berlin')

    async with postgres_session(postgres_database, TABLES) as db:
        await _seed(
            db,
            [
                (1, datetime(2026, 3, 28, 22, 30, tzinfo=UTC)),  # 23:30 CET 28.03
                (2, datetime(2026, 3, 29, 0, 30, tzinfo=UTC)),  # 01:30 CET 29.03, до перевода часов
                (3, datetime(2026, 10, 24, 22, 30, tzinfo=UTC)),  # 00:30 CEST 25.10
                (4, datetime(2026, 10, 25, 23, 30, tzinfo=UTC)),  # 00:30 CET 26.10, после перевода
            ],
        )

        result = await db.execute(
            select(local_date_expr(Transaction.created_at, db, tz=berlin)).order_by(Transaction.created_at)
        )
        days = [as_date(value) for value in result.scalars()]

    assert days == [date(2026, 3, 28), date(2026, 3, 29), date(2026, 10, 25), date(2026, 10, 26)]


@pytest.mark.asyncio
async def test_separate_expressions_group_together(postgres_database, monkeypatch, reset_local_timezone_cache):
    """Прод 2026-09-12: имя зоны уходило bind-параметром, каждое вхождение — своим ($1, $4, $5),
    и PostgreSQL отвечал «column must appear in the GROUP BY clause». Выражение в SELECT и в
    GROUP BY строятся отдельными вызовами, как в статистике продаж, — и обязаны совпадать."""
    use_timezone(monkeypatch, 'Europe/Moscow')

    async with postgres_session(postgres_database, TABLES) as db:
        await _seed(db, [(1, datetime(2026, 9, 11, 22, 30, tzinfo=UTC)), (2, datetime(2026, 9, 11, 10, 0, tzinfo=UTC))])

        result = await db.execute(
            select(
                local_date_expr(Transaction.created_at, db).label('date'),
                func.count(Transaction.id).label('count'),
            )
            .group_by(local_date_expr(Transaction.created_at, db))
            .order_by(local_date_expr(Transaction.created_at, db))
        )
        rows = [(as_date(row.date), row.count) for row in result]

    assert rows == [(date(2026, 9, 11), 1), (date(2026, 9, 12), 1)]
