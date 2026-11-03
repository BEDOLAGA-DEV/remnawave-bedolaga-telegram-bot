"""A failed allowance write cannot leave a committed debit or nominal package."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.models import Subscription, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account
from tests.services.test_traffic_accounting_store import database


async def test_managed_topup_requires_snapshot_before_any_mutation(monkeypatch):
    from app.services.traffic_purchase_service import add_subscription_traffic

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 10
        sub.traffic_accounting_mode = 'managed'
        await db.commit()
        with pytest.raises(AccountingConflict):
            await add_subscription_traffic(db, sub, 5, commit=False)
        assert sub.traffic_limit_gb == 10


async def test_debit_and_grant_roll_back_together(monkeypatch):
    from app.database.crud.user import subtract_user_balance
    from app.services.traffic_accounting_service import lock_accounting_subscription
    from app.services.traffic_purchase_service import add_subscription_traffic

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        user = await db.get(User, 1)
        user.balance_kopeks = 1000
        sub.traffic_limit_gb = 10
        sub.traffic_accounting_mode = 'managed'
        now = datetime.now(UTC)
        state = await initialize_account(
            db,
            1,
            TrafficAccount('test', 0, 0, (Allowance('base:test', 10 * 1024**3, now + timedelta(days=30)),)),
            strategy='MONTH',
            timezone='UTC',
            anchor=now,
            next_refresh_at=now + timedelta(days=30),
        )
        state.phase = 'ready'
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.grant_purchase_locked',
            AsyncMock(side_effect=AccountingConflict('snapshot changed')),
        )
        assert await subtract_user_balance(db, user, 100, 'test', commit=False)
        with pytest.raises(AccountingConflict):
            await add_subscription_traffic(db, sub, 5, panel=object(), commit=False)
        await db.rollback()
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == 1000
        assert sub.traffic_limit_gb == 10


async def test_successful_grant_remains_uncommitted_until_caller_commits(monkeypatch):
    from sqlalchemy import select

    from app.database.models import TrafficAllowance, TrafficPurchase
    from app.services.traffic_accounting_service import lock_accounting_subscription, read_counters
    from app.services.traffic_purchase_service import add_subscription_traffic
    from tests.services.test_traffic_accounting_service import snapshot

    panel = snapshot(used=0, lifetime=0)
    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 10
        sub.traffic_accounting_mode = 'managed'
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
        hook = AsyncMock()
        monkeypatch.setattr('app.services.recurrent_amount.sync_recurrent_bindings_after_price_change', hook)
        sub, _ = await lock_accounting_subscription(db, 1)
        await add_subscription_traffic(db, sub, 5, panel=panel, commit=False)
        purchase = (await db.execute(select(TrafficPurchase))).scalar_one()
        grant = (
            await db.execute(
                select(TrafficAllowance).where(
                    TrafficAllowance.operation_key == f'purchase:{purchase.id}',
                )
            )
        ).scalar_one()
        assert grant.remaining_bytes == 5 * 1024**3
        assert sub.traffic_limit_gb == 15
        hook.assert_not_awaited()
        await db.rollback()
        assert (await db.execute(select(TrafficPurchase))).scalars().all() == []
        await db.refresh(sub)
        assert sub.traffic_limit_gb == 10


@pytest.mark.parametrize('changed_terms', [False, True])
async def test_prepare_migrates_before_lease_then_reads_snapshot(monkeypatch, changed_terms):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services.traffic_purchase_service import prepare_traffic_purchase

    order = []
    sub = SimpleNamespace(
        id=1,
        user_id=1,
        tariff_id=None,
        tariff=None,
        traffic_limit_gb=10,
        purchased_traffic_gb=0,
        end_date=None,
        status='active',
        is_trial=False,
    )
    panel = object()

    @asynccontextmanager
    async def client():
        yield object()

    async def migrate(*args, **kwargs):
        order.append('migration')

    async def lock(*args):
        order.append('lease')
        if changed_terms:
            sub.traffic_limit_gb = 20
        return sub, False

    async def read(*args):
        order.append('snapshot')
        return panel

    monkeypatch.setattr(
        'app.services.traffic_purchase_service.SubscriptionService', lambda: SimpleNamespace(get_api_client=client)
    )
    monkeypatch.setattr('app.services.traffic_accounting_service.ensure_managed_account', migrate)
    monkeypatch.setattr('app.services.traffic_accounting_service.lock_accounting_subscription', lock)
    monkeypatch.setattr('app.services.traffic_accounting_service._fresh_panel', read)
    if changed_terms:
        with pytest.raises(AccountingConflict, match='terms changed'):
            await prepare_traffic_purchase(SimpleNamespace(bind=None), sub)
        assert order == ['migration', 'lease']
    else:
        result = await prepare_traffic_purchase(SimpleNamespace(bind=None), sub)
        assert result == (sub, panel)
        assert order == ['migration', 'lease', 'snapshot']


@pytest.mark.parametrize('entrypoint', ['cabinet', 'miniapp', 'telegram', 'auto'])
@pytest.mark.parametrize('fail_grant', [False, True])
async def test_purchase_commits_debit_package_and_ledger_together(monkeypatch, fail_grant, entrypoint):
    from sqlalchemy import select

    from app.cabinet.routes.subscription_modules import traffic
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest
    from app.config import Settings
    from app.database.models import Tariff, TrafficPurchase, Transaction
    from app.services.traffic_accounting_service import lock_accounting_subscription, read_counters
    from tests.services.test_traffic_accounting_service import snapshot

    panel = snapshot(used=0, lifetime=0)
    now = datetime.now(UTC)
    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        user = await db.get(User, 1)
        user.balance_kopeks = 10000
        tariff = Tariff(
            id=1, name='Test', traffic_limit_gb=10, traffic_topup_enabled=True, traffic_topup_packages={'5': 100}
        )
        db.add(tariff)
        await db.flush()
        sub.tariff_id = 1
        sub.traffic_limit_gb = 10
        sub.traffic_accounting_mode = 'managed'
        sub.status = 'active'
        sub.is_trial = False
        sub.end_date = now + timedelta(days=29)
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

        async def prepare(db, subscription, **kwargs):
            locked, _ = await lock_accounting_subscription(db, subscription.id)
            return locked, panel

        monkeypatch.setattr('app.services.traffic_purchase_service.prepare_traffic_purchase', prepare)
        monkeypatch.setattr('app.services.traffic_purchase_service.finish_traffic_purchase', AsyncMock())
        monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
        monkeypatch.setattr(Settings, 'is_traffic_topup_enabled', lambda self: True)
        monkeypatch.setattr(Settings, 'get_traffic_topup_packages', lambda self: [{'gb': 5, 'price': 100}])
        monkeypatch.setattr(traffic, 'resolve_subscription', AsyncMock(return_value=sub))
        monkeypatch.setattr(traffic, 'should_create_panel_account', AsyncMock(return_value=False))
        monkeypatch.setattr(traffic, 'SubscriptionService', lambda: AsyncMock())
        monkeypatch.setattr(
            traffic, '_apply_addon_discount', lambda *args: {'discounted': 100, 'percent': 0, 'discount': 0}
        )
        monkeypatch.setattr(traffic.settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
        monkeypatch.setattr('app.services.yandex_offline_conv_service.store_cid_only', AsyncMock())
        from app.services.pricing_engine import PricingEngine

        monkeypatch.setattr(PricingEngine, 'calculate_traffic_discount', lambda *args: (100, 0, 0))
        monkeypatch.setattr('app.utils.pricing_utils.calculate_prorated_price', lambda *args: (100, 30))
        monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
        if entrypoint == 'cabinet':

            async def invoke():
                return await traffic.purchase_traffic(TrafficPurchaseRequest(gb=5), user=user, db=db, subscription_id=1)
        elif entrypoint == 'miniapp':
            from app.webapi.routes import miniapp
            from app.webapi.schemas.miniapp import MiniAppTrafficTopupRequest

            monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
            monkeypatch.setattr(miniapp, '_ensure_paid_subscription', lambda *args, **kwargs: sub)
            monkeypatch.setattr(miniapp, 'SubscriptionService', lambda: AsyncMock())

            async def invoke():
                return await miniapp.purchase_traffic_topup_endpoint(
                    MiniAppTrafficTopupRequest(initData='test', subscriptionId=1, gb=5),
                    db=db,
                )
        elif entrypoint == 'telegram':
            from app.handlers.subscription import traffic as telegram

            callback = AsyncMock()
            callback.data = 'add_traffic_5'
            monkeypatch.setattr(telegram, '_resolve_subscription', AsyncMock(return_value=(sub, 1)))
            monkeypatch.setattr(telegram, 'SubscriptionService', lambda: AsyncMock())
            monkeypatch.setattr(
                'app.services.admin_notification_service.AdminNotificationService', lambda *args: AsyncMock()
            )

            async def invoke():
                return await telegram.add_traffic(callback, user, db)
        else:
            from app.services import subscription_auto_purchase_service as auto

            monkeypatch.setattr(
                'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=sub)
            )
            monkeypatch.setattr(auto, 'SubscriptionService', lambda: AsyncMock())
            monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())

            async def invoke():
                return await auto._auto_add_traffic(
                    db, user, {'traffic_gb': 5, 'price_kopeks': 100, 'subscription_id': 1}
                )

        if fail_grant:
            monkeypatch.setattr(
                'app.services.traffic_accounting_service.grant_purchase_locked',
                AsyncMock(side_effect=AccountingConflict('snapshot changed')),
            )
            if entrypoint in ('cabinet', 'miniapp'):
                with pytest.raises(AccountingConflict):
                    await invoke()
                await db.rollback()
            else:
                await invoke()
        else:
            await invoke()
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == (10000 if fail_grant else 9900)
        assert sub.traffic_limit_gb == (10 if fail_grant else 15)
        purchases = (await db.execute(select(TrafficPurchase))).scalars().all()
        payments = (await db.execute(select(Transaction))).scalars().all()
        assert len(purchases) == len(payments) == (0 if fail_grant else 1)


@pytest.mark.parametrize(
    'mode,status,expected',
    [
        ('managed', 'disabled', 'disabled'),
        ('migrating', 'disabled', 'disabled'),
        ('managed', 'limited', 'active'),
        ('legacy', 'disabled', 'active'),
    ],
)
async def test_topup_reactivation_preserves_managed_administrative_disable(mode, status, expected):
    from types import SimpleNamespace

    from app.database.crud.subscription import reactivate_subscription

    sub = SimpleNamespace(
        id=1, user_id=1, traffic_accounting_mode=mode, status=status, end_date=datetime.now(UTC) + timedelta(days=1)
    )
    await reactivate_subscription(AsyncMock(), sub, commit=False)
    assert sub.status == expected
