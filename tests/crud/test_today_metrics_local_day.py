"""«Новых сегодня» и «куплено сегодня» в сводках — по календарному дню settings.TIMEZONE (#3136).

Тот же класс дефекта, что и «доход за сегодня»: регистрация или покупка в
00:30 МСК попадала во «вчера», потому что день начинался в 00:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.crud.user import get_users_statistics
from app.database.models import (
    Subscription,
    SubscriptionConversion,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from tests.fixtures.local_day import reset_local_timezone_cache, use_timezone  # noqa: F401
from tests.fixtures.sqlite_memory import memory_session


def _moscow_moments(monkeypatch) -> tuple[datetime, datetime]:
    """(00:30 МСК сегодня, 23:30 МСК вчера) как моменты UTC."""
    tz = use_timezone(monkeypatch, 'Europe/Moscow')
    midnight_utc = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(UTC)
    return midnight_utc + timedelta(minutes=30), midnight_utc - timedelta(minutes=30)


@pytest.mark.asyncio
async def test_new_users_today_counts_from_local_midnight(monkeypatch, reset_local_timezone_cache):
    early_today, late_yesterday = _moscow_moments(monkeypatch)

    async with memory_session(monkeypatch, [User.__table__]) as db:
        db.add_all(
            [
                User(telegram_id=1, language='ru', status=UserStatus.ACTIVE.value, created_at=early_today),
                User(telegram_id=2, language='ru', status=UserStatus.ACTIVE.value, created_at=late_yesterday),
                User(telegram_id=3, language='ru', status=UserStatus.ACTIVE.value, created_at=datetime.now(UTC)),
            ]
        )
        await db.commit()

        stats = await get_users_statistics(db)

    assert stats['new_today'] == 2


@pytest.mark.asyncio
async def test_subscriptions_purchased_today_counts_from_local_midnight(monkeypatch, reset_local_timezone_cache):
    early_today, late_yesterday = _moscow_moments(monkeypatch)

    tables = [User.__table__, Subscription.__table__, SubscriptionConversion.__table__, Transaction.__table__]
    async with memory_session(monkeypatch, tables) as db:
        user = User(telegram_id=1, language='ru', status=UserStatus.ACTIVE.value)
        db.add(user)
        await db.flush()

        def purchase(at: datetime) -> Transaction:
            return Transaction(
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                amount_kopeks=100,
                payment_method=REAL_PAYMENT_METHODS[0],
                is_completed=True,
                created_at=at,
            )

        db.add_all([purchase(early_today), purchase(late_yesterday), purchase(datetime.now(UTC))])
        await db.commit()

        stats = await get_subscriptions_statistics(db)

    assert stats['purchased_today'] == 2
