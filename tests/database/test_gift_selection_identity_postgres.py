"""Two explicitly confirmed gift selections must not share a payment identity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.database.crud.user import get_user_by_id
from app.database.models import Base, GuestPurchase, SystemSetting, Tariff, Transaction, User
from app.handlers.subscription import gift
from app.services.gift_purchase_service import GIFT_ENABLED_KEY, purchase_gift_from_balance
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('change', ['tariff', 'period'])
async def test_second_selection_while_first_result_waits_has_separate_idempotent_payment(
    postgres_database,
    carts,
    monkeypatch,
    change,
):
    service, uid, _ = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('gift result', None))
    first_checkout = uuid4().hex
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    first_selection = {
        'gift_checkout_id': first_checkout,
        'gift_checkout_selection': [1, 30],
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    await state.set_state(gift.GiftPurchaseStates.confirming_purchase)
    await state.set_data(first_selection)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=uid, telegram_id=uid, language='ru', status='active', balance_kopeks=1000))
        db.add(
            Tariff(
                id=1,
                name='First',
                is_active=True,
                show_in_gift=True,
                device_limit=1,
                traffic_limit_gb=100,
                period_prices={'30': 100, '90': 200},
            )
        )
        db.add(
            Tariff(
                id=2,
                name='Second',
                is_active=True,
                show_in_gift=True,
                device_limit=2,
                traffic_limit_gb=200,
                period_prices={'30': 200},
            )
        )
        await db.commit()
        user = await get_user_by_id(db, uid)
        await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
        independent = await service.get_subscription_cart(uid, 10)
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': first_checkout,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 100,
                'return_to_cart': True,
            },
        )
        delivering = asyncio.Event()
        release = asyncio.Event()

        async def pause_first_result(*args, **kwargs):
            # The real payment is already committed. Only Telegram delivery
            # waits, so this session has no concurrent database operations.
            assert user.balance_kopeks == 900
            delivering.set()
            await release.wait()

        first_callback = SimpleNamespace(
            data=gift._gift_action_callback('gift_confirm', first_selection, await state.get_state()),
            message=SimpleNamespace(edit_text=AsyncMock(side_effect=pause_first_result)),
            answer=AsyncMock(),
            bot=AsyncMock(),
        )
        first_task = asyncio.create_task(gift.handle_gift_confirm(first_callback, user, db, state))
        try:
            await asyncio.wait_for(delivering.wait(), 10)
            tariff_id, days = (2, 30) if change == 'tariff' else (1, 90)
            await state.set_state(gift.GiftPurchaseStates.selecting_period)
            await state.update_data(gift_tariff_id=tariff_id)
            second_callback = SimpleNamespace(
                data=f'gift_period:{tariff_id}:{days}',
                message=SimpleNamespace(edit_text=AsyncMock()),
                answer=AsyncMock(),
                bot=AsyncMock(),
            )
            await gift.handle_gift_period_select(second_callback, user, db, state)
            second_selection = await state.get_data()
            keyboard = second_callback.message.edit_text.await_args.kwargs['reply_markup']
            second_callback.data = next(
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data.startswith('gift_confirm:')
            )
            await gift.handle_gift_confirm(second_callback, user, db, state)
            await db.refresh(user)
            purchases = list(await db.scalars(select(GuestPurchase)))
            assert len(purchases) == 2
            assert user.balance_kopeks == 700
            second_checkout = second_selection['gift_checkout_id']
            assert second_checkout != first_checkout
            assert {item.idempotency_key for item in purchases} == {first_checkout, second_checkout}
            assert sorted(item.amount_kopeks for item in await db.scalars(select(Transaction))) == [-200, -100]
            assert await service.has_topup_intent(uid, independent)
            assert await service.get_user_cart(uid) is None
            await state.set_state('third_selection')
            await state.set_data({'gift_checkout_id': 'third'})
        finally:
            release.set()
            await asyncio.wait_for(first_task, 10)
        assert await state.get_state() == 'third_selection'
        assert await state.get_data() == {'gift_checkout_id': 'third'}
        for checkout, selected_tariff, selected_days, price in (
            (first_checkout, 1, 30, 100),
            (second_checkout, tariff_id, days, 200),
        ):
            replay = await purchase_gift_from_balance(
                db,
                buyer_id=uid,
                tariff_id=selected_tariff,
                period_days=selected_days,
                expected_price_kopeks=price,
                idempotency_key=checkout,
                source='bot',
            )
            assert replay.is_idempotent_replay
        await db.refresh(user)
        assert user.balance_kopeks == 700
        assert len(list(await db.scalars(select(Transaction)))) == 2
