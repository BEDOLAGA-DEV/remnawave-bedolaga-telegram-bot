"""Кабинет: «Доход за сегодня» на дашборде и «за сегодня» в последних платежах (#3136).

Репорт: бот и кабинет показывали разные суммы за день. Бот случайно попадал в
московские сутки (asyncpg подставлял локальную полночь контейнера), кабинет
считал по UTC двумя разными способами. Теперь у всех один источник —
календарный день settings.TIMEZONE.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_stats
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import Transaction, TransactionType, User
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.sqlite_memory import memory_session


ADMIN = SimpleNamespace(id=1, username='admin')
TABLES = [User.__table__, Transaction.__table__]
GATEWAY = REAL_PAYMENT_METHODS[0]


async def _seed_moscow_day(db, monkeypatch):
    tz = use_timezone(monkeypatch, 'Europe/Moscow')
    today_local = datetime.now(tz).date()
    midnight_utc = datetime.combine(today_local, time.min, tzinfo=tz).astimezone(UTC)

    user = User(telegram_id=1, username='payer', first_name='Payer', language='ru')
    db.add(user)
    await db.flush()

    def payment(amount: int, at: datetime):
        return Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=amount,
            payment_method=GATEWAY,
            is_completed=True,
            created_at=at,
        )

    db.add_all(
        [
            payment(100, midnight_utc + timedelta(minutes=30)),  # 00:30 МСК сегодня — до полуночи UTC
            payment(200, midnight_utc - timedelta(minutes=30)),  # 23:30 МСК вчера
            payment(300, datetime.now(UTC)),
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_recent_payments_today_total_uses_local_day(monkeypatch, reset_local_timezone_cache):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_moscow_day(db, monkeypatch)

        response = await admin_stats.get_recent_payments(limit=50, admin=ADMIN, db=db)

    assert response.total_today_kopeks == 100 + 300


@pytest.mark.asyncio
async def test_dashboard_income_today_comes_from_statistics_not_from_chart(monkeypatch):
    """Дашборд не пересчитывает «сегодня» из графика по строке даты UTC."""
    empty_nodes = admin_stats.NodesOverview(total=0, online=0, offline=0, disabled=0, total_users_online=0, nodes=[])
    monkeypatch.setattr(admin_stats, '_get_nodes_overview', AsyncMock(return_value=empty_nodes))
    monkeypatch.setattr(admin_stats, 'get_subscriptions_statistics', AsyncMock(return_value={}))
    monkeypatch.setattr(admin_stats, 'get_server_statistics', AsyncMock(return_value={}))
    monkeypatch.setattr(admin_stats, '_get_tariff_stats', AsyncMock(return_value=None))
    monkeypatch.setattr(
        admin_stats,
        'get_transactions_statistics',
        AsyncMock(return_value={'totals': {}, 'today': {'income_kopeks': 777, 'transactions_count': 1}}),
    )
    monkeypatch.setattr(
        admin_stats,
        'get_revenue_by_period',
        AsyncMock(return_value=[{'date': datetime.now(UTC).date(), 'amount_kopeks': 999}]),
    )

    response = await admin_stats.get_dashboard_stats(admin=ADMIN, db=None)

    assert response.financial.income_today_kopeks == 777
