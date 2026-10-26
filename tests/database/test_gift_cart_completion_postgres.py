"""Actual gift debit/ledger completion must preserve a concurrently saved cart."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, GuestPurchase, Subscription, SystemSetting, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.gift_purchase_service import GIFT_ENABLED_KEY, purchase_gift_from_balance
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('manual_resume', [False, True])
@pytest.mark.parametrize('replacement_mode', [None, 'gift_purchase', 'tariff_purchase'])
async def test_committed_gift_cleanup_preserves_new_cart_and_single_debit(
    postgres_database, carts, monkeypatch, replacement_mode, manual_resume
):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(auto, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    now = datetime.now(UTC)
    checkout = uuid4().hex
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=uid, telegram_id=uid, language='ru', status='active', balance_kopeks=1000))
        db.add(
            Tariff(
                id=1,
                name='Gift',
                is_active=True,
                show_in_gift=True,
                device_limit=1,
                traffic_limit_gb=100,
                period_prices={'30': 100},
            )
        )
        db.add(
            Subscription(
                id=10,
                user_id=uid,
                tariff_id=None,
                status='active',
                is_trial=False,
                remnawave_short_id='test-classic',
                device_limit=3,
                end_date=now + timedelta(days=10),
            )
        )
        await db.commit()
        user = await get_user_by_id(db, uid)
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'tariff_id': 1, 'return_to_cart': True}
        )
        independent = await service.get_subscription_cart(uid, 10)
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 100,
                'user_id': uid,
                'return_to_cart': True,
            },
        )
        original = await service.get_user_cart(uid)
        replacement = None

        async def deliver(**kwargs):
            nonlocal replacement
            # Gift payment has committed before result delivery yields to a
            # different checkout request. Storage is real Valkey throughout.
            assert (await db.get(User, uid)).balance_kopeks == 900
            if replacement_mode:
                await service.save_user_cart(
                    uid,
                    {
                        'cart_mode': replacement_mode,
                        'gift_checkout_id': uuid4().hex,
                        'tariff_id': 1,
                        'period_days': 30,
                        'total_price': 100,
                        'subscription_id': 20 if replacement_mode == 'tariff_purchase' else None,
                        'user_id': uid,
                        'return_to_cart': True,
                    },
                )
                replacement = await service.get_user_cart(uid)
            return SimpleNamespace(message_id=1)

        monkeypatch.setattr(auto, 'send_gift_result_message', deliver)
        assert await auto.auto_purchase_saved_cart_after_topup(db, user, bot=AsyncMock())
        assert await service.get_user_cart(uid) == replacement
        assert await service.has_topup_intent(uid, independent)
        assert not await service.has_topup_intent(uid, original)
        if replacement_mode:
            assert await service.has_topup_intent(uid, replacement)
        if manual_resume:
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey
            from aiogram.fsm.storage.memory import MemoryStorage

            from app.handlers.subscription import gift

            # Recover an already committed gift from its still-saved checkout,
            # then start a different selection during result delivery.
            await service.save_user_cart(uid, original)
            state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
            await state.set_state('old_selection')
            await state.set_data({'gift_checkout_id': checkout})

            async def manual_delivery(*args, **kwargs):
                nonlocal replacement
                if replacement is not None:
                    await service.save_user_cart(uid, replacement)
                    replacement = await service.get_user_cart(uid)
                await state.set_state('new_selection')
                await state.set_data({'gift_checkout_id': 'new'})

            monkeypatch.setattr(gift, 'user_cart_service', service)
            monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
            monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('gift result', None))
            callback = SimpleNamespace(
                message=SimpleNamespace(edit_text=AsyncMock(side_effect=manual_delivery)),
                answer=AsyncMock(),
                bot=AsyncMock(),
            )
            await gift.handle_return_to_gift_cart(callback, user, db, state)
            assert await state.get_state() == 'new_selection'
            assert await state.get_data() == {'gift_checkout_id': 'new'}
            assert await service.get_user_cart(uid) == replacement
            assert await service.has_topup_intent(uid, independent)
        # A retry of the original durable gift checkout must not debit again.
        replay = await purchase_gift_from_balance(
            db,
            buyer_id=uid,
            tariff_id=1,
            period_days=30,
            expected_price_kopeks=100,
            idempotency_key=checkout,
            source='bot',
        )
        assert replay.is_idempotent_replay
        await db.refresh(user)
        assert user.balance_kopeks == 900
        gifts = list(await db.scalars(select(GuestPurchase)))
        assert len(gifts) == 1 and gifts[0].status == 'paid'
        ledger = list(await db.scalars(select(Transaction)))
        assert len(ledger) == 1 and ledger[0].type == 'gift_payment' and ledger[0].amount_kopeks == -100
        classic = await db.get(Subscription, 10)
        assert (classic.tariff_id, classic.device_limit, classic.end_date) == (None, 3, now + timedelta(days=10))
        assert await service.get_user_cart(uid) == replacement

        # An old displayed button must not debit the newly selected gift, nor
        # cancel its real cached cart, even though the new checkout is valid.
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        from app.handlers.subscription import gift

        new_checkout = uuid4().hex
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
        selection = {
            'gift_checkout_id': new_checkout,
            'gift_tariff_id': 1,
            'gift_period_days': 30,
            'gift_expected_price_kopeks': 100,
        }
        await state.set_state(gift.GiftPurchaseStates.confirming_purchase)
        await state.set_data(selection)
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': new_checkout,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 100,
                'user_id': uid,
                'return_to_cart': True,
            },
        )
        new_cart = await service.get_user_cart(uid)
        monkeypatch.setattr(gift, 'user_cart_service', service)
        monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
        monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))
        monkeypatch.setattr(gift, 'show_subscription_info', AsyncMock())
        for action, handler in [('gift_confirm', gift.handle_gift_confirm), ('gift_cancel', gift.handle_gift_cancel)]:
            old_payload = gift._gift_action_callback(
                action,
                {**selection, 'gift_checkout_id': checkout},
                gift.GiftPurchaseStates.confirming_purchase.state,
            )
            callback = SimpleNamespace(
                data=old_payload,
                message=SimpleNamespace(edit_text=AsyncMock()),
                answer=AsyncMock(),
                bot=AsyncMock(),
            )
            await handler(callback, user, db, state)
            callback.message.edit_text.assert_not_awaited()
            assert await state.get_data() == selection
            assert await service.get_user_cart(uid) == new_cart
            assert await service.has_topup_intent(uid, new_cart)
        await db.refresh(user)
        assert user.balance_kopeks == 900
        assert len(list(await db.scalars(select(GuestPurchase)))) == 1
        assert len(list(await db.scalars(select(Transaction)))) == 1
