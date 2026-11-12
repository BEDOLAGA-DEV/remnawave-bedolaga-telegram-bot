"""Every managed checkout must persist its base intent in the money transaction."""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Subscription, Transaction, User
from app.services import traffic_base_grant_service as grants
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account
from tests.database.test_multi_tariff_switch_postgres import switch_data


pytestmark = pytest.mark.postgres


async def _manage(db, sub):
    now = datetime.now(UTC)
    sub.traffic_accounting_mode = 'managed'
    sub.connected_squads = ['test-squad']
    state = await initialize_account(
        db,
        sub.id,
        TrafficAccount('masked-panel', 10, 10, (Allowance('base:old', 1024**3, now + timedelta(days=30)),)),
        strategy='NO_RESET',
        timezone='UTC',
        anchor=now,
        next_refresh_at=None,
    )
    state.phase = 'ready'
    state.base_budget_bytes = sub.traffic_limit_gb * 1024**3
    await db.commit()


@pytest.mark.parametrize(
    ('flow', 'cancel'),
    [
        ('simple-direct', False),
        ('simple-confirm', False),
        ('classic-confirm', False),
        ('classic-trial', False),
        ('classic-legacy-trial', False),
        ('admin-buy', False),
        ('cabinet', False),
        ('autocart', False),
        ('miniapp', False),
        ('simple-direct', True),
        ('simple-confirm', True),
        ('classic-legacy-trial', True),
    ],
)
async def test_managed_checkout_intent_failure_rolls_back_money_and_period(
    postgres_database, monkeypatch, flow, cancel
):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        sub = await db.get(Subscription, 11)
        if flow != 'classic-legacy-trial':
            await _manage(db, sub)
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
        # Single-subscription entry points must resolve this exact account.
        await db.delete(await db.get(Subscription, 12))
        await db.commit()
        old_end, old_balance = sub.end_date, user.balance_kopeks
        failure = AsyncMock(side_effect=asyncio.CancelledError() if cancel else RuntimeError('intent staging failure'))
        if flow == 'classic-legacy-trial':
            monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', failure)
        else:
            monkeypatch.setattr(grants, 'stage_paid_base_grant', failure)
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()), bot=None)
        if flow.startswith('simple'):
            from app.handlers import simple_subscription as simple

            monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
            monkeypatch.setattr(settings, 'SIMPLE_SUBSCRIPTION_ENABLED', True)
            monkeypatch.setattr(simple, '_ensure_simple_subscription_squad_uuid', AsyncMock(return_value='test-squad'))
            monkeypatch.setattr(simple, '_calculate_simple_subscription_price', AsyncMock(return_value=(300, {})))
            sub.tariff_id = None
            sub.end_date = datetime.now(UTC) - timedelta(days=1)
            sub.status = 'expired'
            await db.commit()
            old_end = sub.end_date
            state = SimpleNamespace(
                get_data=AsyncMock(
                    return_value={
                        'subscription_params': {
                            'period_days': 30,
                            'traffic_limit_gb': 100,
                            'device_limit': 2,
                            'squad_uuid': 'test-squad',
                        }
                    }
                ),
                clear=AsyncMock(),
            )
            handler = (
                simple.confirm_simple_subscription_purchase
                if flow == 'simple-confirm'
                else simple.handle_simple_subscription_pay_with_balance
            )
            operation = handler(callback, user, state, db)
        elif flow in ('classic-confirm', 'classic-trial', 'classic-legacy-trial'):
            from app.handlers.subscription import purchase as classic
            from app.services.pricing_engine import PricingEngine, pricing_engine

            if flow in ('classic-trial', 'classic-legacy-trial'):
                sub.is_trial = True
                await db.commit()
            monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
            monkeypatch.setattr(classic, 'save_subscription_checkout_draft', AsyncMock())
            monkeypatch.setattr(
                pricing_engine,
                'calculate_classic_new_subscription_price',
                AsyncMock(return_value=SimpleNamespace(final_total=300, promo_offer_discount=0, breakdown={})),
            )
            details = dict.fromkeys(
                [
                    'base_price_original',
                    'base_price',
                    'base_discount_total',
                    'base_discount_percent',
                    'total_traffic_price',
                    'total_servers_price',
                    'total_devices_price',
                ],
                0,
            )
            details.update(servers_individual_prices=[], months_in_period=1)
            monkeypatch.setattr(PricingEngine, 'classic_pricing_to_purchase_details', lambda _: details)
            state = SimpleNamespace(
                get_data=AsyncMock(
                    return_value={
                        'period_days': 30,
                        'traffic_gb': 100,
                        'devices': 2,
                        'countries': ['test-squad'],
                        'total_price': 300,
                    }
                ),
                clear=AsyncMock(),
            )
            operation = classic.confirm_purchase(callback, state, user, db)
        elif flow == 'admin-buy':
            from app.handlers.admin import users

            monkeypatch.setattr(
                users.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': sub})
            )
            callback.data = 'admin_buy_tariff_execute_1_1_30_1000'
            operation = inspect.unwrap(users.admin_buy_tariff_execute)(callback, user, db)
        elif flow == 'cabinet':
            from app.cabinet.routes.subscription_modules import purchase
            from app.cabinet.schemas.subscription import TariffPurchaseRequest

            operation = purchase.purchase_tariff(
                TariffPurchaseRequest(tariff_id=1, period_days=30, subscription_id=11), user, db
            )
        elif flow == 'autocart':
            from app.services import subscription_auto_purchase_service as auto

            operation = auto._auto_purchase_tariff(
                db, user, {'tariff_id': 1, 'period_days': 30, 'total_price': 1000, 'subscription_id': 11}
            )
        else:
            from app.webapi.routes import miniapp

            monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
            operation = miniapp.purchase_tariff_endpoint(
                miniapp.MiniAppTariffPurchaseRequest(initData='test', tariffId=1, periodDays=30), db
            )
        try:
            await operation
        except asyncio.CancelledError:
            assert cancel
            assert not db.in_transaction()
        except Exception:
            assert not cancel
            # HTTP/service callers propagate; Telegram handlers render their error.
        failure.assert_awaited_once()
        await db.rollback()
        actual_user = await db.get(User, 1, populate_existing=True)
        actual_sub = await db.get(Subscription, 11, populate_existing=True)
        assert actual_user.balance_kopeks == old_balance
        assert actual_sub.end_date == old_end
        assert list(await db.scalars(select(Transaction))) == []


async def test_classic_miniapp_service_intent_failure_rolls_back(postgres_database, monkeypatch):
    from app.database.models import Base
    from app.services import subscription_purchase_service as purchase
    from tests.database.test_miniapp_purchase_atomic_postgres import _prepare
    from tests.fixtures.postgres_db import postgres_session

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        context, pricing, old_end = await _prepare(db)
        user, sub = context.user, context.subscription
        await _manage(db, sub)
        uid, sid, balance = user.id, sub.id, user.balance_kopeks
        failure = AsyncMock(side_effect=RuntimeError('intent staging failure'))
        monkeypatch.setattr(grants, 'stage_paid_base_grant', failure)
        with pytest.raises(RuntimeError, match='intent staging failure'):
            await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        failure.assert_awaited_once()
        await db.rollback()
        assert (await db.get(User, uid, populate_existing=True)).balance_kopeks == balance
        assert (await db.get(Subscription, sid, populate_existing=True)).end_date == old_end
        assert list(await db.scalars(select(Transaction))) == []
