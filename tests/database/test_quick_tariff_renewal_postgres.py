"""TG-063: quick renewal must use the attached tariff, not classic defaults."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CLASSIC_PERIOD_PRICES, settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, PromoGroup, Subscription, Tariff, TrafficPurchase, Transaction, User
from app.handlers import simple_subscription as simple
from app.handlers.subscription.purchase import handle_simple_subscription_purchase
from app.services.subscription_service import SubscriptionService
from tests.database.test_renewal_hwid_postgres import Panel
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_session


pytestmark = pytest.mark.postgres


class QuickRenewalPanel(Panel):
    async def get_user_devices_all(self, user_id):
        return {
            'total': len(self.devices[user_id]),
            'devices': [{'userId': user_id, 'hwid': hwid} for hwid in self.devices[user_id]],
        }


@asynccontextmanager
async def checkout(postgres_database, monkeypatch):
    for name, value in {
        'SALES_MODE': 'tariffs',
        'MULTI_TARIFF_ENABLED': False,
        'SIMPLE_SUBSCRIPTION_ENABLED': True,
        'SIMPLE_SUBSCRIPTION_PERIOD_DAYS': 30,
        'SIMPLE_SUBSCRIPTION_TRAFFIC_GB': 5,
        'SIMPLE_SUBSCRIPTION_SQUAD_UUID': '',
        'DEFAULT_DEVICE_LIMIT': 1,
        'PRICE_30_DAYS': 45000,
        'PRICE_PER_DEVICE': 10000,
        'RESET_TRAFFIC_ON_PAYMENT': False,
        'RESET_DEVICES_ON_RENEWAL': False,
        'ADMIN_NOTIFICATIONS_ENABLED': False,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setitem(CLASSIC_PERIOD_PRICES, 30, 45000)
    cart_store = AsyncMock()
    monkeypatch.setattr('app.services.user_cart_service.user_cart_service.save_user_cart', cart_store)
    now = datetime.now(UTC)
    end = now + timedelta(days=5)
    panel = QuickRenewalPanel(end)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=400000, remnawave_id=9001),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=1,
                    device_price_kopeks=5000,
                    period_prices={'14': 10000, '30': 20000, '365': 200000},
                    allowed_squads=['test-squad'],
                ),
            ]
        )
        await db.commit()
        sub = Subscription(
            id=10,
            user_id=1,
            tariff_id=1,
            remnawave_id=9001,
            remnawave_short_id='test-10',
            status='active',
            is_trial=False,
            start_date=now - timedelta(days=30),
            end_date=end,
            traffic_limit_gb=100,
            traffic_used_gb=17,
            device_limit=3,
            connected_squads=['test-squad'],
        )
        db.add(sub)
        await db.commit()
        user = await get_user_by_id(db, 1)
        callback = SimpleNamespace(
            data='simple_subscription_purchase',
            answer=AsyncMock(),
            bot=SimpleNamespace(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        state = SimpleNamespace(
            update_data=AsyncMock(), set_state=AsyncMock(), clear=AsyncMock(), get_data=AsyncMock(return_value={})
        )
        yield SimpleNamespace(
            db=db,
            user=user,
            sub=sub,
            callback=callback,
            state=state,
            panel=panel,
            now=now,
            end=end,
            cart_store=cart_store,
        )


@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('period', [14, 30, 365])
async def test_quick_renewal_charges_tariff_and_preserves_paid_parameters(
    postgres_database, monkeypatch, expired, period
):
    async with checkout(postgres_database, monkeypatch) as c:
        monkeypatch.setattr(settings, 'SIMPLE_SUBSCRIPTION_PERIOD_DAYS', period)
        if expired:
            c.sub.end_date = c.now - timedelta(days=1)
            c.sub.status = 'expired'
            await c.db.commit()
        await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        # Two extra devices at 5000/month, billed for the exact renewal days.
        expected_price = {14: 14666, 30: 30000, 365: 321666}[period]
        assert c.user.balance_kopeks == 400000 - expected_price
        transactions = (await c.db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -expected_price
        assert c.sub.tariff_id == 1 and c.sub.device_limit == 3
        assert c.sub.traffic_limit_gb == 100 and c.sub.traffic_used_gb == 17
        assert c.sub.connected_squads == ['test-squad']
        assert abs((c.sub.end_date - ((c.now if expired else c.end) + timedelta(days=period))).total_seconds()) < 10
        assert c.panel.updated and c.panel.updated[-1]['hwid_device_limit'] == 3
        assert c.panel.account.expire_at == c.sub.end_date


async def test_insufficient_balance_cart_retains_tariff_and_subscription(postgres_database, monkeypatch):
    async with checkout(postgres_database, monkeypatch) as c:
        c.user.balance_kopeks = 10000
        await c.db.commit()
        await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 10000 and c.sub.end_date == c.end
        assert not (await c.db.scalars(select(Transaction))).all()
        cart = c.cart_store.call_args.args[1]
        assert cart['total_price'] == 30000 and cart['missing_amount'] == 20000 and cart['tariff_id'] == 1
        assert cart['subscription_id'] == 10 and cart['device_limit'] == 3
        assert cart['traffic_limit_gb'] == 100 and cart['countries'] == ['test-squad']

        from app.services import subscription_auto_purchase_service as auto

        monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
        monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
        c.user.balance_kopeks = 40000
        await c.db.commit()
        assert await auto._auto_extend_subscription(c.db, c.user, cart)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 10000
        assert c.sub.end_date == c.end + timedelta(days=30)
        assert c.sub.traffic_limit_gb == 100 and c.sub.device_limit == 3
        transactions = (await c.db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -30000


@pytest.mark.parametrize('entry', ['start', 'confirm', 'direct', 'external'])
async def test_stale_simple_checkout_does_not_sell_classic_parameters_to_tariff(postgres_database, monkeypatch, entry):
    async with checkout(postgres_database, monkeypatch) as c:
        c.state.get_data.return_value = {
            'subscription_params': {
                'period_days': 30,
                'device_limit': 3,
                'traffic_limit_gb': 5,
                'squad_uuid': '',
            }
        }
        handlers = {
            'start': simple.start_simple_subscription_purchase,
            'confirm': simple.confirm_simple_subscription_purchase,
            'direct': simple.handle_simple_subscription_pay_with_balance,
            'external': simple.handle_simple_subscription_payment_method,
        }
        # Expired paid subscriptions must not fall back to classic checkout either.
        c.sub.end_date = c.now - timedelta(days=1)
        c.sub.status = 'expired'
        await c.db.commit()
        await handlers[entry](c.callback, c.user, c.state, c.db)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000
        assert c.sub.tariff_id == 1 and c.sub.traffic_limit_gb == 100 and c.sub.device_limit == 3
        assert c.sub.end_date == c.now - timedelta(days=1)
        assert not (await c.db.scalars(select(Transaction))).all()
        assert c.callback.answer.call_args.kwargs.get('show_alert') is True


@pytest.mark.parametrize('entry', ['quick', 'cabinet'])
@pytest.mark.parametrize('case', ['promo', 'group_and_promo', 'free', 'unlimited', 'classic_mode'])
async def test_quick_tariff_renewal_price_variants(postgres_database, monkeypatch, case, entry):
    async with checkout(postgres_database, monkeypatch) as c:
        tariff = await c.db.get(Tariff, 1)
        expected = 30000
        if case in ('promo', 'group_and_promo'):
            c.user.promo_offer_discount_percent = 20
            c.user.promo_offer_discount_expires_at = c.now + timedelta(days=1)
            expected = 24000
            if case == 'group_and_promo':
                group = PromoGroup(id=1, name='Test', period_discounts={'30': 10}, device_discount_percent=20)
                c.db.add(group)
                await c.db.flush()
                c.user.promo_group_id = 1
                expected = 20800  # (20000*0.9 + 10000*0.8)*0.8
        elif case == 'free':
            tariff.period_prices = {'30': 0}
            tariff.device_price_kopeks = 0
            expected = 0
        elif case == 'unlimited':
            tariff.device_limit = c.sub.device_limit = 0
            expected = 20000
        else:
            monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
        await c.db.commit()
        if entry == 'quick':
            await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        else:
            from app.cabinet.routes.subscription_modules.renewal import RenewalRequest, renew_subscription

            await renew_subscription(RenewalRequest(period_days=30), c.user, c.db, subscription_id=10)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000 - expected
        assert c.sub.end_date == c.end + timedelta(days=30)
        assert c.sub.device_limit == (0 if case == 'unlimited' else 3)
        transactions = (await c.db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -expected
        if case in ('promo', 'group_and_promo'):
            assert c.user.promo_offer_discount_percent == 0


@pytest.mark.parametrize(
    'case', ['missing_period', 'null_price', 'daily', 'trial', 'inactive', 'multi', 'restricted', 'disabled', 'pending']
)
async def test_quick_tariff_renewal_rejects_unsupported_checkout_before_charge(postgres_database, monkeypatch, case):
    async with checkout(postgres_database, monkeypatch) as c:
        tariff = await c.db.get(Tariff, 1)
        if case == 'missing_period':
            monkeypatch.setattr(settings, 'SIMPLE_SUBSCRIPTION_PERIOD_DAYS', 15)
        elif case == 'null_price':
            tariff.period_prices = {'30': None}
        elif case == 'daily':
            tariff.is_daily = True
        elif case == 'trial':
            c.sub.is_trial = True
        elif case == 'inactive':
            tariff.is_active = False
        elif case == 'multi':
            monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
        elif case in ('disabled', 'pending'):
            c.sub.status = case
        else:
            c.user.restriction_subscription = True
        await c.db.commit()
        await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000 and c.sub.end_date == c.end
        assert not (await c.db.scalars(select(Transaction))).all()
        assert not c.panel.updated
        assert c.callback.answer.call_args.kwargs.get('show_alert') is True


async def test_quick_tariff_renewal_rolls_back_debit_and_discount_when_ledger_fails(postgres_database, monkeypatch):
    async with checkout(postgres_database, monkeypatch) as c:
        c.user.promo_offer_discount_percent = 20
        c.user.promo_offer_discount_expires_at = c.now + timedelta(days=1)
        await c.db.commit()
        monkeypatch.setattr(
            'app.services.subscription_renewal_service.create_transaction',
            AsyncMock(side_effect=RuntimeError('synthetic ledger failure')),
        )
        with pytest.raises(RuntimeError, match='synthetic ledger failure'):
            await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000 and c.sub.end_date == c.end
        assert c.user.promo_offer_discount_percent == 20
        assert not (await c.db.scalars(select(Transaction))).all()
        assert not c.panel.updated


@pytest.mark.parametrize('failure', ['changed_tariff', 'ledger', 'cancel'])
async def test_saved_quick_renewal_cannot_switch_tariff_or_leave_unrecorded_charge(
    postgres_database, monkeypatch, failure
):
    from app.services import subscription_auto_purchase_service as auto

    async with checkout(postgres_database, monkeypatch) as c:
        c.user.balance_kopeks = 10000
        await c.db.commit()
        await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        cart = c.cart_store.call_args.args[1]
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        c.user.balance_kopeks = 400000
        if failure == 'changed_tariff':
            c.db.add(
                Tariff(
                    id=2,
                    name='Other',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=3,
                    period_prices={'30': 1000},
                    allowed_squads=['test-squad'],
                )
            )
            await c.db.flush()
            c.sub.tariff_id = 2
        else:
            error = asyncio.CancelledError() if failure == 'cancel' else RuntimeError('synthetic ledger failure')
            monkeypatch.setattr(auto, 'create_transaction', AsyncMock(side_effect=error))
        monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
        monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
        await c.db.commit()
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await auto._auto_extend_subscription(c.db, c.user, cart)
        else:
            assert await auto._auto_extend_subscription(c.db, c.user, cart) is False
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 400000 and c.sub.end_date == c.end
        assert c.sub.tariff_id == (2 if failure == 'changed_tariff' else 1)
        assert not (await c.db.scalars(select(Transaction))).all()


async def test_saved_tariff_renewal_preserves_addons_bought_after_cart(postgres_database, monkeypatch):
    from app.services import subscription_auto_purchase_service as auto

    async with checkout(postgres_database, monkeypatch) as c:
        c.user.balance_kopeks = 10000
        await c.db.commit()
        await handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db)
        cart = c.cart_store.call_args.args[1]
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        c.user.balance_kopeks = 400000
        c.sub.traffic_limit_gb = 125
        c.sub.purchased_traffic_gb = 25
        c.sub.device_limit = 2
        c.db.add(TrafficPurchase(subscription_id=10, traffic_gb=25, expires_at=c.now + timedelta(days=20)))
        await c.db.commit()
        monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
        monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
        assert await auto._auto_extend_subscription(c.db, c.user, cart)
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 375000
        assert c.sub.device_limit == 2 and c.sub.traffic_limit_gb == 125 and c.sub.purchased_traffic_gb == 25


async def test_quick_renewal_reloads_devices_after_waiting_for_concurrent_change(postgres_database, monkeypatch):
    from app.database.crud.user import lock_user_for_pricing

    async with checkout(postgres_database, monkeypatch) as c:
        async with AsyncSession(c.db.bind, expire_on_commit=False) as editor, AsyncSession(c.db.bind) as observer:
            await lock_user_for_pricing(editor, 1)
            changed = await editor.get(Subscription, 10)
            changed.device_limit = 2
            await editor.flush()
            task = asyncio.create_task(handle_simple_subscription_purchase(c.callback, c.state, c.user, c.db))
            try:
                waiting = await lock_waiter_appeared(observer)
                await editor.commit()
                await asyncio.wait_for(task, 15)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert waiting, 'The quick callback must actually wait for the concurrent financial operation'
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == 375000 and c.sub.device_limit == 2
        assert c.sub.end_date == c.end + timedelta(days=30)


async def test_registered_simple_callback_uses_tariff_renewal(postgres_database, monkeypatch):
    from aiogram import Dispatcher

    from app.handlers.subscription import register_handlers

    async with checkout(postgres_database, monkeypatch) as c:
        dp = Dispatcher()
        # Same registration order as app.bot: general subscriptions precede simple purchase.
        register_handlers(dp)
        simple.register_simple_subscription_handlers(dp)
        await dp.callback_query.trigger(c.callback, state=c.state, db_user=c.user, db=c.db)
        await c.db.refresh(c.user)
        assert c.user.balance_kopeks == 370000
        transactions = (await c.db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -30000
