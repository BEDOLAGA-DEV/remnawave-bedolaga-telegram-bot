"""Regression coverage for subscription-payment sales classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes.admin_sales_stats import get_addons_stats, get_renewals_stats
from app.database.models import PaymentMethod, Transaction, TransactionType, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

ADMIN = SimpleNamespace(id=1, username='admin')


def _payment(user_id: int, description: str, created_at: datetime, amount: int = -10000) -> Transaction:
    return Transaction(
        user_id=user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=amount,
        description=description,
        payment_method=PaymentMethod.BALANCE.value,
        is_completed=True,
        created_at=created_at,
        completed_at=created_at,
    )


@pytest.mark.asyncio
async def test_tariff_device_word_is_not_classified_as_addon(postgres_database):
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, [Transaction.__table__, User.__table__]) as db:
        user = User(telegram_id=10001, first_name='Stats')
        traffic_user = User(telegram_id=10004, first_name='Traffic')
        db.add_all([user, traffic_user])
        await db.flush()
        db.add_all(
            [
                _payment(user.id, 'Покупка тарифа 3 устройств на 30 дней', now - timedelta(days=40)),
                _payment(user.id, 'Продление тарифа 3 устройств на 30 дней', now - timedelta(days=5)),
                _payment(user.id, 'Покупка 2 доп. устройств', now - timedelta(days=4), -5000),
                _payment(user.id, 'Изменение количества устройств с 3 до 4', now - timedelta(days=3), -2500),
                _payment(
                    traffic_user.id,
                    'Покупка тарифа Безлимитный трафик на 30 дней',
                    now - timedelta(days=2),
                ),
                _payment(traffic_user.id, 'Докупка 10 ГБ трафика', now - timedelta(days=1), -3000),
            ]
        )
        await db.commit()

        addons = await get_addons_stats(days=30, start_date=None, end_date=None, admin=ADMIN, db=db)
        renewals = await get_renewals_stats(days=30, start_date=None, end_date=None, admin=ADMIN, db=db)

    assert addons.device_purchases == 2
    assert addons.device_revenue_kopeks == 7500
    assert addons.addon_revenue_kopeks == 3000
    assert renewals.total_renewals == 1
    assert renewals.total_revenue_kopeks == 10000


@pytest.mark.asyncio
async def test_all_time_renewals_exclude_first_payment(postgres_database):
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, [Transaction.__table__, User.__table__]) as db:
        repeat_user = User(telegram_id=10002, first_name='Repeat')
        single_user = User(telegram_id=10003, first_name='Single')
        db.add_all([repeat_user, single_user])
        await db.flush()
        db.add_all(
            [
                _payment(repeat_user.id, 'Покупка тарифа 1 устройство на 30 дней', now - timedelta(days=40)),
                _payment(repeat_user.id, 'Продление тарифа 1 устройство на 30 дней', now - timedelta(days=5)),
                _payment(single_user.id, 'Покупка тарифа 5 устройств на 30 дней', now - timedelta(days=3)),
            ]
        )
        await db.commit()

        renewals = await get_renewals_stats(days=0, start_date=None, end_date=None, admin=ADMIN, db=db)

    assert renewals.total_renewals == 1
    assert renewals.total_revenue_kopeks == 10000
    assert sum(item.count for item in renewals.daily) == 1
