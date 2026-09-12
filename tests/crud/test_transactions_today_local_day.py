"""«Доход за сегодня» и доход по дням считаются по календарному дню settings.TIMEZONE (#3136).

Строки ставятся относительно настоящей полуночи Europe/Moscow: A — 00:30 МСК
сегодня (ещё вчера по UTC), B — 23:30 МСК вчера, C — сейчас. Плюс оплата с
баланса и ручное начисление — они в доход не входят.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from app.database.crud.transaction import (
    REAL_PAYMENT_METHODS,
    get_revenue_by_period,
    get_transactions_statistics,
)
from app.database.models import PaymentMethod, Transaction, TransactionType, User
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__, Transaction.__table__]
GATEWAY = REAL_PAYMENT_METHODS[0]


async def _seed(db, monkeypatch):
    tz = use_timezone(monkeypatch, 'Europe/Moscow')
    today_local = datetime.now(tz).date()
    midnight_utc = datetime.combine(today_local, time.min, tzinfo=tz).astimezone(UTC)

    user = User(telegram_id=1, username='payer', first_name='Payer', language='ru')
    db.add(user)
    await db.flush()

    def payment(amount: int, at: datetime, *, method: str = GATEWAY, kind: str = TransactionType.DEPOSIT.value):
        return Transaction(
            user_id=user.id,
            type=kind,
            amount_kopeks=amount,
            payment_method=method,
            is_completed=True,
            created_at=at,
        )

    db.add_all(
        [
            payment(100, midnight_utc + timedelta(minutes=30)),  # A: 00:30 МСК сегодня
            payment(200, midnight_utc - timedelta(minutes=30)),  # B: 23:30 МСК вчера
            payment(300, datetime.now(UTC)),  # C: сейчас
            payment(
                1000,
                datetime.now(UTC),
                method=PaymentMethod.BALANCE.value,
                kind=TransactionType.SUBSCRIPTION_PAYMENT.value,
            ),
            payment(5000, datetime.now(UTC), method=PaymentMethod.MANUAL.value),
        ]
    )
    await db.commit()
    return today_local


@pytest.mark.asyncio
async def test_income_today_starts_at_local_midnight(monkeypatch, reset_local_timezone_cache):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, monkeypatch)

        stats = await get_transactions_statistics(db)

    assert stats['today']['income_kopeks'] == 100 + 300


@pytest.mark.asyncio
async def test_transactions_count_today_starts_at_local_midnight(monkeypatch, reset_local_timezone_cache):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, monkeypatch)

        stats = await get_transactions_statistics(db)

    # Считаются все завершённые операции дня: A, C, оплата с баланса и ручная. B — вчера.
    assert stats['today']['transactions_count'] == 4


@pytest.mark.asyncio
async def test_revenue_by_period_groups_by_local_calendar_day(monkeypatch, reset_local_timezone_cache):
    async with memory_session(monkeypatch, TABLES) as db:
        today_local = await _seed(db, monkeypatch)

        rows = await get_revenue_by_period(db, days=2)

    assert [(row['date'], row['amount_kopeks']) for row in rows] == [
        (today_local - timedelta(days=1), 200),
        (today_local, 100 + 300),
    ]


@pytest.mark.asyncio
async def test_revenue_by_period_for_one_day_is_today_only(monkeypatch, reset_local_timezone_cache):
    async with memory_session(monkeypatch, TABLES) as db:
        today_local = await _seed(db, monkeypatch)

        rows = await get_revenue_by_period(db, days=1)

    assert [(row['date'], row['amount_kopeks']) for row in rows] == [(today_local, 100 + 300)]
