"""A topup cannot buy a gift at a manually refreshed, unconfirmed price."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, GuestPurchase, SystemSetting, Tariff, Transaction, User
from app.handlers.subscription import gift
from app.services import subscription_auto_purchase_service as auto
from app.services.gift_purchase_service import GIFT_ENABLED_KEY
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('old_price', [100, 300])
@pytest.mark.parametrize('confirm_before_topup', [False, True])
async def test_gift_new_price_requires_consent_before_any_debit(
    postgres_database,
    carts,
    monkeypatch,
    old_price,
    confirm_before_topup,
):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(auto, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(auto, 'send_gift_result_message', AsyncMock(return_value=SimpleNamespace(message_id=1)))
    checkout = uuid4().hex
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    await state.set_state(gift.GiftPurchaseStates.confirming_purchase)
    await state.set_data(
        {
            'gift_checkout_id': checkout,
            'gift_tariff_id': 1,
            'gift_period_days': 30,
            'gift_expected_price_kopeks': old_price,
        }
    )
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=uid, telegram_id=uid, language='ru', status='active', balance_kopeks=50))
        db.add(
            Tariff(
                id=1,
                name='Gift',
                is_active=True,
                show_in_gift=True,
                device_limit=1,
                traffic_limit_gb=100,
                period_prices={'30': 200},
            )
        )
        await db.commit()
        user = await get_user_by_id(db, uid)
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': old_price,
                'user_id': uid,
                'return_to_cart': True,
            },
        )
        callback = SimpleNamespace(
            data='return_to_gift_cart',
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
            bot=AsyncMock(),
        )
        await gift.handle_return_to_gift_cart(callback, user, db, state)
        if not confirm_before_topup:
            # The actual wallet is now sufficient. A webhook must still refuse
            # the refreshed quote until the user presses its own confirm button.
            user.balance_kopeks = 300
            await db.commit()
            assert not await auto.auto_purchase_saved_cart_after_topup(db, user, bot=AsyncMock())
            await db.refresh(user)
            assert user.balance_kopeks == 300
            assert list(await db.scalars(select(GuestPurchase))) == []
            assert list(await db.scalars(select(Transaction))) == []
        cart = await service.get_user_cart(uid)
        assert cart['total_price'] == 200 and cart['return_to_cart'] is False
        assert not await service.has_topup_intent(uid, cart)
        keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
        confirm_data = next(
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data.startswith('gift_confirm:')
        )
        callback.data = confirm_data
        await gift.handle_gift_confirm(callback, user, db, state)
        if confirm_before_topup:
            authorized = await service.get_user_cart(uid)
            assert authorized['total_price'] == 200 and authorized['return_to_cart'] is True
            assert await service.has_topup_intent(uid, authorized)
            assert user.balance_kopeks == 50
            user.balance_kopeks = 300
            await db.commit()
            assert await auto.auto_purchase_saved_cart_after_topup(db, user, bot=AsyncMock())
        await db.refresh(user)
        assert user.balance_kopeks == 100
        purchases = list(await db.scalars(select(GuestPurchase)))
        ledger = list(await db.scalars(select(Transaction)))
        assert len(purchases) == 1 and purchases[0].status == 'paid'
        assert len(ledger) == 1 and ledger[0].type == 'gift_payment' and ledger[0].amount_kopeks == -200
        assert await service.get_user_cart(uid) is None
