"""User tariff switches commit the debit and package retirement in one transaction."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Subscription, Tariff, TrafficPurchase, Transaction, User
from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount
from app.services.traffic_accounting_service import read_counters
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_store import database


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
@pytest.mark.parametrize('reset_enabled,cost', [(True, 100), (False, 100), (True, 0)])
@pytest.mark.parametrize('target_limit', [20, 0])
async def test_managed_switch_retires_packages_using_existing_reset_policy(
    monkeypatch, endpoint, reset_enabled, cost, target_limit, multi
):
    async with switch_context(monkeypatch, endpoint, target_limit, cost, reset_enabled, multi=multi) as (db, invoke):
        await invoke()
        sub = await db.get(Subscription, 1)
        user = await db.get(User, 1)
        account, state = await load_account(db, 1)
        assert sub.tariff_id == 2
        assert user.balance_kopeks == 1000 - cost
        assert (await db.scalars(select(TrafficPurchase))).all() == []
        assert len((await db.scalars(select(Transaction))).all()) == 1
        if target_limit:
            assert state.phase == 'ready'
            expected = 20 if reset_enabled and cost > 0 else 15
            assert account.remaining_bytes(datetime.now(UTC)) == expected * 1024**3
        else:
            assert state.phase == 'unlimited'
            assert account.remaining_bytes(datetime.now(UTC)) == 0
        assert all(a.remaining_bytes == 0 for a in account.allowances if a.key.startswith('purchase:'))
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as replay:
            await invoke()
        assert replay.value.status_code == 400
        assert len((await db.scalars(select(Transaction))).all()) == 1
        await db.refresh(user)
        assert user.balance_kopeks == 1000 - cost


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
async def test_switch_grant_failure_rolls_back_money_and_keeps_packages(monkeypatch, endpoint):
    async with switch_context(monkeypatch, endpoint, 20, 100, True) as (db, invoke):
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.change_managed_tariff_locked',
            AsyncMock(side_effect=AccountingConflict('ledger failure')),
        )
        with pytest.raises(AccountingConflict):
            await invoke()
        await db.rollback()
        user = await db.get(User, 1)
        sub = await db.get(Subscription, 1)
        assert user.balance_kopeks == 1000
        assert sub.tariff_id == 1
        assert len((await db.scalars(select(TrafficPurchase))).all()) == 1
        assert (await db.scalars(select(Transaction))).all() == []


from contextlib import asynccontextmanager


@asynccontextmanager
async def switch_context(monkeypatch, endpoint, target_limit, cost, reset_enabled, *, multi=False):
    from app.cabinet.routes.subscription_modules import tariff_switch as cabinet
    from app.cabinet.schemas.subscription import TariffPurchaseRequest
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppTariffSwitchRequest

    now = datetime.now(UTC)
    panel = SimpleNamespace(
        id=123,
        created_at=now,
        traffic_limit_strategy='NO_RESET',
        user_traffic=SimpleNamespace(used_traffic_bytes=0, lifetime_used_traffic_bytes=0),
    )
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', reset_enabled)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    result = SimpleNamespace(
        upgrade_cost=cost,
        is_upgrade=cost > 0,
        raw_cost=cost,
        discount_value=0,
        effective_discount_pct=0,
        new_period_days=30,
        offer_discount_pct=0,
    )
    monkeypatch.setattr(cabinet.pricing_engine, 'calculate_tariff_switch_cost', lambda *args, **kwargs: result)
    monkeypatch.setattr(miniapp, '_calculate_tariff_switch', lambda *args, **kwargs: result)
    monkeypatch.setattr('app.services.checkout_squad_validation.validate_checkout_squads', AsyncMock())
    monkeypatch.setattr('app.services.traffic_accounting_service._fresh_panel', AsyncMock(return_value=panel))
    monkeypatch.setattr('app.services.yandex_offline_conv_service.store_cid_only', AsyncMock())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    for path in [
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        'app.services.payment.lava.cancel_lava_recurring_for_subscription_safe',
        'app.services.cashera_recurring_cancel.cancel_cashera_recurring_for_subscription_safe',
    ]:
        monkeypatch.setattr(path, AsyncMock())

    sync = AsyncMock()

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            yield SimpleNamespace(
                reset_user_devices=AsyncMock(return_value=True), update_user=AsyncMock(return_value=panel)
            )

        async def update_remnawave_user(self, *args, **kwargs):
            await sync(*args, **kwargs)

        async def create_remnawave_user(self, *args, **kwargs):
            return None

    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', Service)
    monkeypatch.setattr('app.services.traffic_purchase_service.SubscriptionService', Service)
    monkeypatch.setattr(cabinet, 'SubscriptionService', Service)
    monkeypatch.setattr(cabinet, 'RemnaWaveService', Service)
    monkeypatch.setattr(miniapp, 'SubscriptionService', Service)
    async with database(monkeypatch) as db:
        user = await db.get(User, 1)
        user.balance_kopeks = 1000
        user.status = 'active'
        user.remnawave_id = 123
        db.add_all(
            [
                Tariff(
                    id=1, name='Old', is_active=True, traffic_limit_gb=10, device_limit=2, period_prices={'30': 100}
                ),
                Tariff(
                    id=2,
                    name='New',
                    is_active=True,
                    traffic_limit_gb=target_limit,
                    device_limit=2,
                    period_prices={'30': 200},
                ),
            ]
        )
        await db.flush()
        sub = await db.get(Subscription, 1)
        sub.tariff_id = 1
        sub.status = 'active'
        sub.is_trial = False
        sub.end_date = now + timedelta(days=20)
        sub.traffic_limit_gb = 12
        sub.purchased_traffic_gb = 2
        sub.traffic_accounting_mode = 'managed'
        sub.device_limit = 2
        sub.remnawave_id = 123
        purchase = TrafficPurchase(subscription_id=1, traffic_gb=2, expires_at=now + timedelta(days=30))
        db.add(purchase)
        await db.flush()
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                read_counters(panel).identity,
                0,
                0,
                (
                    Allowance('base:initial', 5 * 1024**3, now + timedelta(days=30), 10 * 1024**3),
                    Allowance(f'purchase:{purchase.id}', 2 * 1024**3, now + timedelta(days=30)),
                ),
            ),
            strategy='MONTH',
            timezone='UTC',
            anchor=now,
            next_refresh_at=now + timedelta(days=30),
        )
        state.phase = 'ready'
        state.base_budget_bytes = 10 * 1024**3
        await db.commit()
        monkeypatch.setattr(cabinet, '_resolve_switch_subscription', AsyncMock(return_value=sub))
        monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(user, 'subscriptions', [sub])

        async def invoke():
            if endpoint == 'cabinet':
                return await cabinet.switch_tariff(TariffPurchaseRequest(tariff_id=2, period_days=30), user, db, 1)
            return await miniapp.switch_tariff_endpoint(
                MiniAppTariffSwitchRequest(init_data='test', tariff_id=2, subscriptionId=1), db
            )

        invoke.panel = panel
        yield db, invoke
        for call in sync.await_args_list:
            assert not call.kwargs.get('reset_traffic', False)


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp'])
async def test_legacy_source_with_package_migrates_before_switch_charge(monkeypatch, endpoint):
    from sqlalchemy import delete

    from app.database.models import TrafficAccountingState, TrafficAllowance

    async with switch_context(monkeypatch, endpoint, 20, 100, True) as (db, invoke):
        await db.execute(delete(TrafficAllowance))
        await db.execute(delete(TrafficAccountingState))
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'legacy'
        await db.commit()
        invoke.panel.traffic_limit_bytes = 12 * 1024**3
        invoke.panel.user_traffic.used_traffic_bytes = 5 * 1024**3
        invoke.panel.user_traffic.lifetime_used_traffic_bytes = 5 * 1024**3
        await invoke()
        await db.refresh(sub)
        assert sub.traffic_accounting_mode == 'managed'
        account, state = await load_account(db, 1)
        assert state.phase == 'ready'
        assert account.remaining_bytes(datetime.now(UTC)) == 20 * 1024**3
        assert (await db.scalars(select(TrafficPurchase))).all() == []
        assert len((await db.scalars(select(Transaction))).all()) == 1
