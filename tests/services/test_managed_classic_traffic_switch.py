"""Classic traffic changes keep each entry point's package policy atomically."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.database.models import Subscription, TrafficPurchase, Transaction, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount
from app.services.traffic_accounting_delivery import deliver_account_projection
from app.services.traffic_accounting_service import read_counters
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_store import database


GB = 1024**3


@asynccontextmanager
async def switch_context(monkeypatch, endpoint, target=100):
    from app.cabinet.routes.subscription_modules import traffic as cabinet
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppSubscriptionTrafficUpdateRequest

    now = datetime.now(UTC)
    panel = SimpleNamespace(
        id=123,
        created_at=now,
        traffic_limit_strategy='NO_RESET',
        traffic_limit_bytes=80 * GB,
        status='ACTIVE',
        expire_at=now + timedelta(days=30),
        user_traffic=SimpleNamespace(used_traffic_bytes=20 * GB, lifetime_used_traffic_bytes=20 * GB),
    )
    api = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=panel),
        update_user=AsyncMock(return_value=panel),
        get_user_devices_all=AsyncMock(return_value={'devices': [], 'total': 0}),
    )

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            yield api

        update_remnawave_user = AsyncMock(return_value=panel)
        create_remnawave_user = AsyncMock(return_value=panel)

    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'NO_RESET')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(
        type(settings),
        'get_traffic_packages',
        lambda *_: [
            {'gb': 80, 'price': 1000, 'enabled': True},
            {'gb': 100, 'price': 2000, 'enabled': True},
            {'gb': 50, 'price': 500, 'enabled': True},
            {'gb': 0, 'price': 3000, 'enabled': True},
        ],
    )
    monkeypatch.setattr(type(settings), 'get_traffic_price', lambda _, gb: {80: 1000, 100: 2000, 50: 500, 0: 3000}[gb])
    monkeypatch.setattr(type(settings), 'is_traffic_topup_blocked', lambda *_: False)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', Service)
    monkeypatch.setattr('app.services.traffic_purchase_service.SubscriptionService', Service)
    monkeypatch.setattr(cabinet, 'SubscriptionService', Service)
    monkeypatch.setattr(cabinet, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(miniapp, 'SubscriptionService', Service)
    monkeypatch.setattr(miniapp, 'with_admin_notification_service', AsyncMock())
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    monkeypatch.setattr('app.services.traffic_purchase_service.finish_traffic_purchase', AsyncMock())
    async with database(monkeypatch) as db:
        user = await db.get(User, 1)
        user.balance_kopeks = 100_000
        user.status = 'active'
        user.remnawave_id = 123
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        sub.is_trial = False
        sub.status = 'active'
        sub.traffic_limit_gb = 80
        sub.purchased_traffic_gb = 20
        sub.end_date = panel.expire_at
        purchase = TrafficPurchase(subscription_id=1, traffic_gb=20, expires_at=panel.expire_at)
        db.add(purchase)
        await db.flush()
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                read_counters(panel).identity,
                20 * GB,
                20 * GB,
                (
                    Allowance('base:initial', 40 * GB, panel.expire_at, 60 * GB),
                    Allowance(f'purchase:{purchase.id}', 20 * GB, panel.expire_at),
                ),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        state.base_budget_bytes = 60 * GB
        await db.commit()
        set_committed_value(user, 'subscriptions', [sub])
        monkeypatch.setattr(cabinet, 'resolve_subscription', AsyncMock(return_value=sub))
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))

        async def invoke():
            if endpoint == 'cabinet':
                return await cabinet.switch_traffic_package(TrafficPurchaseRequest(gb=target), user, db, 1)
            return await miniapp.update_subscription_traffic_endpoint(
                MiniAppSubscriptionTrafficUpdateRequest(init_data='test', traffic=target, subscriptionId=1), db
            )

        yield db, invoke, api, now


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
@pytest.mark.parametrize('target', [100, 50, 0])
async def test_classic_switch_preserves_spent_base_and_existing_package_policy(monkeypatch, endpoint, target):
    async with switch_context(monkeypatch, endpoint, target) as (db, invoke, api, now):
        await invoke()
        await db.rollback()  # release read transaction before owned delivery
        await deliver_account_projection(async_sessionmaker(db.bind, expire_on_commit=False), api, 1, now=now)
        account, state = await load_account(db, 1)
        sub = await db.get(Subscription, 1)
        user = await db.get(User, 1)
        assert sub.traffic_limit_gb == target
        assert (user.balance_kopeks < 100_000) == (target in (100, 0))
        assert len(list(await db.scalars(select(Transaction)))) == int(target in (100, 0))
        purchases = list(await db.scalars(select(TrafficPurchase)))
        assert len(purchases) == int(endpoint == 'miniapp')
        if target:
            assert state.phase == 'ready'
            assert account.remaining_bytes(now) == (target - 20) * GB
            expected_package = 20 * GB if endpoint == 'miniapp' else 0
            assert (
                sum(a.remaining_bytes for a in account.allowances if a.key.startswith('purchase:')) == expected_package
            )
        else:
            assert state.phase == 'unlimited'
            assert account.remaining_bytes(now) == 0


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
async def test_classic_switch_ledger_failure_rolls_back_debit_and_rights(monkeypatch, endpoint):
    async with switch_context(monkeypatch, endpoint) as (db, invoke, _api, _now):
        target = (
            'app.services.traffic_accounting_service.change_managed_tariff_locked'
            if endpoint == 'cabinet'
            else 'app.services.traffic_base_grant_service.stage_paid_base_grant'
        )
        monkeypatch.setattr(target, AsyncMock(side_effect=AccountingConflict('injected ledger failure')))
        with pytest.raises(AccountingConflict):
            await invoke()
        await db.rollback()
        assert (await db.get(User, 1)).balance_kopeks == 100_000
        assert (await db.get(Subscription, 1)).traffic_limit_gb == 80
        assert len(list(await db.scalars(select(TrafficPurchase)))) == 1
        assert list(await db.scalars(select(Transaction))) == []
