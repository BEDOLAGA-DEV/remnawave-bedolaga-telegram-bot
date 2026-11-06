"""Destructive cleanup must not prevent a later atomic paid purchase."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import Subscription, Tariff, TrafficBaseGrantIntent, TrafficPurchase, TransactionType, User
from app.services.remnawave_service import RemnaWaveService
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_lifecycle import retire_managed_account
from app.services.traffic_accounting_service import lock_accounting_subscription, read_counters
from app.services.traffic_accounting_store import initialize_account, load_account
from app.services.traffic_accounting_worker import TrafficAccountingWorker
from tests.services.test_traffic_accounting_store import database


class Panel:
    def __init__(self, now):
        self.now = now
        self.account = SimpleNamespace(
            id=123,
            created_at=now,
            username='old',
            status='ACTIVE',
            expire_at=now + timedelta(days=30),
            traffic_limit_strategy='NO_RESET',
            traffic_limit_bytes=100,
            user_traffic=SimpleNamespace(used_traffic_bytes=30, lifetime_used_traffic_bytes=100),
        )
        self.creates = 0
        self.writes = []

    async def reset_user_devices(self, _id):
        return True

    async def get_user_devices_all(self, _id):
        return {'devices': [], 'total': 0}

    async def get_user_by_id(self, ident):
        return self.account if self.account and self.account.id == ident else None

    async def get_user_by_username(self, username):
        return self.account if self.account and self.account.username == username else None

    async def update_user(self, **kwargs):
        self.writes.append(kwargs)
        for key, value in kwargs.items():
            if key != 'user_id':
                setattr(self.account, key, getattr(value, 'value', value))
        return self.account

    async def create_user(self, **kwargs):
        self.creates += 1
        assert kwargs['status'].value == 'DISABLED'
        # Cleanup retired all old grants. Creation must not revive their quota.
        assert kwargs['traffic_limit_bytes'] == getattr(self, 'expected_initial_limit', 1)
        self.account = SimpleNamespace(
            id=456,
            created_at=self.now,
            user_traffic=SimpleNamespace(used_traffic_bytes=0, lifetime_used_traffic_bytes=0),
            **kwargs,
        )
        return self.account


@asynccontextmanager
async def repurchase_context(monkeypatch, *, missing, same_tariff=False):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'NO_RESET')
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    now = datetime.now(UTC)
    api = Panel(now)

    @asynccontextmanager
    async def client():
        yield api

    async with database(monkeypatch) as db:
        user = await db.get(User, 1)
        user.balance_kopeks = 1000
        user.status = 'active'
        user.remnawave_id = 123
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_accounting_mode = 'managed'
        sub.status = 'active'
        sub.is_trial = False
        sub.end_date = now + timedelta(days=30)
        sub.traffic_limit_gb = 1
        sub.purchased_traffic_gb = 1
        purchase_row = TrafficPurchase(subscription_id=1, traffic_gb=1, expires_at=sub.end_date)
        db.add(purchase_row)
        await db.flush()
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                read_counters(api.account).identity,
                100,
                30,
                (Allowance(f'purchase:{purchase_row.id}', 70, sub.end_date, 1024**3),),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        db.add(Tariff(id=7, name='New', traffic_limit_gb=1, device_limit=1, period_prices={'30': 100}))
        await db.commit()
        if same_tariff:
            sub.tariff_id = 7
            await db.commit()
        set_committed_value(user, 'subscriptions', [sub])
        service = RemnaWaveService()
        monkeypatch.setattr(service, 'get_api_client', client)
        assert await service.force_cleanup_user_data(db, user) is True
        assert sub.purchased_traffic_gb == 0
        assert list(await db.scalars(select(TrafficPurchase))) == []
        # Some cleanup paths already clear the subscription link too. Exercise
        # that stricter state as well, never rely on a dangling numeric ID.
        sub.remnawave_id = None
        await db.commit()
        assert user.remnawave_id is None
        if missing:
            api.account = None

        class Service:
            get_api_client = staticmethod(client)

        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        worker = TrafficAccountingWorker(factory, Service)
        yield db, api, worker, now


async def purchase(db, *, limit=1):
    sub, _ = await lock_accounting_subscription(db, 1)
    user = sub.user
    assert await subtract_user_balance(db, user, 100, 'New subscription', commit=False)
    await create_transaction(
        db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=100,
        description='New subscription',
        commit=False,
    )
    await extend_subscription(
        db,
        sub,
        30,
        tariff_id=7,
        traffic_limit_gb=limit,
        reset_used_traffic=False,
        paid_traffic_reset=False,
        commit=False,
    )


@pytest.mark.parametrize('missing', [False, True])
async def test_cleanup_then_paid_purchase_is_delivered_by_worker(monkeypatch, missing):
    async with repurchase_context(monkeypatch, missing=missing) as (db, api, worker, now):
        await purchase(db)
        await db.commit()
        assert await worker.run_once(now=now + timedelta(seconds=5)) == 1
        await db.rollback()
        account, state = await load_account(db, 1)
        assert state.phase == 'ready'
        assert state.delivery_failures == 0
        assert account.remaining_bytes(now + timedelta(seconds=5)) == 1024**3
        assert next(a for a in account.allowances if a.key.startswith('purchase:')).remaining_bytes == 0
        assert (await db.get(User, 1)).balance_kopeks == 900
        assert (await db.get(Subscription, 1, populate_existing=True)).remnawave_id == (456 if missing else 123)
        assert api.account.status == 'ACTIVE'
        assert api.creates == int(missing)
        assert len(list(await db.scalars(select(TrafficBaseGrantIntent)))) == 1


@pytest.mark.parametrize('later_cleanup', [False, True])
async def test_uncommitted_or_retired_purchase_cannot_resume_recovery(monkeypatch, later_cleanup):
    async with repurchase_context(monkeypatch, missing=True) as (db, api, worker, now):
        await purchase(db)
        if later_cleanup:
            await db.commit()
            await retire_managed_account(db, 1)
            await db.commit()
        else:
            await db.rollback()
        assert await worker.run_once(now=now + timedelta(seconds=5)) == 0
        # A job selected while the paid purchase was ready may arrive only
        # after cleanup committed. Its stale phase must not authorize recreation.
        await worker._process_candidate(1, 'ready', now=now + timedelta(seconds=5))
        assert api.creates == 0
        _, state = await load_account(db, 1)
        assert state.phase == 'suspended'


@pytest.mark.parametrize('conflict', ['epoch', 'owner'])
async def test_paid_link_recovery_refuses_foreign_or_reused_account(monkeypatch, conflict):
    async with repurchase_context(monkeypatch, missing=False) as (db, api, worker, now):
        await purchase(db)
        await db.commit()
        if conflict == 'epoch':
            api.account.created_at += timedelta(days=1)
        else:
            db.add(User(id=2, status='active', remnawave_id=123))
            await db.commit()
        await worker.run_once(now=now + timedelta(seconds=5))
        assert api.writes == []
        assert api.creates == 0
        await db.rollback()
        _, state = await load_account(db, 1)
        assert state.delivery_failures == 1


async def test_cleanup_then_same_unlimited_tariff_conversion_rearms_recovery(monkeypatch):
    async with repurchase_context(monkeypatch, missing=True, same_tariff=True) as (db, api, worker, now):
        await purchase(db, limit=0)
        await db.commit()
        await worker.run_once(now=now + timedelta(seconds=5))
        await db.rollback()
        account, state = await load_account(db, 1)
        assert state.phase == 'unlimited'
        assert state.delivery_failures == 0
        assert account.remaining_bytes(now) == 0
        assert api.account.traffic_limit_bytes == 0
        assert api.account.status == 'ACTIVE'
        assert api.creates == 1


async def test_admin_classic_purchase_after_cleanup_rearms_zero_limit(monkeypatch):
    import inspect
    from unittest.mock import AsyncMock

    from app.handlers.admin import users

    async with repurchase_context(monkeypatch, missing=True) as (db, api, worker, now):
        user = await db.get(User, 1)
        sub = await db.get(Subscription, 1)
        assert sub.traffic_limit_gb == 0
        assert sub.tariff_id is None
        monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
        monkeypatch.setattr(
            users.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': sub})
        )
        monkeypatch.setattr(users, '_admin_buy_periods', AsyncMock(return_value=[30]))
        monkeypatch.setattr(users, '_calculate_subscription_period_price', AsyncMock(return_value=100))
        monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())

        @asynccontextmanager
        async def unavailable(_self):
            # Prove paid intent survives losing synchronous post-commit delivery.
            raise TimeoutError('temporary panel outage')
            yield

        monkeypatch.setattr(RemnaWaveService, 'get_api_client', unavailable)
        callback = SimpleNamespace(
            data='admin_buy_sub_execute_1_30_100',
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            bot=None,
        )
        await inspect.unwrap(users.admin_buy_subscription_execute)(callback, user, db)
        await db.rollback()
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 900
        await db.rollback()
        await worker.run_once(now=now + timedelta(seconds=5))
        account, state = await load_account(db, 1)
        assert state.phase == 'unlimited'
        assert state.delivery_failures == 0
        assert api.account.status == 'ACTIVE'
        assert account.remaining_bytes(now) == 0


async def test_paid_same_limit_renewal_rearms_planned_suspension(monkeypatch):
    async with repurchase_context(monkeypatch, missing=True, same_tariff=True) as (db, api, worker, now):
        sub = await db.get(Subscription, 1)
        # Planned deletion can leave a paid subscription with unchanged terms.
        sub.is_trial = False
        sub.traffic_limit_gb = 1
        from app.services.traffic_accounting_store import save_account

        # A planned deletion preserves already paid remainder, unlike cleanup.
        # Seed that checkpoint: the existing base is mostly spent, not retired.
        account, state = await load_account(db, 1)
        retained = account.add(Allowance('base:retained', 70, now + timedelta(days=30), 1024**3))
        await save_account(db, 1, retained, expected_revision=state.revision)
        state.base_budget_bytes = 1024**3
        api.expected_initial_limit = 70
        await db.commit()
        await purchase(db, limit=1)
        await db.commit()
        await worker.run_once(now=now + timedelta(seconds=5))
        account, state = await load_account(db, 1)
        assert state.phase == 'ready'
        assert state.delivery_failures == 0
        assert api.account.status == 'ACTIVE'
        assert account.remaining_bytes(now) == 70


async def test_bonus_days_do_not_rearm_retired_zero_limit(monkeypatch):
    async with repurchase_context(monkeypatch, missing=True) as (db, api, worker, now):
        sub, _ = await lock_accounting_subscription(db, 1)
        await extend_subscription(db, sub, 3, commit=False)
        await db.commit()
        assert await worker.run_once(now=now + timedelta(seconds=5)) == 0
        account, state = await load_account(db, 1)
        assert state.phase == 'suspended'
        assert state.activation_requested is False
        assert account.remaining_bytes(now) == 0
        assert list(await db.scalars(select(TrafficBaseGrantIntent))) == []
        assert (await db.get(User, 1)).balance_kopeks == 1000
        assert api.creates == 0
