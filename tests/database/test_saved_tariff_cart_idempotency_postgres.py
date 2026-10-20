"""A saved checkout must debit at most once across sessions and commits."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud import subscription as subscriptions
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, TransactionType, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.database.test_classic_device_migration_postgres import Panel
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('concurrent', [False, True])
async def test_saved_cart_only_charged_once(
    postgres_database,
    monkeypatch,
    concurrent,
    failure=None,
    promo=0,
    first_flow='auto',
    second_flow='auto',
    days=30,
    legacy=False,
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_subscription_cart', AsyncMock())
    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    entered = asyncio.Event()
    release = asyncio.Event()
    if failure == 'ledger':
        from app.database.crud import transaction as transactions

        original_transaction = transactions.create_transaction
        ledger_calls = 0

        async def create_transaction(*args, **kwargs):
            nonlocal ledger_calls
            ledger_calls += 1
            if ledger_calls == 1:
                raise RuntimeError('Injected ledger failure')
            return await original_transaction(*args, **kwargs)

        monkeypatch.setattr(transactions, 'create_transaction', create_transaction)
        monkeypatch.setattr(cabinet, 'create_transaction', create_transaction)
    if failure == 'refund':
        monkeypatch.setattr(cabinet, 'add_user_balance', AsyncMock(return_value=False))
        monkeypatch.setattr(cabinet, '_persist_failed_refund', AsyncMock())
        monkeypatch.setattr('app.database.crud.user.add_user_balance', AsyncMock(return_value=False))
    original_extend = subscriptions.extend_subscription
    calls = 0

    async def extend(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure in ('provision', 'refund') and calls == 1:
            raise RuntimeError('Injected provisioning failure')
        if concurrent and calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original_extend(*args, **kwargs)

    monkeypatch.setattr(subscriptions, 'extend_subscription', extend)
    monkeypatch.setattr(telegram, 'extend_subscription', extend)
    monkeypatch.setattr(cabinet, 'extend_subscription', extend)
    monkeypatch.setattr(cabinet.user_cart_service, 'save_user_cart', AsyncMock(return_value=True))
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add(
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    status='active',
                    balance_kopeks=10000,
                    remnawave_id=9001,
                    promo_offer_discount_percent=promo,
                    promo_offer_discount_source='test',
                    promo_offer_discount_expires_at=now + timedelta(days=1),
                )
            )
            db.add(
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    period_prices={'30': 300},
                    custom_days_enabled=True,
                    price_per_day_kopeks=10,
                    min_days=1,
                    max_days=365,
                    allowed_squads=['test-squad'],
                )
            )
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=None,
                    is_trial=False,
                    status='active',
                    device_limit=3,
                    end_date=now + timedelta(days=10),
                    remnawave_id=9001,
                    remnawave_short_id='test',
                    subscription_url='https://vpn.example/sub/test',
                )
            )
            await db.commit()
        cart = {
            'cart_mode': 'tariff_purchase',
            'tariff_id': 1,
            'period_days': days,
            'subscription_id': 10,
            'tariff_checkout_id': '11111111-1111-4111-8111-111111111111',
        }

        if failure == 'topup':
            from fastapi import HTTPException

            async with maker() as db:
                user = await get_user_by_id(db, 1)
                user.balance_kopeks = 0
                await db.commit()
                with pytest.raises(HTTPException) as insufficient:
                    await cabinet.purchase_tariff(
                        cabinet.TariffPurchaseRequest(
                            tariff_id=1,
                            period_days=days,
                            subscription_id=10,
                            checkout_id=cart['tariff_checkout_id'],
                        ),
                        user,
                        db,
                    )
                assert insufficient.value.status_code == 402
                saved = cabinet.user_cart_service.save_user_cart.await_args.args[1]
                assert saved['tariff_checkout_id'] == cart['tariff_checkout_id']
                assert saved['subscription_id'] == 10 and saved['period_days'] == days
                cart = dict(saved)
                user.balance_kopeks = 10000
                await db.commit()

        if legacy:
            cart.pop('tariff_checkout_id')
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        callback = SimpleNamespace(
            answer=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )
        if 'manual' in (first_flow, second_flow):
            async with maker() as db:
                user = await get_user_by_id(db, 1)
                await telegram.return_to_saved_tariff_cart(callback, state, user, db, dict(cart))
                callback.data = (
                    callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
                )

        async def preview_fresh():
            nonlocal cart
            async with maker() as db:
                user = await get_user_by_id(db, 1)
                await state.update_data(target_subscription_id=10, custom_days=days, custom_traffic_gb=100)
                callback.data = 'tariff_period:1:30' if days == 30 else 'custom_days:1:0'
                preview_handler = telegram.select_tariff_period if days == 30 else telegram.handle_custom_days_change
                await inspect.unwrap(preview_handler)(callback, user, db, state)
                markup = callback.message.edit_text.await_args.kwargs['reply_markup']
                callback.data = next(
                    button.callback_data
                    for row in markup.inline_keyboard
                    for button in row
                    if button.callback_data and button.callback_data.startswith(('tariff_confirm:', 'custom_confirm:'))
                )
                assert len(callback.data.encode()) <= 64
                checkout = (await state.get_data()).get('restored_tariff_checkout')
                if checkout:
                    cart = dict(checkout['cart'])

        if 'fresh' in (first_flow, second_flow):
            await preview_fresh()

        if failure == 'stale_source':
            async with maker() as db:
                db.add(Tariff(id=2, name='Changed', is_active=True, device_limit=7))
                await db.flush()
                changed = await db.get(Subscription, 10)
                changed.tariff_id = 2
                await db.commit()

        async def purchase(flow='auto'):
            async with maker() as db:
                user = await get_user_by_id(db, 1)
                if flow == 'cabinet':
                    app = FastAPI()
                    app.add_api_route('/purchase', cabinet.purchase_tariff, methods=['POST'])

                    async def current_user():
                        return user

                    async def current_db():
                        return db

                    app.dependency_overrides[cabinet.get_current_cabinet_user] = current_user
                    app.dependency_overrides[cabinet.get_cabinet_db] = current_db
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url='https://cabinet.example'
                    ) as client:
                        response = await client.post(
                            '/purchase',
                            json={
                                'tariff_id': 1,
                                'period_days': days,
                                'subscription_id': 10,
                                'checkout_id': cart['tariff_checkout_id'],
                            },
                        )
                    if response.status_code == 409:
                        assert response.json()['detail']['code'] == 'checkout_already_processed'
                        return False
                    if failure in ('ledger', 'provision', 'refund') and response.status_code == 500:
                        return False
                    assert response.status_code == 200, response.text
                    return response.json()
                if flow == 'auto':
                    return await auto._auto_purchase_tariff(db, user, dict(cart))
                handler = (
                    telegram.handle_custom_confirm
                    if callback.data.startswith('custom_confirm:')
                    else telegram.confirm_tariff_purchase
                )
                await inspect.unwrap(handler)(callback, user, db, state)
                return None

        if concurrent:
            first = asyncio.create_task(purchase(first_flow))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                second = await asyncio.wait_for(purchase(second_flow), 10)
            finally:
                release.set()
            results = [await first, second]
        else:
            first_result = await purchase(first_flow)
            if failure == 'cleared_state':
                await state.clear()
            if failure == 'new_checkout':
                cart['tariff_checkout_id'] = '22222222-2222-4222-8222-222222222222'
                if second_flow == 'manual':
                    async with maker() as db:
                        user = await get_user_by_id(db, 1)
                        await telegram._proceed_with_selected_tariff(callback, user, db, state, 1)
                        assert (await state.get_data()).get('restored_tariff_checkout') is None
                        await telegram.return_to_saved_tariff_cart(callback, state, user, db, dict(cart))
                        callback.data = (
                            callback.message.edit_text.await_args.kwargs['reply_markup']
                            .inline_keyboard[0][0]
                            .callback_data
                        )
            if failure == 'new_checkout' and second_flow == 'fresh':
                await preview_fresh()
            results = [first_result, await purchase(second_flow)]
        expected_results = (
            [False, False]
            if failure in ('refund', 'stale_source')
            else [False, True]
            if failure in ('ledger', 'provision')
            else [True, True]
            if failure == 'new_checkout'
            else [True, False]
        )
        if first_flow == second_flow == 'auto':
            assert results == expected_results
        async with maker() as db:
            user = await get_user_by_id(db, 1)
            sub = await db.get(Subscription, 10)
            payments = (await db.scalars(select(Transaction))).all()
            base_price = days * 10 + 200 * max(1, round(days / 30))
            price = base_price - base_price * promo // 100
            if failure == 'stale_source':
                assert user.balance_kopeks == 10000 and not payments
                assert sub.tariff_id == 2 and sub.device_limit == 3
                assert sub.end_date == now + timedelta(days=10) and not panel.patches
                return
            count = 2 if failure == 'new_checkout' else 1
            total = price + base_price if failure == 'new_checkout' else price
            assert user.balance_kopeks == 10000 - total
            assert sorted(p.amount_kopeks for p in payments) == (
                [-price, -price, price]
                if failure == 'provision'
                else [-base_price, -price]
                if failure == 'new_checkout'
                else [-price]
            )
            if failure == 'refund':
                assert not payments[0].is_completed and payments[0].external_id
            else:
                assert (
                    sum(p.is_completed and p.type == TransactionType.SUBSCRIPTION_PAYMENT.value for p in payments)
                    == count
                )
            assert sub.end_date == now + timedelta(days=10 if failure == 'refund' else 10 + days * count)
            if failure == 'provision' and promo:
                assert user.promo_offer_discount_percent == 0
            assert sub.remnawave_id == 9001 and sub.device_limit == 3
            assert len(panel.patches) == (0 if failure == 'refund' else count)


@pytest.mark.parametrize('failure', ['ledger', 'provision', 'refund', 'new_checkout'])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_saved_cart_retries_only_after_atomic_compensation(postgres_database, monkeypatch, failure, promo):
    await test_saved_cart_only_charged_once(postgres_database, monkeypatch, False, failure=failure, promo=promo)


@pytest.mark.parametrize('promo', [17, 100])
async def test_concurrent_saved_cart_consumes_promo_once(postgres_database, monkeypatch, promo):
    await test_saved_cart_only_charged_once(postgres_database, monkeypatch, True, promo=promo)


@pytest.mark.parametrize(('first_flow', 'second_flow'), [('auto', 'manual'), ('manual', 'auto'), ('manual', 'manual')])
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('days', [30, 75])
async def test_restored_manual_and_auto_share_checkout(
    postgres_database, monkeypatch, first_flow, second_flow, concurrent, days
):
    await test_saved_cart_only_charged_once(
        postgres_database, monkeypatch, concurrent, first_flow=first_flow, second_flow=second_flow, days=days
    )


@pytest.mark.parametrize('days', [30, 75])
@pytest.mark.parametrize(('first_flow', 'second_flow'), [('auto', 'manual'), ('manual', 'auto')])
async def test_legacy_restored_checkout_keeps_original_identity(
    postgres_database, monkeypatch, days, first_flow, second_flow
):
    await test_saved_cart_only_charged_once(
        postgres_database, monkeypatch, True, first_flow=first_flow, second_flow=second_flow, days=days, legacy=True
    )


@pytest.mark.parametrize('days', [30, 75])
async def test_old_saved_cart_button_cannot_charge_after_state_clear(postgres_database, monkeypatch, days):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        first_flow='manual',
        second_flow='manual',
        days=days,
        failure='cleared_state',
    )


@pytest.mark.parametrize('failure', ['ledger', 'provision', 'refund', 'new_checkout'])
@pytest.mark.parametrize('days', [30, 75])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_restored_manual_compensation_and_fresh_checkout(postgres_database, monkeypatch, failure, days, promo):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        failure=failure,
        promo=promo,
        first_flow='manual',
        second_flow='manual',
        days=days,
    )


@pytest.mark.parametrize('flow', ['auto', 'manual'])
@pytest.mark.parametrize('days', [30, 75])
async def test_saved_checkout_does_not_overwrite_changed_paid_source(postgres_database, monkeypatch, flow, days):
    await test_saved_cart_only_charged_once(
        postgres_database, monkeypatch, False, failure='stale_source', first_flow=flow, second_flow=flow, days=days
    )


@pytest.mark.parametrize(
    ('first_flow', 'second_flow'), [('cabinet', 'cabinet'), ('cabinet', 'auto'), ('auto', 'cabinet')]
)
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('days', [30, 75])
async def test_cabinet_request_and_cart_share_payment_identity(
    postgres_database, monkeypatch, first_flow, second_flow, concurrent, days
):
    await test_saved_cart_only_charged_once(
        postgres_database, monkeypatch, concurrent, first_flow=first_flow, second_flow=second_flow, days=days
    )


@pytest.mark.parametrize('failure', ['ledger', 'provision', 'refund', 'new_checkout'])
@pytest.mark.parametrize('promo', [0, 17, 100])
@pytest.mark.parametrize('second_flow', ['cabinet', 'auto'])
async def test_cabinet_retry_requires_compensation_or_new_identity(
    postgres_database, monkeypatch, failure, promo, second_flow
):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        failure=failure,
        promo=promo,
        first_flow='cabinet',
        second_flow=second_flow,
    )


@pytest.mark.parametrize(('first_flow', 'second_flow'), [('cabinet', 'auto'), ('auto', 'cabinet')])
@pytest.mark.parametrize('days', [30, 75])
async def test_cabinet_insufficient_balance_cart_retains_request_identity(
    postgres_database, monkeypatch, first_flow, second_flow, days
):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        failure='topup',
        first_flow=first_flow,
        second_flow=second_flow,
        days=days,
    )


@pytest.mark.parametrize(('first_flow', 'second_flow'), [('fresh', 'fresh'), ('fresh', 'auto'), ('auto', 'fresh')])
@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('days', [30, 75])
async def test_fresh_telegram_preview_is_one_purchase_attempt(
    postgres_database, monkeypatch, first_flow, second_flow, concurrent, days
):
    await test_saved_cart_only_charged_once(
        postgres_database, monkeypatch, concurrent, first_flow=first_flow, second_flow=second_flow, days=days
    )


@pytest.mark.parametrize('failure', ['ledger', 'provision', 'refund', 'new_checkout'])
@pytest.mark.parametrize('promo', [0, 17, 100])
@pytest.mark.parametrize('days', [30, 75])
async def test_fresh_telegram_compensation_and_new_selection(postgres_database, monkeypatch, failure, promo, days):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        failure=failure,
        promo=promo,
        days=days,
        first_flow='fresh',
        second_flow='fresh',
    )


@pytest.mark.parametrize('failure', ['cleared_state', 'stale_source'])
@pytest.mark.parametrize('days', [30, 75])
async def test_fresh_telegram_old_state_and_changed_source_are_safe(postgres_database, monkeypatch, failure, days):
    await test_saved_cart_only_charged_once(
        postgres_database,
        monkeypatch,
        False,
        failure=failure,
        days=days,
        first_flow='fresh',
        second_flow='fresh',
    )
