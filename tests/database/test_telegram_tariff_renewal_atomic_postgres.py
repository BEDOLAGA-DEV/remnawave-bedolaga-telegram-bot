"""Tariff renewal must commit its debit, paid days and ledger together."""

import asyncio
import inspect

import pytest
from sqlalchemy import select

from app.database.models import Transaction
from app.handlers.subscription import tariff_purchase as telegram
from tests.database.test_quick_tariff_renewal_postgres import checkout


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('failure', ['extension', 'ledger', 'cancel'])
@pytest.mark.parametrize('flow', ['renewal', 'switch'])
async def test_tariff_renewal_failure_does_not_leave_a_debit(postgres_database, monkeypatch, failure, flow):
    async with checkout(postgres_database, monkeypatch) as c:
        c.callback.data = 'tariff_ext_confirm:10:1:30'
        handler = telegram.confirm_tariff_extend
        if flow == 'switch':
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, Mock

            from app.config import settings
            from app.database.models import Tariff

            monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
            monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
            c.db.add(
                Tariff(
                    id=2,
                    name='Other',
                    is_active=True,
                    traffic_limit_gb=200,
                    device_limit=2,
                    period_prices={'30': 25000},
                    allowed_squads=['test-squad'],
                )
            )
            await c.db.commit()
            c.callback.data = 'tariff_switch_confirm:2:30'
            handler = telegram.confirm_tariff_switch
            monkeypatch.setattr(
                'app.services.remnawave_service.RemnaWaveService',
                lambda: SimpleNamespace(get_api_client=Mock(return_value=AsyncMock())),
            )

        async def fail(*args, **kwargs):
            if failure == 'cancel':
                raise asyncio.CancelledError
            raise RuntimeError('injected billing failure')

        monkeypatch.setattr(telegram, 'create_transaction' if failure == 'ledger' else 'extend_subscription', fail)
        call = inspect.unwrap(handler)
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await call(c.callback, c.user, c.db, c.state)
        else:
            await call(c.callback, c.user, c.db, c.state)
        await c.db.rollback()  # request teardown must not reveal an earlier partial commit
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000
        assert c.sub.end_date == c.end
        assert list(await c.db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
async def test_tariff_renewal_commits_paid_days_even_when_panel_fails(postgres_database, monkeypatch, multi, trial):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.services.remnawave_retry_queue import remnawave_retry_queue

    async with checkout(postgres_database, monkeypatch) as c:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
        c.sub.is_trial = trial
        await c.db.commit()
        c.callback.data = 'tariff_ext_confirm:10:1:30'
        panel = SimpleNamespace(update_remnawave_user=AsyncMock(side_effect=RuntimeError('panel unavailable')))
        monkeypatch.setattr(telegram, 'SubscriptionService', lambda: panel)
        monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=False))
        monkeypatch.setattr(remnawave_retry_queue, 'enqueue', lambda **kwargs: None)
        await inspect.unwrap(telegram.confirm_tariff_extend)(c.callback, c.user, c.db, c.state)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        transactions = list(await c.db.scalars(select(Transaction)))
        assert c.user.balance_kopeks == 370000
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -30000
        assert c.sub.is_trial is False
        assert c.sub.end_date >= c.now + timedelta(days=30)
        if not trial:
            assert c.sub.end_date == c.end + timedelta(days=30)
        assert c.sub.pending_panel_expire_at == c.sub.end_date


async def test_tariff_manual_charge_blocks_autopay_for_same_cycle(postgres_database, monkeypatch):
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.database.test_autopay_races_postgres import worker
    from tests.fixtures.postgres_db import lock_waiter_appeared

    async with checkout(postgres_database, monkeypatch) as c:
        c.sub.autopay_enabled = True
        c.sub.autopay_days_before = 3
        c.sub.end_date = c.now + timedelta(days=2)
        await c.db.commit()
        old_end = c.sub.end_date
        c.callback.data = 'tariff_ext_confirm:10:1:30'
        held, release = asyncio.Event(), asyncio.Event()
        original = telegram.subtract_user_balance

        async def pause_charge(*args, **kwargs):
            held.set()
            await asyncio.wait_for(release.wait(), 10)
            return await original(*args, **kwargs)

        monkeypatch.setattr(telegram, 'subtract_user_balance', pause_charge)
        async with AsyncSession(c.db.bind, expire_on_commit=False) as auto_db:
            async with AsyncSession(c.db.bind, expire_on_commit=False) as observer:
                manual = asyncio.create_task(
                    inspect.unwrap(telegram.confirm_tariff_extend)(c.callback, c.user, c.db, c.state)
                )
                automatic = None
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    automatic = asyncio.create_task(worker()._process_autopayments(auto_db))
                    assert await lock_waiter_appeared(observer, timeout=3)
                    release.set()
                    await asyncio.wait_for(asyncio.gather(manual, automatic), 15)
                finally:
                    release.set()
                    for task in (manual, automatic):
                        if task and not task.done():
                            task.cancel()
                    await asyncio.gather(*(t for t in (manual, automatic) if t), return_exceptions=True)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 370000
        assert c.sub.end_date == old_end + timedelta(days=30)
        assert len(list(await c.db.scalars(select(Transaction)))) == 1


@pytest.mark.parametrize('change', ['paid-days', 'tariff'])
async def test_tariff_renewal_rechecks_stale_subscription_before_charge(postgres_database, monkeypatch, change):
    from datetime import timedelta

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription, Tariff

    async with checkout(postgres_database, monkeypatch) as c:
        c.callback.data = 'tariff_ext_confirm:10:1:30'
        async with AsyncSession(c.db.bind, expire_on_commit=False) as other:
            if change == 'paid-days':
                await other.execute(
                    update(Subscription).where(Subscription.id == 10).values(end_date=c.end + timedelta(days=30))
                )
            else:
                other.add(
                    Tariff(
                        id=2,
                        name='Other',
                        is_active=True,
                        traffic_limit_gb=200,
                        device_limit=2,
                        period_prices={'30': 25000},
                    )
                )
                await other.flush()
                await other.execute(update(Subscription).where(Subscription.id == 10).values(tariff_id=2))
            await other.commit()
        await inspect.unwrap(telegram.confirm_tariff_extend)(c.callback, c.user, c.db, c.state)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        transactions = list(await c.db.scalars(select(Transaction)))
        if change == 'paid-days':
            assert c.sub.end_date == c.end + timedelta(days=60)
            assert c.user.balance_kopeks == 370000
            assert len(transactions) == 1
        else:
            assert c.sub.tariff_id == 2
            assert c.sub.end_date == c.end
            assert c.user.balance_kopeks == 400000
            assert transactions == []


@pytest.mark.parametrize('multi', [False, True])
async def test_tariff_switch_commits_target_and_payment_despite_panel_failure(postgres_database, monkeypatch, multi):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from app.config import settings
    from app.database.models import Tariff
    from app.services.remnawave_retry_queue import remnawave_retry_queue

    async with checkout(postgres_database, monkeypatch) as c:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
        monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
        monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
        c.db.add(
            Tariff(
                id=2,
                name='Other',
                is_active=True,
                traffic_limit_gb=200,
                device_limit=2,
                period_prices={'30': 25000},
                allowed_squads=['test-squad'],
            )
        )
        await c.db.commit()
        c.callback.data = 'tariff_switch_confirm:2:30'
        c.state.get_data.return_value = {'active_subscription_id': 10}
        panel = SimpleNamespace(update_remnawave_user=AsyncMock(side_effect=RuntimeError('panel unavailable')))
        monkeypatch.setattr(telegram, 'SubscriptionService', lambda: panel)
        monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=False))
        monkeypatch.setattr(remnawave_retry_queue, 'enqueue', lambda **kwargs: None)
        monkeypatch.setattr(
            'app.services.remnawave_service.RemnaWaveService',
            lambda: SimpleNamespace(get_api_client=Mock(return_value=AsyncMock())),
        )
        await inspect.unwrap(telegram.confirm_tariff_switch)(c.callback, c.user, c.db, c.state)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        transactions = list(await c.db.scalars(select(Transaction)))
        assert c.user.balance_kopeks == 375000
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -25000
        assert c.sub.tariff_id == 2 and c.sub.device_limit == 2
        assert c.sub.traffic_limit_gb == 200
        assert c.sub.end_date == c.end + timedelta(days=30)
        assert c.sub.pending_panel_expire_at == c.sub.end_date


@pytest.mark.parametrize('flow', ['renewal', 'switch'])
@pytest.mark.parametrize('rollback', [False, True])
async def test_lava_shift_observes_only_committed_manual_period(postgres_database, monkeypatch, flow, rollback):
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription, User

    async with checkout(postgres_database, monkeypatch) as c:
        c.callback.data = 'tariff_ext_confirm:10:1:30' if flow == 'renewal' else 'tariff_switch_confirm:1:30'
        handler = telegram.confirm_tariff_extend if flow == 'renewal' else telegram.confirm_tariff_switch
        monkeypatch.setattr(
            'app.services.remnawave_service.RemnaWaveService',
            lambda: SimpleNamespace(get_api_client=Mock(return_value=AsyncMock())),
        )
        committed_shifts = []

        async def shift(db, subscription_id, days):
            # External provider observes financial rights from another connection.
            async with AsyncSession(c.db.bind, expire_on_commit=False) as observer:
                sub = await observer.get(Subscription, subscription_id)
                user = await observer.get(User, c.user.id)
                ledger = list(await observer.scalars(select(Transaction)))
                assert sub.end_date == c.end + timedelta(days=days)
                assert user.balance_kopeks == 400000 - (30000 if flow == 'renewal' else 20000)
                assert len(ledger) == 1
                committed_shifts.append((subscription_id, days))

        monkeypatch.setattr('app.services.payment.lava.shift_lava_next_charge_after_manual_extension', shift)
        if rollback:

            async def fail(*args, **kwargs):
                raise RuntimeError('ledger write failed')

            monkeypatch.setattr(telegram, 'create_transaction', fail)
        await inspect.unwrap(handler)(c.callback, c.user, c.db, c.state)
        assert committed_shifts == ([] if rollback else [(10, 30)])
