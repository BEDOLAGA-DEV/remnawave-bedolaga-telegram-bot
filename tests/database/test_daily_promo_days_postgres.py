"""#2896: credited promo days defer daily billing until the granted access expires."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.cabinet.routes import promocode as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, PromoCode, Subscription, Tariff, Transaction, User
from app.services import daily_subscription_service as daily
from app.services.promocode_service import PromoCodeService
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('flow', ['service', 'cabinet'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('balance', [0, 10000])
async def test_daily_promo_days_defer_charge_and_keep_access(
    postgres_database, monkeypatch, flow, multi, expired, balance
):
    import app.database.crud.subscription as subscription_crud

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    panel_updates = []

    async def update_panel(self, db, subscription, **kwargs):
        panel_updates.append(subscription.end_date)
        return SimpleNamespace(id=9001)

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', update_panel)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    clock = {'now': now}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['now'] if tz else clock['now'].replace(tzinfo=None)

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=balance, remnawave_id=9001),
                Tariff(
                    id=1,
                    name='Daily',
                    is_active=True,
                    is_daily=True,
                    daily_price_kopeks=100,
                    traffic_limit_gb=100,
                    device_limit=2,
                    allowed_squads=['test-squad'],
                ),
                PromoCode(
                    id=1, code='FREE3', type='subscription_days', subscription_days=3, max_uses=10, is_active=True
                ),
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=1,
                    remnawave_short_id='test-10',
                    remnawave_id=9001,
                    status='expired' if expired else 'active',
                    is_trial=False,
                    device_limit=2,
                    end_date=now + timedelta(hours=-1 if expired else 6),
                    last_daily_charge_at=now - timedelta(hours=25),
                    connected_squads=['test-squad'],
                ),
            ]
        )
        await db.commit()
        user = await get_user_by_id(db, 1)
        service = PromoCodeService()
        if flow == 'cabinet':
            result = await cabinet.activate_promocode(
                cabinet.PromocodeActivateRequest(code='FREE3', subscription_id=10), user, db
            )
            assert result.success
        else:
            assert (await service.activate_promocode(db, 1, 'FREE3', subscription_id=10))['success']
        sub = await db.get(Subscription, 10)
        granted_end = sub.end_date
        assert granted_end > now + timedelta(days=2)
        assert panel_updates[-1] == granted_end
        assert not (await service.activate_promocode(db, 1, 'FREE3', subscription_id=10))['success']
        await db.refresh(sub)
        assert sub.end_date == granted_end

        @asynccontextmanager
        async def session():
            yield db

        monkeypatch.setattr(daily, 'AsyncSessionLocal', session)
        monkeypatch.setattr(daily, 'datetime', Clock)
        monkeypatch.setattr(subscription_crud, 'datetime', Clock)
        clock['now'] = now + timedelta(hours=25)
        stats = await daily.DailySubscriptionService().process_daily_charges()
        await db.refresh(user)
        await db.refresh(sub)
        assert stats['charged'] == stats['suspended'] == stats['errors'] == 0
        assert user.balance_kopeks == balance and sub.status == 'active'
        assert sub.end_date == granted_end
        assert (await db.scalars(select(Transaction))).all() == []
        # A worker may already have selected this row before the promo was applied.
        subscription = await daily.DailySubscriptionService()._reload_daily_subscription(db, 10)
        assert await daily.DailySubscriptionService()._process_single_charge(db, subscription) == 'skipped'
        clock['now'] = granted_end + timedelta(seconds=1)
        stats = await daily.DailySubscriptionService().process_daily_charges()
        await db.refresh(user)
        assert stats['charged'] == (1 if balance else 0)
        assert stats['suspended'] == (0 if balance else 1)
        assert user.balance_kopeks == (balance - 100 if balance else 0)
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == (1 if balance else 0)
        if transactions:
            assert transactions[0].amount_kopeks == -100
        assert (await daily.DailySubscriptionService().process_daily_charges())['charged'] == 0


async def test_parallel_daily_workers_charge_only_once(postgres_database, monkeypatch):
    import asyncio

    from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        left,
        right,
        observer,
    ):
        left.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=1000, remnawave_id=9001),
                Tariff(
                    id=1,
                    name='Daily',
                    is_active=True,
                    is_daily=True,
                    daily_price_kopeks=100,
                    traffic_limit_gb=100,
                    device_limit=2,
                ),
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=1,
                    remnawave_id=9001,
                    remnawave_short_id='test-10',
                    status='active',
                    is_trial=False,
                    end_date=now - timedelta(hours=1),
                    last_daily_charge_at=now - timedelta(hours=25),
                    connected_squads=['test-squad'],
                ),
            ]
        )
        await left.commit()
        service = daily.DailySubscriptionService()
        first_sub = await service._reload_daily_subscription(left, 10)
        second_sub = await service._reload_daily_subscription(right, 10)
        deducted = asyncio.Event()
        release = asyncio.Event()
        subtract = daily.subtract_user_balance

        async def pause(db, *args, **kwargs):
            result = await subtract(db, *args, **kwargs)
            if db is left:
                deducted.set()
                await asyncio.wait_for(release.wait(), 10)
            return result

        monkeypatch.setattr(daily, 'subtract_user_balance', pause)

        async def worker(db, sub):
            try:
                return await service._process_single_charge(db, sub)
            finally:
                await db.rollback()

        first = asyncio.create_task(worker(left, first_sub))
        await asyncio.wait_for(deducted.wait(), 10)
        second = asyncio.create_task(worker(right, second_sub))
        try:
            await wait_for_lock_waiter(observer)
        finally:
            release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert sorted(outcomes) == ['charged', 'skipped']
        await observer.rollback()
        assert (await observer.get(User, 1)).balance_kopeks == 900
        transactions = (await observer.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -100
