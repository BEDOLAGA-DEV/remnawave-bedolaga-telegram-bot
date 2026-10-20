"""Opt-in acceptance tests against a disposable REAL Remnawave panel and PostgreSQL.

TEST_REMNAWAVE_URL must name a loopback panel; TEST_REMNAWAVE_TOKEN_FILE holds
its disposable API token. HWID checks must already be enabled. No payment,
Telegram, production panel or real proxy traffic is exercised by this module.
"""

import asyncio
import base64
import inspect
import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@asynccontextmanager
async def real_migration(postgres_database, monkeypatch, base, flow='cabinet'):
    url = os.environ.get('TEST_REMNAWAVE_URL')
    token_file = os.environ.get('TEST_REMNAWAVE_TOKEN_FILE')
    if not url or not token_file:
        pytest.skip('Explicit disposable loopback Remnawave URL/token required')
    assert urlparse(url).hostname in {'127.0.0.1', 'localhost', '::1'}, 'Only disposable local panels permitted'
    token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'HAPP_CRYPTOLINK_API_FALLBACK_ENABLED', False)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', AsyncMock())
    if flow != 'wallet_topup':
        monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())

    @asynccontextmanager
    async def client(self):
        async with RemnaWaveAPI(base_url=url, api_key=token) as api:
            yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    headers = {'Authorization': f'Bearer {token}', 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1'}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as setup:

        async def request(method, path, data=None):
            response = await setup.request(method, path, json=data)
            assert response.is_success, (method, response.status_code, response.text[:300])
            if response.status_code == 204:
                return None
            return response.json()['response']

        config = await request('GET', '/api/subscription-settings')
        assert config['hwidSettings']['enabled'], 'Enable HWID checks on the disposable panel first'
        accounts = []
        now = datetime.now(UTC)
        try:
            for index, cap in enumerate([10, 7]):
                accounts.append(
                    await request(
                        'POST',
                        '/api/users',
                        {
                            'username': 'audit-' + secrets.token_hex(6),
                            'telegramId': 101,
                            'expireAt': (now + timedelta(days=10 if index == 0 else 50)).isoformat(),
                            'hwidDeviceLimit': cap,
                        },
                    )
                )
            old, neighbor = accounts
            for index in range(10):
                await request('POST', '/api/hwid/devices', {'userId': old['id'], 'hwid': f'audit-device-{index:02}'})
            async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
                db.add(
                    User(
                        id=1,
                        telegram_id=101,
                        language='ru',
                        status='active',
                        balance_kopeks=10000,
                        remnawave_id=neighbor['id'],
                    )
                )
                db.add(
                    Tariff(
                        id=1,
                        name='Audit',
                        is_active=True,
                        traffic_limit_gb=100,
                        custom_traffic_enabled=flow in ('cart_return_traffic', 'auto_cart_traffic'),
                        traffic_price_per_gb_kopeks=2,
                        min_traffic_gb=10,
                        max_traffic_gb=500,
                        device_limit=base,
                        max_device_limit=5,
                        device_price_kopeks=137,
                        period_prices={'30': 301},
                        is_daily=flow.endswith('daily'),
                        daily_price_kopeks=101,
                        custom_days_enabled=True,
                        price_per_day_kopeks=10,
                        allowed_squads=[],
                    )
                )
                for index, account in enumerate(accounts):
                    db.add(
                        Subscription(
                            id=10 + index * 10,
                            user_id=1,
                            tariff_id=None,
                            is_trial=False,
                            remnawave_id=account['id'],
                            remnawave_short_id=account['shortUuid'],
                            subscription_url=account['subscriptionUrl'],
                            status='active',
                            end_date=now + timedelta(days=10 if index == 0 else 50),
                            device_limit=10 if index == 0 else 7,
                        )
                    )
                await db.commit()
                user = await get_user_by_id(db, 1)
                subscription = await db.get(Subscription, 10)
                tariff = await db.get(Tariff, 1)
                if flow in ('cabinet', 'cabinet_daily'):
                    price = (101 if flow.endswith('daily') else 301) + (5 - base) * 137
                    preview = await cabinet._build_tariff_response(db, tariff, user=user, subscription=subscription)
                    assert preview['device_limit'] == 5
                    if flow.endswith('daily'):
                        assert preview['daily_price_kopeks'] == price
                    else:
                        assert next(p for p in preview['periods'] if p['days'] == 30)['price_kopeks'] == price
                    checkout_id = '11111111-1111-4111-8111-111111111111'
                    purchase_request = cabinet.TariffPurchaseRequest(
                        tariff_id=1,
                        period_days=1 if flow.endswith('daily') else 30,
                        subscription_id=10,
                        checkout_id=None if flow.endswith('daily') else checkout_id,
                    )
                    result = await cabinet.purchase_tariff(purchase_request, user, db)
                    assert result['success']
                    if flow == 'cabinet':
                        with pytest.raises(HTTPException) as duplicate:
                            await cabinet.purchase_tariff(purchase_request, user, db)
                        assert duplicate.value.status_code == 409
                        assert not await auto._auto_purchase_tariff(
                            db,
                            user,
                            {
                                'cart_mode': 'tariff_purchase',
                                'tariff_id': 1,
                                'period_days': 30,
                                'subscription_id': 10,
                                'tariff_checkout_id': checkout_id,
                            },
                        )
                elif flow == 'telegram_period':
                    price = 301 + (5 - base) * 137
                    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
                    await state.update_data(target_subscription_id=10)
                    callback = SimpleNamespace(
                        data='tariff_period:1:30',
                        answer=AsyncMock(),
                        message=SimpleNamespace(edit_text=AsyncMock()),
                    )
                    await inspect.unwrap(telegram.select_tariff_period)(callback, user, db, state)
                    callback.data = (
                        callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
                    )
                    checkout = (await state.get_data())['restored_tariff_checkout']
                    await inspect.unwrap(telegram.confirm_tariff_purchase)(callback, user, db, state)
                    await inspect.unwrap(telegram.confirm_tariff_purchase)(callback, user, db, state)
                    assert not await auto._auto_purchase_tariff(db, user, checkout['identity'])
                elif flow in ('telegram_daily', 'auto_daily', 'cart_return_daily'):
                    price = 101 + (5 - base) * 137
                    if flow == 'auto_daily':
                        assert await auto._auto_purchase_daily_tariff(db, user, {'tariff_id': 1, 'subscription_id': 10})
                    else:
                        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
                        await state.update_data(target_subscription_id=10)
                        callback = SimpleNamespace(
                            data='daily_tariff_confirm:1',
                            answer=AsyncMock(),
                            bot=SimpleNamespace(send_message=AsyncMock()),
                            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
                        )
                        if flow == 'cart_return_daily':
                            await state.update_data(target_subscription_id=None, active_subscription_id=20)
                            await telegram.return_to_saved_tariff_cart(
                                callback,
                                state,
                                user,
                                db,
                                {
                                    'cart_mode': 'daily_tariff_purchase',
                                    'tariff_id': 1,
                                    'subscription_id': 10,
                                    'total_price': 1,
                                    'daily_price_kopeks': 1,
                                },
                            )
                            callback.data = (
                                callback.message.edit_text.await_args.kwargs['reply_markup']
                                .inline_keyboard[0][0]
                                .callback_data
                            )
                            assert (await state.get_data())['target_subscription_id'] == 10
                        else:
                            await telegram._proceed_with_selected_tariff(callback, user, db, state, 1)
                        assert telegram.format_price_kopeks(price) in callback.message.edit_text.await_args.args[0]
                        await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
                    if flow == 'auto_daily':
                        assert not await auto._auto_purchase_daily_tariff(
                            db, user, {'tariff_id': 1, 'subscription_id': 10}
                        )
                    else:
                        await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
                elif flow == 'wallet_topup':
                    from app.database.models import LavaPayment
                    from app.services.payment import common
                    from app.services.payment.lava import LavaPaymentMixin
                    from app.services.user_cart_service import UserCartService
                    from tests.database.test_topup_classic_cart_postgres import CartStorage

                    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
                    cart = UserCartService()
                    cart._initialized = True
                    cart._redis_client = CartStorage()
                    monkeypatch.setattr(auto, 'user_cart_service', cart)
                    monkeypatch.setattr(common, 'user_cart_service', cart)
                    price = 301 + (5 - base) * 137
                    user.balance_kopeks = 0
                    payment = LavaPayment(
                        user_id=1,
                        order_id='test-' + secrets.token_hex(8),
                        amount_kopeks=1000,
                        currency='RUB',
                        is_paid=False,
                        status='pending',
                    )
                    db.add(payment)
                    await db.commit()
                    assert await cart.save_user_cart(
                        1,
                        {
                            'cart_mode': 'tariff_purchase',
                            'tariff_id': 1,
                            'period_days': 30,
                            'subscription_id': 10,
                            'total_price': price,
                            'return_to_cart': True,
                        },
                    )
                    replay_cart = await cart.get_subscription_cart(1, 10)
                    await cart.delete_global_cart_only(1)
                    actor = LavaPaymentMixin()
                    actor.bot = None
                    assert await actor._finalize_lava_payment(db, payment, trigger='test-callback')
                    assert await actor._finalize_lava_payment(db, payment, trigger='test-repeat')
                    assert await cart.get_all_subscription_carts(1) == []
                    assert not await auto._auto_purchase_tariff(db, user, replay_cart)
                else:
                    custom_traffic = 250 if flow in ('cart_return_traffic', 'auto_cart_traffic') else None
                    price = 750 + (5 - base) * 137 * 2 + (custom_traffic or 0) * 2
                    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
                    await state.update_data(
                        target_subscription_id=10, custom_days=75, custom_traffic_gb=custom_traffic or 100
                    )
                    preview = await telegram.format_custom_tariff_preview(
                        tariff,
                        75,
                        custom_traffic or 100,
                        user.balance_kopeks,
                        db_user=user,
                        db=db,
                        state=state,
                    )
                    assert f'Итого: {telegram.format_price_kopeks(price)}' in preview
                    callback = SimpleNamespace(
                        data='custom_confirm:1',
                        answer=AsyncMock(),
                        bot=SimpleNamespace(send_message=AsyncMock()),
                        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
                    )
                    if flow == 'telegram_custom':
                        callback.data = 'custom_days:1:0'
                        await inspect.unwrap(telegram.handle_custom_days_change)(callback, user, db, state)
                        callback.data = next(
                            b.callback_data
                            for row in callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
                            for b in row
                            if b.callback_data and b.callback_data.startswith('custom_confirm:')
                        )
                    if flow in ('cart_return_custom', 'cart_return_traffic'):
                        await state.update_data(target_subscription_id=None, active_subscription_id=20)
                        await telegram.return_to_saved_tariff_cart(
                            callback,
                            state,
                            user,
                            db,
                            {
                                'cart_mode': 'tariff_purchase',
                                'tariff_id': 1,
                                'period_days': 75,
                                'custom_traffic_gb': custom_traffic,
                                'subscription_id': 10,
                                'total_price': 1,
                            },
                        )
                        callback.data = (
                            callback.message.edit_text.await_args.kwargs['reply_markup']
                            .inline_keyboard[0][0]
                            .callback_data
                        )
                        assert callback.data.startswith('custom_confirm:1:') and len(callback.data.encode()) <= 64
                        assert telegram.format_price_kopeks(price) in callback.message.edit_text.await_args.args[0]
                    if flow == 'auto_cart_traffic':
                        replay_cart = {
                            'tariff_id': 1,
                            'period_days': 75,
                            'subscription_id': 10,
                            'custom_traffic_gb': 250,
                            'traffic_limit_gb': 250,
                            'total_price': 1,
                        }
                        assert await auto._auto_purchase_tariff(db, user, replay_cart)
                        assert not await auto._auto_purchase_tariff(db, user, replay_cart)
                        await telegram.return_to_saved_tariff_cart(
                            callback, state, user, db, dict(replay_cart, cart_mode='tariff_purchase')
                        )
                        callback.data = (
                            callback.message.edit_text.await_args.kwargs['reply_markup']
                            .inline_keyboard[0][0]
                            .callback_data
                        )
                        await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)

                    else:
                        await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)
                        if flow in ('telegram_custom', 'cart_return_custom', 'cart_return_traffic'):
                            checkout = (await state.get_data())['restored_tariff_checkout']
                            await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)
                            assert not await auto._auto_purchase_tariff(db, user, checkout['identity'])
                await db.refresh(user)
                await db.refresh(subscription)
                assert user.balance_kopeks == (1000 if flow == 'wallet_topup' else 10000) - price
                assert subscription.tariff_id == 1 and subscription.device_limit == 5
                assert subscription.remnawave_id == old['id']
                assert subscription.subscription_url == old['subscriptionUrl']
                transactions = (await db.scalars(select(Transaction))).all()
                assert sorted(t.amount_kopeks for t in transactions) == (
                    [-price, 1000] if flow == 'wallet_topup' else [-price]
                )
                assert len((await db.scalars(select(Subscription))).all()) == 2
                remote = await request('GET', f'/api/users/{old["id"]}')
                assert remote['hwidDeviceLimit'] == 5
                if flow in ('cart_return_traffic', 'auto_cart_traffic'):
                    assert subscription.traffic_limit_gb == 250
                    assert remote['trafficLimitBytes'] == 250 * 1024**3
                untouched = await request('GET', f'/api/users/{neighbor["id"]}')
                assert untouched['hwidDeviceLimit'] == 7 and untouched['expireAt'] == neighbor['expireAt']
                assert (await db.get(Subscription, 20)).device_limit == 7
                yield setup, old, request
        finally:
            for account in reversed(accounts):
                await request('DELETE', f'/api/users/{account["id"]}')


@pytest.mark.parametrize('base', [1, 2])
@pytest.mark.parametrize(
    'flow',
    [
        'cabinet',
        'telegram_custom',
        'telegram_period',
        'cabinet_daily',
        'telegram_daily',
        'auto_daily',
        'wallet_topup',
        'cart_return_daily',
        'cart_return_custom',
        'cart_return_traffic',
        'auto_cart_traffic',
    ],
)
async def test_purchase_price_and_cap_reach_real_panel(postgres_database, monkeypatch, base, flow):
    async with real_migration(postgres_database, monkeypatch, base, flow) as (_, old, request):
        devices = await request('GET', f'/api/hwid/devices/{old["id"]}')
        # Observation only: this verifies persistence, NOT enforcement of five devices.
        assert devices['total'] == 10


async def test_tariff_maximum_applies_to_previously_registered_hwids(postgres_database, monkeypatch):
    """Known RED acceptance criterion; do not replace with HTTP200 assertions or xfail."""
    async with real_migration(postgres_database, monkeypatch, 1) as (http, old, _):
        admitted = 0
        for index in range(11):
            response = await http.get(
                '/api/sub/' + old['shortUuid'],
                headers={
                    'x-hwid': f'audit-device-{index:02}',
                    'User-Agent': 'Happ/3.0.0',
                },
            )
            assert response.status_code == 200
            decoded = base64.b64decode(response.content).decode()
            denied = 'Limit%20of%20devices%20reached' in decoded
            if index == 10:
                assert denied and response.headers.get('announce')
            else:
                admitted += not denied
        assert admitted <= 5, f'After a real purchase, panel admitted {admitted} old HWIDs with tariff maximum=5'
