"""A paid daily purchase must not erase a later cart or dialog selection."""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('multi,fresh', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('replacement', ['none', 'state', 'data', 'both', 'same_cart', 'other_cart'])
async def test_daily_completion_preserves_new_checkout(
    postgres_database, monkeypatch, carts, multi, fresh, replacement, cart_kind='daily'
):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(telegram, 'user_cart_service', service)
    monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=True))
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
    state = FSMContext(storage=RedisStorage(redis=client), key=StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    source_id = None if fresh else 10
    original_data = {'target_subscription_id': source_id, 'selected_tariff_id': 1}
    original_cart = {
        'cart_mode': 'daily_tariff_purchase',
        'subscription_id': source_id,
        'tariff_id': 1,
        'return_to_cart': True,
    }
    await state.set_state('daily-confirm')
    await state.set_data(original_data)
    if cart_kind == 'gift':
        original_cart['cart_mode'] = 'gift_purchase'
    elif cart_kind == 'other_tariff':
        original_cart['tariff_id'] = 2
    if cart_kind != 'absent':
        assert await service.save_user_cart(uid, original_cart)
    new_cart = await service.get_user_cart(uid) if cart_kind in ('gift', 'other_tariff') else None

    async def after_payment(*args, **kwargs):
        nonlocal new_cart
        if replacement in ('state', 'both'):
            await state.set_state('new-selection')
        if replacement in ('data', 'both'):
            await state.set_data({'target_subscription_id': 20, 'selected_tariff_id': 2})
        if replacement in ('same_cart', 'other_cart', 'both'):
            new_cart = dict(original_cart)
            if replacement != 'same_cart':
                new_cart.update(subscription_id=20, tariff_id=2)
            assert await service.save_user_cart(uid, new_cart)
            new_cart = await service.get_user_cart(uid)

    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', after_payment)
    callback = SimpleNamespace(
        data='daily_tariff_confirm:1',
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    try:
        async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
            db.add(User(id=uid, telegram_id=uid, language='ru', balance_kopeks=1000))
            db.add(
                Tariff(
                    id=1,
                    name='Daily',
                    is_active=True,
                    is_daily=True,
                    daily_price_kopeks=100,
                    device_limit=1,
                    traffic_limit_gb=100,
                    allowed_squads=['test-squad'],
                )
            )
            if not fresh:
                db.add(
                    Subscription(
                        id=10,
                        user_id=uid,
                        tariff_id=None,
                        is_trial=False,
                        status='active',
                        device_limit=1,
                        end_date=datetime.now(UTC) + timedelta(days=10),
                    )
                )
            await db.commit()
            user = await get_user_by_id(db, uid)
            await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
            await db.refresh(user)
            payments = (await db.scalars(select(Transaction))).all()
            assert user.balance_kopeks == 900
            assert len(payments) == 1 and payments[0].amount_kopeks == -100
            assert payments[0].is_completed

        if replacement in ('state', 'data', 'both'):
            assert await state.get_state() == ('new-selection' if replacement in ('state', 'both') else 'daily-confirm')
            assert await state.get_data() == (
                {'target_subscription_id': 20, 'selected_tariff_id': 2}
                if replacement in ('data', 'both')
                else original_data
            )
            callback.message.edit_text.assert_not_awaited()
        else:
            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert '🎉' in callback.message.edit_text.await_args.args[0]
        assert await service.get_user_cart(uid) == new_cart
        if new_cart is not None:
            assert await service.has_topup_intent(uid, new_cart)
            if new_cart.get('subscription_id'):
                assert await service.get_subscription_cart(uid, new_cart['subscription_id']) == new_cart
        if source_id is not None and (new_cart is None or new_cart.get('subscription_id') != source_id):
            assert await service.get_subscription_cart(uid, source_id) is None
    finally:
        await state.clear()


@pytest.mark.parametrize('multi,fresh', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('cart_kind', ['absent', 'gift', 'other_tariff'])
async def test_daily_completion_does_not_consume_unrelated_cart(
    postgres_database, monkeypatch, carts, multi, fresh, cart_kind
):
    await test_daily_completion_preserves_new_checkout(
        postgres_database, monkeypatch, carts, multi, fresh, 'none', cart_kind=cart_kind
    )
