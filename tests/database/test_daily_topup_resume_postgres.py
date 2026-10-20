"""Daily top-up spending obeys live settings and is atomic across callbacks."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Transaction, User
from app.services import subscription_auto_purchase_service as auto_purchase, subscription_renewal_service as renewal
from tests.fixtures.postgres_db import postgres_session, postgres_sessions, wait_for_lock_waiter
from tests.services.test_daily_topup_resume_traffic_reset import (
    BALANCE_KOPEKS,
    DAILY_PRICE_KOPEKS,
    _FakePanelSync,
    _rows,
)


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    fake = _FakePanelSync()
    monkeypatch.setattr(auto_purchase, 'SubscriptionService', lambda: fake)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    return fake


async def seed(db, status='disabled'):
    db.add_all(_rows(traffic_reset_mode='NO_RESET', status=status))
    await db.commit()
    return await db.get(User, 1), await db.get(Subscription, 10)


async def payments(db):
    return list((await db.scalars(select(Transaction))).all())


@pytest.mark.parametrize('auto_enabled', [False, True])
@pytest.mark.parametrize('daily_enabled', [False, True])
@pytest.mark.parametrize('status', ['disabled', 'expired', 'limited'])
@pytest.mark.parametrize('multi', [False, True])
async def test_daily_topup_obeys_settings_and_repeat(
    postgres_database, monkeypatch, panel, auto_enabled, daily_enabled, status, multi
):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', auto_enabled)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', daily_enabled)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await seed(db, status)
        before = (sub.status, sub.end_date, sub.last_daily_charge_at, sub.updated_at)
        resumed = await auto_purchase.try_resume_disabled_daily_after_topup(db, user)
        expected = auto_enabled and daily_enabled
        assert resumed is expected
        assert await auto_purchase.try_resume_disabled_daily_after_topup(db, user) is False
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE_KOPEKS - (DAILY_PRICE_KOPEKS if expected else 0)
        records = await payments(db)
        assert len(records) == int(expected)
        assert len(panel.calls) == int(expected)
        if expected:
            assert records[0].amount_kopeks == -DAILY_PRICE_KOPEKS
            assert sub.status == 'active'
            assert sub.last_daily_charge_at > before[2]
            assert sub.end_date > before[1]
        else:
            assert (sub.status, sub.end_date, sub.last_daily_charge_at, sub.updated_at) == before


@pytest.mark.parametrize('reason', ['paused', 'trial', 'period', 'balance', 'recent'])
async def test_daily_topup_preserves_eligibility_guards(postgres_database, monkeypatch, panel, reason):
    from datetime import UTC, datetime, timedelta

    from app.database.models import Tariff

    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await seed(db)
        if reason == 'paused':
            sub.is_daily_paused = True
        elif reason == 'trial':
            sub.is_trial = True
        elif reason == 'period':
            (await db.get(Tariff, 1)).is_daily = False
        elif reason == 'balance':
            user.balance_kopeks = DAILY_PRICE_KOPEKS - 1
        else:
            sub.updated_at = datetime.now(UTC)
        if reason != 'recent':
            # Exercise each guard itself, not the unrelated recent-update guard.
            sub.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        await db.commit()
        balance = user.balance_kopeks
        assert await auto_purchase.try_resume_disabled_daily_after_topup(db, user) is False
        await db.refresh(user)
        assert user.balance_kopeks == balance
        assert await payments(db) == []
        assert panel.calls == []


@pytest.mark.parametrize(('auto_enabled', 'daily_enabled'), [(False, True), (True, False)])
@pytest.mark.parametrize('status', ['disabled', 'expired', 'limited'])
@pytest.mark.parametrize('multi', [False, True])
async def test_payment_hook_keeps_topup_and_notification_when_daily_disabled(
    postgres_database, monkeypatch, panel, auto_enabled, daily_enabled, status, multi
):
    from datetime import UTC, datetime, timedelta

    from app.database.models import Tariff
    from app.services.payment import common

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', auto_enabled)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', daily_enabled)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    notification = AsyncMock()
    monkeypatch.setattr(common, 'notify_email_user_topup', notification)
    monkeypatch.setattr(common.user_cart_service, 'get_user_cart', AsyncMock(return_value=None))
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await seed(db, status)
        sub.autopay_enabled = True
        sub.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        (await db.get(Tariff, 1)).period_prices = {'1': DAILY_PRICE_KOPEKS}
        await db.commit()
        await common.send_cart_notification_after_topup(user, 5000, db, None)
        await db.refresh(user)
        assert user.balance_kopeks == BALANCE_KOPEKS
        assert await payments(db) == []
        assert panel.calls == []
        notification.assert_awaited_once_with(user, 5000)


async def test_daily_topup_reads_database_setting_override(postgres_database, monkeypatch, panel):
    from app.services.settings_store import store_setting
    from app.services.system_settings_service import bot_configuration_service as configuration

    configuration.initialize_definitions()
    monkeypatch.setattr(configuration, '_env_override_keys', set())
    monkeypatch.setattr(configuration, '_overrides_raw', {})
    async with postgres_session(postgres_database, TABLES) as db:
        user, _ = await seed(db)
        await store_setting(db, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
        assert settings.is_auto_purchase_after_topup_enabled() is False
        assert await auto_purchase.try_resume_disabled_daily_after_topup(db, user) is False
        assert await payments(db) == []
        await store_setting(db, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        assert await auto_purchase.try_resume_disabled_daily_after_topup(db, user) is True
        assert len(await payments(db)) == 1


async def test_two_topups_charge_one_day(postgres_database, monkeypatch, panel):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (left, right, observer):
        user, _ = await seed(left)
        other = await right.get(User, user.id)
        first_deducted = asyncio.Event()
        second_deducted = asyncio.Event()
        release_first = asyncio.Event()
        subtract = auto_purchase.subtract_user_balance

        async def pause_after_first_deduction(db, *args, **kwargs):
            result = await subtract(db, *args, **kwargs)
            if db is left:
                first_deducted.set()
                await asyncio.wait_for(release_first.wait(), 10)
            else:
                second_deducted.set()
            return result

        monkeypatch.setattr(auto_purchase, 'subtract_user_balance', pause_after_first_deduction)

        async def callback(db, current_user):
            try:
                return await auto_purchase.try_resume_disabled_daily_after_topup(db, current_user)
            finally:
                # Like the request/session lifecycle, release locks on no-op too.
                await db.rollback()

        first = asyncio.create_task(callback(left, user))
        await asyncio.wait_for(first_deducted.wait(), 10)
        second = asyncio.create_task(callback(right, other))
        waiting = asyncio.create_task(wait_for_lock_waiter(observer))
        duplicated = asyncio.create_task(second_deducted.wait())
        try:
            done, _ = await asyncio.wait({waiting, duplicated}, timeout=7, return_when=asyncio.FIRST_COMPLETED)
            assert done, 'second callback neither waited for the row lock nor deducted'
            for task in done:
                await task
        finally:
            release_first.set()
            for task in (waiting, duplicated):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, duplicated, return_exceptions=True)
        outcomes = await asyncio.wait_for(asyncio.gather(first, second), 10)
        await observer.rollback()
        fresh_user = await observer.get(User, 1)
        assert fresh_user.balance_kopeks == BALANCE_KOPEKS - DAILY_PRICE_KOPEKS
        assert sorted(outcomes) == [False, True]
        assert len(await payments(observer)) == 1
        assert len(panel.calls) == 1


@pytest.mark.parametrize('failure', ['activation', 'transaction', 'charge_time'])
async def test_failed_resume_rolls_back_payment(postgres_database, monkeypatch, panel, failure):
    import app.database.crud.subscription as subscriptions

    async with postgres_session(postgres_database, TABLES) as db:
        user, sub = await seed(db)
        before = (sub.status, sub.end_date, sub.last_daily_charge_at)
        if failure == 'activation':
            commit = db.commit
            failed = False

            async def fail_activation():
                nonlocal failed
                if not failed and sub.status == 'active':
                    failed = True
                    raise RuntimeError('activation failed')
                await commit()

            monkeypatch.setattr(db, 'commit', fail_activation)
        elif failure == 'transaction':
            monkeypatch.setattr(
                auto_purchase, 'create_transaction', AsyncMock(side_effect=RuntimeError('ledger failed'))
            )
        else:
            monkeypatch.setattr(
                subscriptions, 'update_daily_charge_time', AsyncMock(side_effect=RuntimeError('charge time failed'))
            )
        assert await auto_purchase.try_resume_disabled_daily_after_topup(db, user) is False
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE_KOPEKS
        assert (sub.status, sub.end_date, sub.last_daily_charge_at) == before
        assert await payments(db) == []
        assert panel.calls == []
