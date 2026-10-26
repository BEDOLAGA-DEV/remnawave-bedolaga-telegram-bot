from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import PERIOD_PRICES, settings
from app.database.models import Base, Subscription, Transaction, User, WheelConfig, WheelPrize, WheelSpin
from app.services.wheel_service import FortuneWheelService
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('panel_action', ['commit', 'rollback'])
@pytest.mark.parametrize('prize_kind', ['balance_bonus', 'subscription_days'])
@pytest.mark.parametrize('grant_fails', [False, True])
@pytest.mark.parametrize(
    ('payment_type', 'roll', 'cost'), [('subscription_days', 0.98, 330), ('telegram_stars', 0.95, 1000)]
)
async def test_real_spin_uses_current_cost_and_persists_exact_prize(
    postgres_database, monkeypatch, payment_type, roll, cost, grant_fails, prize_kind, panel_action
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(type(settings), 'get_stars_rate', lambda self: 1)
    monkeypatch.setitem(PERIOD_PRICES, 30, 9900)
    monkeypatch.setattr('app.services.wheel_service.random.random', lambda: roll)
    if prize_kind == 'subscription_days':
        roll = 0.85 if payment_type == 'subscription_days' else 0.5
    monkeypatch.setattr('app.services.wheel_service.random.random', lambda: roll)
    sync_history_counts = []

    async def sync_with_commit(session, subscription):
        # The real panel service can commit this session. Observe whether spin delivery is already durable.
        sync_history_counts.append(await session.scalar(select(func.count()).select_from(WheelSpin)))
        if panel_action == 'rollback':
            await session.rollback()
            return None
        await session.commit()
        return True

    panel_sync = AsyncMock(side_effect=sync_with_commit)
    monkeypatch.setattr('app.services.wheel_service.SubscriptionService.update_remnawave_user', panel_sync)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        monkeypatch.setattr(
            'app.database.database.AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False)
        )
        user = User(id=1, telegram_id=1001, language='ru', status='active', balance_kopeks=10000)
        end = datetime.now(UTC) + timedelta(days=40)
        sub = Subscription(
            id=1,
            user_id=1,
            status='active',
            is_trial=False,
            start_date=datetime.now(UTC),
            end_date=end,
            traffic_limit_gb=100,
            device_limit=1,
        )
        config = WheelConfig(
            id=1,
            is_enabled=True,
            rtp_percent=70,
            spin_cost_stars=10,
            spin_cost_days=1,
            daily_spin_limit=5,
            min_subscription_days_for_day_payment=3,
        )
        db.add_all([user, sub, config])
        await db.flush()
        for index, (value, kind, award) in enumerate(
            [
                (0, 'nothing', 0),
                (330, 'subscription_days', 1),
                (2500, 'balance_bonus', 2500),
                (10000, 'balance_bonus', 10000),
                (50000, 'balance_bonus', 50000),
            ],
            1,
        ):
            db.add(
                WheelPrize(
                    id=index,
                    config_id=1,
                    prize_type=kind,
                    prize_value=award,
                    prize_value_kopeks=value,
                    display_name=f'Example {index}',
                    sort_order=index,
                    is_active=True,
                )
            )
        await db.commit()
        if grant_fails:
            original_grant = FortuneWheelService._apply_prize

            async def grant_then_fail(service, *args, **kwargs):
                await original_grant(service, *args, **kwargs)
                raise ValueError('local grant failure')

            monkeypatch.setattr(FortuneWheelService, '_apply_prize', grant_then_fail)
        result = await FortuneWheelService().spin(db, user, payment_type, subscription_id=1)
        if grant_fails:
            assert not result.success
            panel_sync.assert_not_awaited()
            await db.refresh(user)
            await db.refresh(sub)
            assert user.balance_kopeks == 10000
            assert sub.end_date == end
            assert await db.scalar(select(func.count()).select_from(WheelSpin)) == 0
            assert await db.scalar(select(func.count()).select_from(Transaction)) == 0
            return
        assert result.success, result
        expected_prize_id = 3 if prize_kind == 'balance_bonus' else 2
        assert result.prize_id == expected_prize_id
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == 10000 - (1000 if payment_type == 'telegram_stars' else 0) + (
            2500 if prize_kind == 'balance_bonus' else 0
        )
        assert sub.end_date == end + timedelta(
            days=(1 if prize_kind == 'subscription_days' else 0) - (1 if payment_type == 'subscription_days' else 0)
        )
        assert all(count == 1 for count in sync_history_counts)
        assert panel_sync.await_count == (
            1 if payment_type == 'subscription_days' or prize_kind == 'subscription_days' else 0
        )
        spin = (await db.execute(select(WheelSpin))).scalar_one()
        assert spin.prize_id == expected_prize_id
        assert spin.payment_type == payment_type
        assert spin.payment_value_kopeks == cost
        assert spin.prize_value_kopeks == (2500 if prize_kind == 'balance_bonus' else 330)
        assert spin.is_applied is True
        transactions = list((await db.execute(select(Transaction))).scalars())
        if prize_kind == 'balance_bonus':
            assert len(transactions) == 1
            assert transactions[0].amount_kopeks == 2500
        else:
            assert transactions == []
