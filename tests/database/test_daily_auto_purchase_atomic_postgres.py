"""Daily activation must retain the wallet lock until product and ledger persist."""

import asyncio
import inspect
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
from app.database.crud import subscription as subscriptions, transaction as transactions, user as users
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('fresh', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
@pytest.mark.parametrize('failure', [None, 'debit', 'ledger', 'provision'])
async def test_daily_auto_activation_is_atomic(
    postgres_database,
    monkeypatch,
    fresh,
    promo,
    failure,
    first_flow='auto',
    second_flow='auto',
    multi=True,
    trial=False,
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    now = datetime.now(UTC)
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
    monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=True))
    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'save_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_subscription_cart', AsyncMock())
    entered, release, second_debit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    debit = users.subtract_user_balance
    debit_calls = 0

    async def charge(*args, **kwargs):
        nonlocal debit_calls
        debit_calls += 1
        call = debit_calls
        result = await debit(*args, **kwargs)
        if call == 1:
            if failure == 'debit':
                raise RuntimeError('Injected debit boundary failure')
            if failure is None:
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
        else:
            second_debit.set()
        return result

    monkeypatch.setattr(users, 'subtract_user_balance', charge)
    monkeypatch.setattr(telegram, 'subtract_user_balance', charge)
    monkeypatch.setattr(cabinet, 'subtract_user_balance', charge)
    create_transaction = transactions.create_transaction
    ledger_calls = 0

    async def ledger(*args, **kwargs):
        nonlocal ledger_calls
        ledger_calls += 1
        if failure == 'ledger' and ledger_calls == 1:
            raise RuntimeError('Injected daily ledger failure')
        return await create_transaction(*args, **kwargs)

    monkeypatch.setattr(transactions, 'create_transaction', ledger)
    monkeypatch.setattr(telegram, 'create_transaction', ledger)
    monkeypatch.setattr(cabinet, 'create_transaction', ledger)
    create = subscriptions.create_paid_subscription
    creates = 0

    async def provision(*args, **kwargs):
        nonlocal creates
        creates += 1
        if failure == 'provision' and creates == 1:
            raise RuntimeError('Injected daily provisioning failure')
        return await create(*args, **kwargs)

    monkeypatch.setattr(subscriptions, 'create_paid_subscription', provision)
    monkeypatch.setattr(telegram, 'create_paid_subscription', provision)
    monkeypatch.setattr(cabinet, 'create_paid_subscription', provision)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        shifted = []

        async def shift_after_commit(db_arg, subscription_id, days):
            async with maker() as observer:
                payments = (await observer.scalars(select(Transaction))).all()
                active = await observer.get(Subscription, subscription_id)
                assert len(payments) == 1 and payments[0].is_completed
                assert active.tariff_id == 1 and active.last_daily_charge_at is not None
            shifted.append((subscription_id, days))

        monkeypatch.setattr(
            'app.services.payment.lava.shift_lava_next_charge_after_manual_extension', shift_after_commit
        )
        async with maker() as db:
            db.add(
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    balance_kopeks=10000,
                    promo_offer_discount_percent=promo,
                    promo_offer_discount_source='test',
                    promo_offer_discount_expires_at=now + timedelta(days=1),
                    remnawave_id=9001,
                )
            )
            db.add(
                Tariff(
                    id=1,
                    name='Daily',
                    is_active=True,
                    is_daily=True,
                    daily_price_kopeks=100,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    allowed_squads=['test-squad'],
                )
            )
            if not fresh:
                db.add(
                    Subscription(
                        id=10,
                        user_id=1,
                        tariff_id=None,
                        is_trial=trial,
                        status='active',
                        device_limit=3,
                        end_date=now + timedelta(days=10),
                        remnawave_id=9001,
                        remnawave_short_id='test',
                        subscription_url='https://vpn.example/sub/test',
                    )
                )
            await db.commit()
        cart = {'tariff_id': 1, 'subscription_id': None if fresh else 10}

        async def purchase(flow='auto'):
            async with maker() as db:
                user = await users.get_user_by_id(db, 1)
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
                                'period_days': 1,
                                'subscription_id': cart['subscription_id'],
                            },
                        )
                    if response.status_code == 409:
                        return False
                    if failure and response.status_code == 500:
                        return False
                    assert response.status_code == 200, response.text
                    return True
                if flow == 'telegram':
                    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
                    await state.update_data(target_subscription_id=cart['subscription_id'])
                    callback = SimpleNamespace(
                        data='daily_tariff_confirm:1',
                        answer=AsyncMock(),
                        bot=SimpleNamespace(send_message=AsyncMock()),
                        message=SimpleNamespace(edit_text=AsyncMock()),
                    )
                    await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
                    return bool(
                        callback.message.edit_text.await_args and '🎉' in callback.message.edit_text.await_args.args[0]
                    )
                return await auto._auto_purchase_daily_tariff(db, user, cart)

        if failure is None:
            first = asyncio.create_task(purchase(first_flow))
            await asyncio.wait_for(entered.wait(), 10)
            second = asyncio.create_task(purchase(second_flow))
            try:
                # Old code reaches the second debit before first activation; fixed
                # code blocks on the first transaction's user lock instead.
                try:
                    await asyncio.wait_for(second_debit.wait(), 0.5)
                except TimeoutError:
                    pass
            finally:
                release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert results == [True, False]
        elif failure == 'provision' and not fresh:
            # Existing-row provisioning has no create hook: exercise rollback at
            # the ledger boundary after its fields have been staged instead.
            monkeypatch.setattr(
                transactions, 'create_transaction', AsyncMock(side_effect=RuntimeError('Injected ledger failure'))
            )
            monkeypatch.setattr(telegram, 'create_transaction', transactions.create_transaction)
            monkeypatch.setattr(cabinet, 'create_transaction', transactions.create_transaction)
            assert not await purchase(first_flow)
            monkeypatch.setattr(transactions, 'create_transaction', create_transaction)
            monkeypatch.setattr(telegram, 'create_transaction', create_transaction)
            monkeypatch.setattr(cabinet, 'create_transaction', create_transaction)
            assert await purchase(second_flow)
        else:
            assert not await purchase(first_flow)
            assert await purchase(second_flow)
        async with maker() as db:
            user = await users.get_user_by_id(db, 1)
            base = 100 if fresh or trial else 300
            price = base - base * promo // 100
            assert user.balance_kopeks == 10000 - price
            payments = (await db.scalars(select(Transaction))).all()
            assert len(payments) == 1 and payments[0].amount_kopeks == -price and payments[0].is_completed
            rows = (await db.scalars(select(Subscription))).all()
            assert len(rows) == 1 and rows[0].tariff_id == 1 and not rows[0].is_trial
            assert rows[0].last_daily_charge_at is not None and not rows[0].is_daily_paused
            assert (
                abs(
                    (
                        rows[0].end_date
                        - (now + timedelta(days=11 if first_flow == 'cabinet' and not fresh and not trial else 1))
                    ).total_seconds()
                )
                < 20
            )
            assert rows[0].device_limit == (1 if fresh or trial else 3)
            assert user.promo_offer_discount_percent == 0
            successful_flow = first_flow if failure is None else second_flow
            assert shifted == ([(rows[0].id, 1)] if successful_flow == 'cabinet' and not fresh else [])


@pytest.mark.parametrize(
    ('first_flow', 'second_flow'),
    [
        ('telegram', 'telegram'),
        ('cabinet', 'cabinet'),
        ('telegram', 'auto'),
        ('auto', 'telegram'),
        ('cabinet', 'auto'),
        ('auto', 'cabinet'),
        ('telegram', 'cabinet'),
        ('cabinet', 'telegram'),
    ],
)
@pytest.mark.parametrize('fresh', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_daily_activation_mixed_entries_debit_once(
    postgres_database, monkeypatch, first_flow, second_flow, fresh, promo
):
    await test_daily_auto_activation_is_atomic(
        postgres_database,
        monkeypatch,
        fresh,
        promo,
        None,
        first_flow=first_flow,
        second_flow=second_flow,
    )


@pytest.mark.parametrize('flow', ['telegram', 'cabinet'])
@pytest.mark.parametrize('fresh', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
@pytest.mark.parametrize('failure', ['debit', 'ledger', 'provision'])
async def test_daily_manual_failure_rolls_back_before_retry(
    postgres_database, monkeypatch, flow, fresh, promo, failure
):
    await test_daily_auto_activation_is_atomic(
        postgres_database,
        monkeypatch,
        fresh,
        promo,
        failure,
        first_flow=flow,
        second_flow=flow,
    )


@pytest.mark.parametrize('flow', ['telegram', 'cabinet'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_daily_trial_conversion_and_auto_overlap(postgres_database, monkeypatch, flow, multi, promo):
    await test_daily_auto_activation_is_atomic(
        postgres_database,
        monkeypatch,
        False,
        promo,
        None,
        first_flow=flow,
        second_flow='auto',
        multi=multi,
        trial=True,
    )


@pytest.mark.parametrize('flow', ['telegram', 'cabinet'])
@pytest.mark.parametrize('fresh', [False, True])
@pytest.mark.parametrize('promo', [0, 17, 100])
async def test_daily_single_subscription_manual_and_auto_overlap(postgres_database, monkeypatch, flow, fresh, promo):
    await test_daily_auto_activation_is_atomic(
        postgres_database,
        monkeypatch,
        fresh,
        promo,
        None,
        first_flow=flow,
        second_flow='auto',
        multi=False,
    )
