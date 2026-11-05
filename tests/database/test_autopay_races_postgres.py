"""Autopay must recheck consent and due date under the billing transaction lock."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud import user as user_crud
from app.database.models import Base, Transaction, User
from app.services import monitoring_service as monitoring, subscription_auto_purchase_service as auto
from tests.database.test_renewal_survives_stale_panel_snapshot_postgres import (
    BALANCE,
    PRICE,
    _renew_from_cabinet,
    _seed,
)
from tests.fixtures.postgres_db import postgres_session, postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def quiet_settings(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(monitoring.cache, 'exists', AsyncMock(return_value=True))


def worker():
    service = monitoring.MonitoringService(bot=None)
    service.subscription_service = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=SimpleNamespace(id=555))
    )
    return service


@pytest.mark.parametrize('change', ['manual-renew', 'disable-autopay', 'block-user'])
async def test_candidate_is_revalidated_before_charge(postgres_database, monkeypatch, change):
    async with postgres_sessions(postgres_database, TABLES, count=2) as (auto_db, manual_db):
        user, sub, old_end = await _seed(manual_db, datetime.now(UTC))
        uid, sid = user.id, sub.id
        selected, proceed = asyncio.Event(), asyncio.Event()
        original_lock = user_crud.lock_user_for_pricing

        async def delayed_lock(db, user_id):
            if db is auto_db:
                selected.set()
                await asyncio.wait_for(proceed.wait(), 10)
            return await original_lock(db, user_id)

        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', delayed_lock)
        task = asyncio.create_task(worker()._process_autopayments(auto_db))
        try:
            await asyncio.wait_for(selected.wait(), 10)
            if change == 'manual-renew':
                await _renew_from_cabinet(manual_db, user, sub, monkeypatch)
                await manual_db.commit()  # close the completed request's side-effect transaction
            elif change == 'disable-autopay':
                sub.autopay_enabled = False
                await manual_db.commit()
            else:
                user.status = 'blocked'
                await manual_db.commit()
        finally:
            proceed.set()
            await asyncio.wait_for(task, 15)
        await manual_db.refresh(user)
        await manual_db.refresh(sub)
        paid_count = int(change == 'manual-renew')
        assert user.balance_kopeks == BALANCE - PRICE * paid_count
        assert sub.end_date == old_end + timedelta(days=30 * paid_count)
        transactions = list(await manual_db.scalars(select(Transaction).where(Transaction.user_id == uid)))
        assert len(transactions) == paid_count
        assert sub.id == sid


@pytest.mark.parametrize('entry', ['monitor', 'topup'])
async def test_two_workers_cannot_charge_the_same_due_cycle(postgres_database, monkeypatch, entry):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, observer):
        user, sub, old_end = await _seed(observer, datetime.now(UTC))
        if entry == 'topup':
            sub.status = 'expired'
            sub.end_date = old_end = datetime.now(UTC) - timedelta(minutes=30)
            sub.updated_at = datetime.now(UTC) - timedelta(days=1)
            await observer.commit()
            monkeypatch.setattr(auto, 'SubscriptionService', lambda: worker().subscription_service)
        uid, sid = user.id, sub.id
        deducted, proceed = asyncio.Event(), asyncio.Event()
        module = monitoring if entry == 'monitor' else auto
        original_extend = module.extend_subscription

        async def delayed_extend(db, subscription, days, **kwargs):
            if db is first:
                deducted.set()
                await asyncio.wait_for(proceed.wait(), 10)
            return await original_extend(db, subscription, days, **kwargs)

        monkeypatch.setattr(module, 'extend_subscription', delayed_extend)

        async def run(db):
            if entry == 'monitor':
                await worker()._process_autopayments(db)
            else:
                await auto.try_auto_extend_expired_after_topup(db, await db.get(User, uid))

        task1 = asyncio.create_task(run(first))
        task2 = None
        try:
            await asyncio.wait_for(deducted.wait(), 10)
            task2 = asyncio.create_task(run(second))
            # Correct code blocks worker2; old code completes a duplicate charge.
            from tests.fixtures.postgres_db import lock_waiter_appeared

            await lock_waiter_appeared(observer, timeout=1)
        finally:
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*([task1, task2] if task2 else [task1])), 15)
        await observer.refresh(user)
        await observer.refresh(sub)
        assert user.balance_kopeks == BALANCE - PRICE
        expected_end = old_end + timedelta(days=30) if entry == 'monitor' else datetime.now(UTC) + timedelta(days=30)
        assert abs((sub.end_date - expected_end).total_seconds()) < 15
        assert len(list(await observer.scalars(select(Transaction).where(Transaction.user_id == uid)))) == 1
        assert sub.id == sid


@pytest.mark.parametrize('mode', ['classic', 'tariffs'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('entry', ['monitor', 'topup'])
async def test_imported_subscription_with_autopay_off_never_charges(postgres_database, monkeypatch, mode, multi, entry):
    monkeypatch.setattr(settings, 'SALES_MODE', mode)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub, _ = await _seed(db, datetime.now(UTC))
        sub.tariff_id = None
        sub.autopay_enabled = False
        sub.end_date = datetime.now(UTC) - timedelta(minutes=30)
        sub.status = 'expired'
        sub.updated_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        original_end = sub.end_date
        if entry == 'monitor':
            await worker()._process_autopayments(db)
        else:
            assert await auto.try_auto_extend_expired_after_topup(db, user) is False
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE
        assert sub.end_date == original_end
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('entry', ['monitor', 'topup', 'manual'])
@pytest.mark.parametrize('fault', ['extend', 'ledger', 'commit'])
async def test_failed_renewal_rolls_back_money_days_and_promo(postgres_database, monkeypatch, entry, fault):
    from app.database.crud import transaction as transaction_crud
    from app.services import subscription_renewal_service as renewal
    from app.services.pricing_engine import pricing_engine

    async with postgres_session(postgres_database, TABLES) as db:
        user, sub, old_end = await _seed(db, datetime.now(UTC))
        user.promo_offer_discount_percent = 15
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        if entry == 'topup':
            sub.status = 'expired'
            sub.end_date = old_end = datetime.now(UTC) - timedelta(minutes=30)
            sub.updated_at = datetime.now(UTC) - timedelta(days=1)
            monkeypatch.setattr(auto, 'SubscriptionService', lambda: worker().subscription_service)
        await db.commit()
        if entry == 'manual':
            user = await user_crud.lock_user_for_pricing(db, user.id)
            pricing = await pricing_engine.calculate_renewal_price(db, sub, 30, user=user)
            assert pricing.promo_offer_discount > 0
        module = {'monitor': monitoring, 'topup': auto, 'manual': renewal}[entry]
        fail = AsyncMock(side_effect=RuntimeError('synthetic storage failure'))
        if fault == 'extend':
            monkeypatch.setattr(module, 'extend_subscription', fail)
        elif fault == 'ledger':
            monkeypatch.setattr(transaction_crud if entry == 'monitor' else module, 'create_transaction', fail)
        else:
            monkeypatch.setattr(db, 'commit', fail)
        if entry == 'monitor':
            await worker()._process_autopayments(db)
        elif entry == 'manual':
            with pytest.raises(RuntimeError, match='synthetic storage failure'):
                await renewal.SubscriptionRenewalService().finalize(db, user, sub, pricing)
        else:
            assert await auto.try_auto_extend_expired_after_topup(db, user) is False
        fail.assert_awaited()
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE
        assert sub.end_date == old_end
        assert user.promo_offer_discount_percent == 15
        assert user.has_had_paid_subscription is False
        assert sub.pending_panel_expire_at is None
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('entry', ['monitor', 'topup'])
async def test_panel_rollback_does_not_lose_committed_ledger(postgres_database, monkeypatch, entry):
    from app.services import remnawave_retry_queue as retry

    monkeypatch.setattr(retry, 'remnawave_retry_queue', retry.RemnaWaveRetryQueue())
    async with postgres_session(postgres_database, TABLES) as db:
        user, sub, old_end = await _seed(db, datetime.now(UTC))
        if entry == 'topup':
            sub.status = 'expired'
            sub.end_date = old_end = datetime.now(UTC) - timedelta(minutes=30)
            sub.updated_at = datetime.now(UTC) - timedelta(days=1)
            await db.commit()

        async def unavailable(db, subscription, **kwargs):
            await db.rollback()  # actual panel service invalidates ORM on error

        service = worker()
        service.subscription_service.update_remnawave_user = unavailable
        if entry == 'monitor':
            await service._process_autopayments(db)
        else:
            monkeypatch.setattr(auto, 'SubscriptionService', lambda: service.subscription_service)
            assert await auto.try_auto_extend_expired_after_topup(db, user) is True
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE - PRICE
        assert sub.end_date > old_end + timedelta(days=29)
        assert sub.pending_panel_expire_at == sub.end_date
        assert len(list(await db.scalars(select(Transaction)))) == 1
        assert retry.remnawave_retry_queue.pending_count == 1


async def test_manual_charge_in_flight_blocks_autopay_for_same_cycle(postgres_database, monkeypatch):
    from app.services import subscription_renewal_service as renewal

    async with postgres_sessions(postgres_database, TABLES, count=3) as (manual, automatic, observer):
        user, sub, old_end = await _seed(manual, datetime.now(UTC))
        uid, sid = user.id, sub.id
        deducted, proceed = asyncio.Event(), asyncio.Event()
        original_charge = renewal.subtract_user_balance

        async def charge(db, *args, **kwargs):
            result = await original_charge(db, *args, **kwargs)
            if db is manual:
                deducted.set()
                await asyncio.wait_for(proceed.wait(), 10)
            return result

        monkeypatch.setattr(renewal, 'subtract_user_balance', charge)

        async def manual_request():
            await _renew_from_cabinet(manual, user, sub, monkeypatch)
            await manual.commit()

        task1 = asyncio.create_task(manual_request())
        task2 = None
        try:
            await asyncio.wait_for(deducted.wait(), 10)
            task2 = asyncio.create_task(worker()._process_autopayments(automatic))
            from tests.fixtures.postgres_db import lock_waiter_appeared

            await lock_waiter_appeared(observer, timeout=1)
        finally:
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*([task1, task2] if task2 else [task1])), 15)
        await observer.rollback()
        current = await observer.get(User, uid)
        subscription = await observer.get(type(sub), sid)
        assert current.balance_kopeks == BALANCE - PRICE
        assert subscription.end_date == old_end + timedelta(days=30)
        assert len(list(await observer.scalars(select(Transaction)))) == 1


async def test_recurring_ledger_fk_does_not_deadlock_with_autopay(postgres_database):
    from sqlalchemy import select, text

    from app.database.crud.transaction import create_transaction
    from app.database.models import Subscription, TransactionType
    from app.services.autopay_guard import lock_autopay_target
    from tests.fixtures.postgres_db import wait_for_lock_waiter

    async with postgres_sessions(postgres_database, TABLES, count=3) as (recurring, automatic, observer):
        user, sub, old_end = await _seed(recurring, datetime.now(UTC))
        uid, sid = user.id, sub.id
        await recurring.execute(text("SET deadlock_timeout = '100ms'"))
        await recurring.execute(select(Subscription).where(Subscription.id == sid).with_for_update())
        task = asyncio.create_task(lock_autopay_target(automatic, uid, sid, expected_end_date=old_end))
        try:
            await wait_for_lock_waiter(observer)
            # Recurring handlers lock Subscription then insert a Transaction;
            # the foreign key needs KEY SHARE on User, not a balance mutation.
            await create_transaction(
                recurring,
                uid,
                TransactionType.SUBSCRIPTION_PAYMENT,
                PRICE,
                description='Synthetic recurring confirmation',
                commit=False,
            )
            sub.end_date += timedelta(days=30)
            await recurring.commit()
            assert await asyncio.wait_for(task, 5) is None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_tariff_switch_does_not_deadlock_with_autopay(postgres_database, monkeypatch):
    from app.cabinet.routes.subscription_modules import tariff_switch as route
    from app.cabinet.schemas.subscription import TariffPurchaseRequest
    from app.services.autopay_guard import lock_autopay_target
    from tests.fixtures.postgres_db import lock_waiter_appeared

    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
    monkeypatch.setattr(route, 'SubscriptionService', lambda: SimpleNamespace(create_remnawave_user=AsyncMock()))
    async with postgres_sessions(postgres_database, TABLES, count=3) as (manual, automatic, observer):
        from sqlalchemy import text

        from app.database.models import Tariff

        user, sub, old_end = await _seed(manual, datetime.now(UTC))
        uid, sid = user.id, sub.id
        tariff = Tariff(
            name='Alternative',
            is_active=True,
            traffic_limit_gb=100,
            device_limit=3,
            period_prices={'30': PRICE},
            allowed_squads=[],
        )
        manual.add(tariff)
        await manual.commit()
        target_id = tariff.id
        await manual.execute(text("SET deadlock_timeout = '100ms'"))
        selected, proceed = asyncio.Event(), asyncio.Event()
        original_lock = user_crud.lock_user_for_pricing

        async def pause_manual(db, user_id):
            if db is manual:
                selected.set()
                await asyncio.wait_for(proceed.wait(), 10)
            return await original_lock(db, user_id)

        monkeypatch.setattr(user_crud, 'lock_user_for_pricing', pause_manual)
        switch_task = asyncio.create_task(
            route.switch_tariff(
                TariffPurchaseRequest(tariff_id=target_id, period_days=30),
                user,
                manual,
                sid,
            )
        )
        guard_task = None
        try:
            await asyncio.wait_for(selected.wait(), 5)
            guard_task = asyncio.create_task(lock_autopay_target(automatic, uid, sid, expected_end_date=old_end))
            await lock_waiter_appeared(observer, timeout=1)
            # If the guard acquired both locks, release them before finishing
            # switch. Old order instead leaves guard blocked on Subscription.
            if guard_task.done():
                await guard_task
                await automatic.commit()
            proceed.set()
            await asyncio.wait_for(switch_task, 5)
            await manual.commit()
            await asyncio.wait_for(guard_task, 5)
        finally:
            proceed.set()
            for task in (switch_task, guard_task):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*[t for t in (switch_task, guard_task) if t], return_exceptions=True)


@pytest.mark.parametrize('entry', ['monitor', 'topup'])
async def test_cancellation_rolls_back_uncommitted_autopay(postgres_database, monkeypatch, entry):
    from app.database.crud import transaction as transaction_crud

    async with postgres_session(postgres_database, TABLES) as db:
        user, sub, old_end = await _seed(db, datetime.now(UTC))
        if entry == 'topup':
            sub.status = 'expired'
            sub.end_date = old_end = datetime.now(UTC) - timedelta(minutes=30)
            sub.updated_at = datetime.now(UTC) - timedelta(days=1)
            await db.commit()
        pending = asyncio.Event()

        async def interrupted(*args, **kwargs):
            pending.set()
            await asyncio.Future()

        monkeypatch.setattr(transaction_crud if entry == 'monitor' else auto, 'create_transaction', interrupted)
        operation = (
            worker()._process_autopayments(db)
            if entry == 'monitor'
            else auto.try_auto_extend_expired_after_topup(db, user)
        )
        task = asyncio.create_task(operation)
        await asyncio.wait_for(pending.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.in_transaction(), 'Cancelled billing must release locks and rollback the charge'
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == BALANCE
        assert sub.end_date == old_end
