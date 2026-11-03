"""Real PostgreSQL contention keeps debit, purchase and allowance indivisible."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import Base, Subscription, TrafficPurchase, Transaction, TransactionType, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount
from app.services.traffic_accounting_service import lock_accounting_subscription, read_counters
from app.services.traffic_accounting_store import initialize_account, load_account
from app.services.traffic_purchase_service import add_subscription_traffic
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres


async def seed(db):
    now = datetime.now(UTC)
    panel = SimpleNamespace(
        id=123,
        created_at=now,
        traffic_limit_strategy='NO_RESET',
        user_traffic=SimpleNamespace(used_traffic_bytes=0, lifetime_used_traffic_bytes=0),
    )
    db.add(User(id=1, balance_kopeks=10000, remnawave_id=123))
    db.add(
        Subscription(
            id=1,
            user_id=1,
            start_date=now,
            end_date=now + timedelta(days=30),
            status='active',
            is_trial=False,
            traffic_limit_gb=10,
            traffic_accounting_mode='managed',
        )
    )
    await db.flush()
    state = await initialize_account(
        db,
        1,
        TrafficAccount(
            read_counters(panel).identity, 0, 0, (Allowance('base:test', 10 * 1024**3, now + timedelta(days=30)),)
        ),
        strategy='MONTH',
        timezone='UTC',
        anchor=now,
        next_refresh_at=now + timedelta(days=30),
    )
    state.phase = 'ready'
    await db.commit()
    return panel


async def stage_purchase(db, panel, gb):
    sub, _ = await lock_accounting_subscription(db, 1)
    user = await db.get(User, 1)
    assert await subtract_user_balance(db, user, 100, 'test traffic', commit=False)
    await add_subscription_traffic(db, sub, gb, panel=panel, commit=False)
    await create_transaction(db, 1, TransactionType.SUBSCRIPTION_PAYMENT, 100, 'test traffic', commit=False)


@pytest.mark.parametrize('failure', ['none', 'ledger', 'stale_request'])
async def test_parallel_purchase_never_loses_quota_or_partially_charges(postgres_database, monkeypatch, failure):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        first,
        second,
        observer,
    ):
        panel = await seed(first)
        # The second request has already read the old terms, before the first charge.
        stale = await second.get(Subscription, 1)
        await stage_purchase(first, panel, 5)
        if failure == 'ledger':
            from app.services.traffic_accounting_service import grant_purchase_locked

            async def fail_second_grant(db, subscription, purchase, panel, **kwargs):
                if purchase.traffic_gb == 7:
                    raise AccountingConflict('injected ledger failure')
                return await grant_purchase_locked(db, subscription, purchase, panel, **kwargs)

            monkeypatch.setattr('app.services.traffic_accounting_service.grant_purchase_locked', fail_second_grant)

        @asynccontextmanager
        async def api_client(self):
            yield SimpleNamespace(get_user_by_id=AsyncMock(return_value=panel))

        monkeypatch.setattr('app.services.subscription_service.SubscriptionService.get_api_client', api_client)

        async def second_purchase():
            try:
                if failure == 'stale_request':
                    from app.services.traffic_purchase_service import prepare_traffic_purchase

                    await prepare_traffic_purchase(second, stale)
                await stage_purchase(second, panel, 7)
                await second.commit()
                return True
            except AccountingConflict:
                await second.rollback()
                return False

        task = asyncio.create_task(second_purchase())
        try:
            assert await lock_waiter_appeared(observer, timeout=3)
            await first.commit()
            assert await asyncio.wait_for(task, 10) is (failure == 'none')
            await observer.rollback()
            user = await observer.get(User, 1)
            sub = await observer.get(Subscription, 1)
            account, _ = await load_account(observer, 1)
            assert user.balance_kopeks == (9800 if failure == 'none' else 9900)
            assert sub.traffic_limit_gb == (22 if failure == 'none' else 15)
            assert account.remaining_bytes(datetime.now(UTC)) == sub.traffic_limit_gb * 1024**3
            expected_count = 2 if failure == 'none' else 1
            assert await observer.scalar(select(func.count()).select_from(TrafficPurchase)) == expected_count
            assert await observer.scalar(select(func.count()).select_from(Transaction)) == expected_count
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_telegram_accounting_failure_preserves_language_after_rollback(postgres_database, monkeypatch, language):
    from app.config import settings
    from app.handlers.subscription import traffic
    from app.localization.texts import get_texts

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        await seed(db)
        user = await db.get(User, 1)
        user.language = language
        await db.commit()
        subscription = await db.get(Subscription, 1)
        monkeypatch.setattr(traffic, '_resolve_subscription', AsyncMock(return_value=(subscription, 1)))
        monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
        monkeypatch.setattr(type(settings), 'is_traffic_topup_blocked', lambda self: False)
        monkeypatch.setattr(type(settings), 'get_traffic_topup_price', lambda self, gb: 100)
        monkeypatch.setattr(
            'app.services.traffic_purchase_service.prepare_traffic_purchase',
            AsyncMock(side_effect=RuntimeError('synthetic accounting failure')),
        )
        callback = SimpleNamespace(data='add_traffic_5', answer=AsyncMock())
        await traffic.add_traffic(callback, user, db)
        callback.answer.assert_awaited_once_with(
            get_texts(language).t('SUBSCRIPTION_UI_ACCOUNTING_UNAVAILABLE'), show_alert=True
        )
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert not (await db.scalars(select(Transaction))).all()
