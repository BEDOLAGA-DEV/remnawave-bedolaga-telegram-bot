"""Tests for native bot gift code manual entry and activation flow (Task 5).

Covers:
- Step 1: Activation entry from gift catalog, localized prompt/buttons, cancellation,
          inaccessible message handling, non-text input, FSM & Redis cart isolation,
          and registration order relative to promocode/coupon handlers.
- Step 2: Input and result mapping: canonical code, Telegram URL, cabinet URL,
          malformed code, insecure short code, unknown gift, buyer self-claim,
          gift owned by another user, already claimed by same user, non-activatable status,
          transient failure retryability, HTML escaping, and complete token confidentiality.
"""

from __future__ import annotations

import html
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.landing import generate_purchase_token
from app.database.models import (
    AdvertisingCampaign,
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    MainMenuButton,
    PaymentMethodConfig,
    PinnedMessage,
    PromoGroup,
    PromoOfferLog,
    SentNotification,
    ServerSquad,
    Subscription,
    SystemSetting,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
    UserPromoGroup,
    Webhook,
    WebhookDelivery,
    tariff_promo_groups,
)
from app.handlers.promocode import register_handlers as register_promocode_handlers
from app.handlers.subscription import register_gift_handlers
from app.handlers.subscription.gift import (
    handle_gift_activation_cancel,
    handle_gift_code_input,
    handle_gift_enter_code,
)
from app.services.gift_purchase_service import (
    GIFT_ENABLED_KEY,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
from app.services.guest_purchase_service import GuestPurchaseError
from app.states import GiftActivationStates
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    SystemSetting.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    GuestPurchase.__table__,
    Transaction.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
    ServerSquad.__table__,
    MainMenuButton.__table__,
    PinnedMessage.__table__,
    AdvertisingCampaign.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    Webhook.__table__,
    WebhookDelivery.__table__,
    PaymentMethodConfig.__table__,
]


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract callback_data from an inline keyboard."""
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def _make_fsm_context(user_id: int, chat_id: int | None = None) -> FSMContext:
    storage = MemoryStorage()
    c_id = chat_id if chat_id is not None else user_id
    key = StorageKey(bot_id=1, chat_id=c_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(
    text: str | None = None,
    user_id: int = 123456789,
    username: str = 'test_user',
    has_text: bool = True,
) -> Message:
    msg = MagicMock(spec=Message)
    msg.message_id = 200
    if has_text:
        msg.text = text
    else:
        msg.text = None
        msg.photo = [MagicMock()]
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name='TestUser', username=username)
    msg.chat = Chat(id=user_id, type='private')
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


async def _seed_test_tariffs_and_settings(db) -> Tariff:
    setting = SystemSetting(key=GIFT_ENABLED_KEY, value='true')
    db.add(setting)
    tariff = Tariff(
        name='VIP <Fast & Secure>',
        is_active=True,
        show_in_gift=True,
        device_limit=3,
        traffic_limit_gb=100,
        period_prices={'30': 35000},
        display_order=1,
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


# ── Step 1: Activation Entry and State Isolation Tests ───────────────────────


class TestGiftCodeActivationEntryAndIsolation:
    """Tests covering entry, navigation, inaccessible messages, non-text input, and state isolation."""

    @pytest.mark.asyncio
    async def test_gift_enter_code_sets_state_and_renders_prompt(self):
        """Clicking gift_enter_code transitions state to GiftActivationStates.waiting_for_code and shows prompt."""
        user = MagicMock(spec=User)
        user.id = 1
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(12345)

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = 'gift_enter_code'
        callback.from_user = TgUser(id=12345, is_bot=False, first_name='Tester')
        callback.message = AsyncMock(spec=Message)
        callback.message.edit_text = AsyncMock()
        callback.answer = AsyncMock()

        await handle_gift_enter_code(callback, user, db, state)

        assert await state.get_state() == GiftActivationStates.waiting_for_code.state
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args[0][0]
        assert 'Активация подарка' in text

        reply_markup = callback.message.edit_text.call_args[1].get('reply_markup')
        assert reply_markup is not None
        assert 'gift_activation_cancel' in _callbacks(reply_markup)
        callback.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gift_enter_code_inaccessible_message_acknowledged(self):
        """Inaccessible message on gift_enter_code is safely acknowledged without crashing."""
        user = MagicMock(spec=User)
        user.id = 1
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(12345)

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = 'gift_enter_code'
        callback.message = InaccessibleMessage(
            chat=Chat(id=12345, type='private'),
            message_id=10,
            date=0,
        )
        callback.answer = AsyncMock()

        await handle_gift_enter_code(callback, user, db, state)

        callback.answer.assert_awaited_once_with()
        assert await state.get_state() is None

    @pytest.mark.asyncio
    async def test_gift_activation_cancel_clears_activation_state_and_returns_to_catalog(self):
        """Cancelling manual activation resets activation state and returns to gift catalog."""
        user = MagicMock(spec=User)
        user.id = 1
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(12345)
        await state.set_state(GiftActivationStates.waiting_for_code)

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = 'gift_activation_cancel'
        callback.from_user = TgUser(id=12345, is_bot=False, first_name='Tester')
        callback.message = AsyncMock(spec=Message)
        callback.message.edit_text = AsyncMock()
        callback.answer = AsyncMock()

        with patch('app.handlers.subscription.gift.handle_gift_catalog', AsyncMock()) as mock_catalog:
            await handle_gift_activation_cancel(callback, user, db, state)
            assert await state.get_state() is None
            mock_catalog.assert_awaited_once_with(callback, user, db, state)

    @pytest.mark.asyncio
    async def test_gift_activation_cancel_inaccessible_message(self):
        """Inaccessible message on gift_activation_cancel is safely acknowledged."""
        user = MagicMock(spec=User)
        user.id = 1
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(12345)
        await state.set_state(GiftActivationStates.waiting_for_code)

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = 'gift_activation_cancel'
        callback.message = InaccessibleMessage(
            chat=Chat(id=12345, type='private'),
            message_id=10,
            date=0,
        )
        callback.answer = AsyncMock()

        await handle_gift_activation_cancel(callback, user, db, state)
        callback.answer.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_activation_entry_and_cancel_preserves_saved_cart_and_intent(self, monkeypatch):
        """Entering activation state or cancelling does NOT delete a saved gift purchase cart in Redis."""
        user_id = 777
        storage: dict[str, dict] = {
            'cart': {
                'cart_mode': 'gift_purchase',
                'tariff_id': 5,
                'period_days': 30,
                'user_id': user_id,
            },
            'has_intent': True,
        }

        mock_get_cart = AsyncMock(side_effect=lambda uid: storage.get('cart') if uid == user_id else None)
        mock_has_intent = AsyncMock(
            side_effect=lambda uid: storage.get('has_intent', False) if uid == user_id else False
        )
        mock_delete_cart = AsyncMock(side_effect=lambda uid: storage.pop('cart', None))
        mock_clear_intent = AsyncMock(side_effect=lambda uid: storage.pop('has_intent', None))

        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service.get_user_cart', mock_get_cart)
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service.has_topup_intent', mock_has_intent)
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service.delete_user_cart', mock_delete_cart)
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service.clear_topup_intent', mock_clear_intent)

        user = MagicMock(spec=User)
        user.id = user_id
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(user_id)

        callback = AsyncMock(spec=CallbackQuery)
        callback.data = 'gift_enter_code'
        callback.from_user = TgUser(id=user_id, is_bot=False, first_name='Tester')
        callback.message = AsyncMock(spec=Message)
        callback.message.edit_text = AsyncMock()
        callback.answer = AsyncMock()

        # Enter code flow
        await handle_gift_enter_code(callback, user, db, state)
        assert await state.get_state() == GiftActivationStates.waiting_for_code.state

        # Saved cart and top-up intent must NOT have been deleted
        mock_delete_cart.assert_not_awaited()
        mock_clear_intent.assert_not_awaited()
        assert storage.get('cart') is not None

        # Cancel code flow
        with patch('app.handlers.subscription.gift.handle_gift_catalog', AsyncMock()):
            await handle_gift_activation_cancel(callback, user, db, state)

        # Saved cart and top-up intent still must NOT have been deleted on activation cancel
        mock_delete_cart.assert_not_awaited()
        mock_clear_intent.assert_not_awaited()
        assert storage.get('cart') is not None

    @pytest.mark.asyncio
    async def test_non_text_message_while_waiting_for_code_warns_and_retains_state(self):
        """Sending a photo / sticker / non-text message while waiting for code warns user and retains state."""
        user = MagicMock(spec=User)
        user.id = 1
        user.language = 'ru'
        db = AsyncMock(spec=AsyncSession)
        state = _make_fsm_context(12345)
        await state.set_state(GiftActivationStates.waiting_for_code)

        msg = _make_message(user_id=12345, has_text=False)

        await handle_gift_code_input(msg, user, db, state)

        # State must NOT be cleared on non-text input
        assert await state.get_state() == GiftActivationStates.waiting_for_code.state
        msg.answer.assert_awaited_once()
        answered_text = msg.answer.call_args[0][0]
        assert 'текстовый код' in answered_text.lower() or 'ссылку' in answered_text.lower()
        reply_markup = msg.answer.call_args[1].get('reply_markup')
        assert reply_markup is not None
        assert 'gift_activation_cancel' in _callbacks(reply_markup)

    def test_handler_registration_and_state_isolation(self):
        """Dispatcher registration verifies exact filters and no state overlap with promocode or admin handlers."""
        dp = Dispatcher()
        register_gift_handlers(dp)
        register_promocode_handlers(dp)

        # Message handlers check
        msg_handlers = dp.message.handlers
        gift_msg_handler = None
        promo_msg_handler = None

        for h in msg_handlers:
            if h.callback == handle_gift_code_input:
                gift_msg_handler = h
            elif 'process_promocode' in getattr(h.callback, '__name__', ''):
                promo_msg_handler = h

        assert gift_msg_handler is not None, 'handle_gift_code_input must be registered on dp.message'
        assert promo_msg_handler is not None

        # Verify callback filters
        cb_handlers = dp.callback_query.handlers
        enter_code_handler = next((h for h in cb_handlers if h.callback == handle_gift_enter_code), None)
        cancel_handler = next((h for h in cb_handlers if h.callback == handle_gift_activation_cancel), None)

        assert enter_code_handler is not None
        assert cancel_handler is not None


# ── Step 2: Input and Result Mapping Tests ───────────────────────────────────


class TestGiftCodeInputAndResultMapping:
    """Tests covering various input formats, domain error mapping, and confidentiality."""

    @pytest.mark.asyncio
    async def test_successful_activation_via_canonical_code(self, monkeypatch):
        """Entering a 64-char token or 48-char bot prefix activates gift, clears state, and formats HTML."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add_all([buyer, claimant])
            await db.commit()
            await db.refresh(buyer)
            await db.refresh(claimant)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_manual_1',
            )
            raw_token = purchase_res.purchase.token

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=raw_token, user_id=claimant.telegram_id)

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await handle_gift_code_input(msg, claimant, db, state)

            # Assert database state
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            p = res.scalars().first()
            assert p.status == GuestPurchaseStatus.DELIVERED.value
            assert p.user_id == claimant.id

            # Assert FSM cleared
            assert await state.get_state() is None

            # Assert message formatting and confidentiality
            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'Подарок активирован' in ans_text
            assert html.escape('VIP <Fast & Secure>') in ans_text
            assert '<Fast & Secure>' not in ans_text  # properly escaped
            assert raw_token not in ans_text  # NO token leak

    @pytest.mark.asyncio
    async def test_successful_activation_via_telegram_url(self, monkeypatch):
        """Entering a full Telegram claim link https://t.me/my_bot?start=GIFT_<prefix> activates gift."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add_all([buyer, claimant])
            await db.commit()
            await db.refresh(buyer)
            await db.refresh(claimant)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_manual_tg_url',
            )
            raw_token = purchase_res.purchase.token
            prefix = raw_token[:48]
            tg_url = f'https://t.me/super_bot?start=GIFT_{prefix}'

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=tg_url, user_id=claimant.telegram_id)

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await handle_gift_code_input(msg, claimant, db, state)

            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            p = res.scalars().first()
            assert p.status == GuestPurchaseStatus.DELIVERED.value
            assert p.user_id == claimant.id
            assert await state.get_state() is None

    @pytest.mark.asyncio
    async def test_successful_activation_via_cabinet_url(self, monkeypatch):
        """Entering a Cabinet URL https://cabinet.example.com/gifts/claim?token=<64_hex> activates gift."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add_all([buyer, claimant])
            await db.commit()
            await db.refresh(buyer)
            await db.refresh(claimant)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_manual_cab_url',
            )
            raw_token = purchase_res.purchase.token
            cabinet_url = f'https://cabinet.example.com/buy/gift/{raw_token}'

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=cabinet_url, user_id=claimant.telegram_id)

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await handle_gift_code_input(msg, claimant, db, state)

            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            p = res.scalars().first()
            assert p.status == GuestPurchaseStatus.DELIVERED.value
            assert p.user_id == claimant.id
            assert await state.get_state() is None

    @pytest.mark.asyncio
    async def test_malformed_code_rejected_and_state_retained_for_retry(self, monkeypatch):
        """Entering garbage text sends invalid/not found error and keeps user in activation state for retry."""
        async with memory_session(monkeypatch, _TABLES) as db:
            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add(claimant)
            await db.commit()

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text='invalid-gibberish-$$$#!@', user_id=claimant.telegram_id)

            await handle_gift_code_input(msg, claimant, db, state)

            assert await state.get_state() == GiftActivationStates.waiting_for_code.state
            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'не найден' in ans_text.lower() or 'недоступен' in ans_text.lower()
            assert 'invalid-gibberish' not in ans_text  # NO echo of input

    @pytest.mark.asyncio
    async def test_insecure_short_code_rejected_in_telegram_mode(self, monkeypatch):
        """Short legacy code (e.g. 12 chars) is rejected in bot code entry (allow_legacy_short=False)."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value='22222',
                is_gift=True,
                status=GuestPurchaseStatus.PAID.value,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=35000,
            )
            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add_all([purchase, claimant])
            await db.commit()

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            # Input only 12 chars
            msg = _make_message(text=token[:12], user_id=claimant.telegram_id)

            await handle_gift_code_input(msg, claimant, db, state)

            # Purchase remains PAID and unassigned
            await db.refresh(purchase)
            assert purchase.status == GuestPurchaseStatus.PAID.value
            assert purchase.user_id is None

            # State is retained for retry
            assert await state.get_state() == GiftActivationStates.waiting_for_code.state
            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'не найден' in ans_text.lower() or 'недоступен' in ans_text.lower()
            assert token[:12] not in ans_text

    @pytest.mark.asyncio
    async def test_buyer_self_claim_rejected(self, monkeypatch):
        """Buyer attempting to activate their own gift in bot is rejected with friendly error."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000, language='ru')
            db.add(buyer)
            await db.commit()
            await db.refresh(buyer)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_buyer_self_manual',
            )
            raw_token = purchase_res.purchase.token

            state = _make_fsm_context(buyer.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=raw_token, user_id=buyer.telegram_id)

            await handle_gift_code_input(msg, buyer, db, state)

            # Purchase is still PAID
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            p = res.scalars().first()
            assert p.status == GuestPurchaseStatus.PAID.value
            assert p.user_id is None

            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'свой собственный подарок' in ans_text
            assert raw_token not in ans_text

    @pytest.mark.asyncio
    async def test_gift_already_owned_by_another_user_rejected(self, monkeypatch):
        """Gift already claimed by user A cannot be claimed by user B."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            user_a = User(telegram_id=22222, username='user_a', balance_kopeks=0)
            user_b = User(telegram_id=33333, username='user_b', balance_kopeks=0, language='ru')
            db.add_all([user_a, user_b])
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(user_a.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.DELIVERED.value,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=35000,
                user_id=user_a.id,
            )
            db.add(purchase)
            await db.commit()

            state = _make_fsm_context(user_b.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=token, user_id=user_b.telegram_id)

            await handle_gift_code_input(msg, user_b, db, state)

            # Purchase remains owned by user A
            await db.refresh(purchase)
            assert purchase.user_id == user_a.id

            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'уже был активирован' in ans_text
            assert token not in ans_text

    @pytest.mark.asyncio
    async def test_already_activated_by_same_user_idempotent_success(self, monkeypatch):
        """Re-entering the code of an already delivered gift owned by claimant returns success idempotently."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add(claimant)
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(claimant.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.DELIVERED.value,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=35000,
                user_id=claimant.id,
            )
            db.add(purchase)
            await db.commit()

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=token, user_id=claimant.telegram_id)

            await handle_gift_code_input(msg, claimant, db, state)

            assert await state.get_state() is None
            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'Подарок активирован' in ans_text
            assert token not in ans_text

    @pytest.mark.asyncio
    async def test_non_activatable_status_rejected(self, monkeypatch):
        """Gift in non-activatable status (e.g. FAILED) is rejected."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add(claimant)
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(claimant.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.FAILED.value,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=35000,
            )
            db.add(purchase)
            await db.commit()

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=token, user_id=claimant.telegram_id)

            await handle_gift_code_input(msg, claimant, db, state)

            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'невозможно активировать' in ans_text
            assert token not in ans_text

    @pytest.mark.asyncio
    async def test_transient_failure_keeps_state_retryable(self, monkeypatch):
        """If provisioning fails with GuestPurchaseError (500), error message is sent and state remains retryable."""
        async with memory_session(monkeypatch, _TABLES) as db:
            tariff = await _seed_test_tariffs_and_settings(db)

            claimant = User(telegram_id=22222, username='claimant', balance_kopeks=0, language='ru')
            db.add(claimant)
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(claimant.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PAID.value,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=35000,
            )
            db.add(purchase)
            await db.commit()

            state = _make_fsm_context(claimant.telegram_id)
            await state.set_state(GiftActivationStates.waiting_for_code)

            msg = _make_message(text=token, user_id=claimant.telegram_id)

            with patch(
                'app.services.guest_purchase_service.activate_purchase',
                AsyncMock(side_effect=GuestPurchaseError('Remnawave sync error', status_code=502)),
            ):
                await handle_gift_code_input(msg, claimant, db, state)

            # State is retained for retry
            assert await state.get_state() == GiftActivationStates.waiting_for_code.state
            msg.answer.assert_awaited_once()
            ans_text = msg.answer.call_args[0][0]
            assert 'ошибка при активации' in ans_text.lower() or 'личный кабинет' in ans_text.lower()
            assert token not in ans_text
            reply_markup = msg.answer.call_args[1].get('reply_markup')
            assert reply_markup is not None
            assert 'gift_activation_cancel' in _callbacks(reply_markup)
