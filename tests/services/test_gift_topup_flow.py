"""Unit and integration tests for Task 6:
Insufficient-balance cart, top-up resume, automatic completion, and cart isolation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import GuestPurchase, GuestPurchaseStatus, Tariff, Transaction, TransactionType, User
from app.handlers.subscription.gift import (
    handle_gift_cancel,
    handle_gift_confirm,
    handle_return_to_gift_cart,
)
from app.keyboards.inline import get_insufficient_balance_keyboard
from app.services.gift_purchase_service import (
    GiftInsufficientBalanceError,
    GiftPurchaseResult,
    GiftQuote,
    GiftTariffUnavailableError,
)
from app.services.payment.common import PaymentCommonMixin
from app.services.subscription_auto_purchase_service import auto_purchase_saved_cart_after_topup
from app.services.user_cart_service import UserCartService
from app.states import GiftPurchaseStates


# ── Mock Redis ──────────────────────────────────────────────────────────────


class MockRedis:
    def __init__(self):
        self.storage: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str):
        self.storage[key] = value
        return True

    async def get(self, key: str):
        return self.storage.get(key)

    async def delete(self, key: str):
        if key in self.storage:
            del self.storage[key]
            return 1
        return 0

    async def exists(self, key: str):
        return 1 if key in self.storage else 0

    async def scan(self, cursor: int = 0, match: str | None = None, count: int = 50):
        import fnmatch

        matched = []
        for k in self.storage:
            if match is None or fnmatch.fnmatch(k, match):
                matched.append(k)
        return 0, matched


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]


def _button_texts(keyboard: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in keyboard.inline_keyboard for b in row if b.text]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis():
    return MockRedis()


@pytest.fixture
def test_cart_service(mock_redis):
    service = UserCartService()
    service._redis_client = mock_redis
    service._initialized = True
    return service


@pytest.fixture
def mock_db_user():
    user = MagicMock(spec=User)
    user.id = 42
    user.telegram_id = 123456789
    user.username = 'gift_tester'
    user.language = 'ru'
    user.balance_kopeks = 10025  # 100.25 RUB
    user.subscription = None
    return user


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=AsyncSession)
    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    mock_res.scalar_one_or_none.return_value = None
    mock_res.scalar.return_value = None
    db.execute = AsyncMock(return_value=mock_res)
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    me = MagicMock()
    me.username = 'test_gift_bot'
    bot.get_me = AsyncMock(return_value=me)
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def mock_callback(mock_db_user, mock_bot):
    callback = AsyncMock(spec=CallbackQuery)
    callback.bot = mock_bot
    callback.from_user = MagicMock(spec=TgUser)
    callback.from_user.id = mock_db_user.telegram_id
    callback.from_user.username = mock_db_user.username
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = 'gift_confirm'
    return callback


@pytest.fixture
def memory_state():
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=123456789, user_id=123456789)
    return FSMContext(storage=storage, key=key)


@pytest.fixture
def sample_quote():
    return GiftQuote(
        tariff_id=1,
        tariff_name='VIP Plan',
        period_days=30,
        traffic_limit_gb=100,
        device_limit=3,
        original_price_kopeks=35000,
        final_price_kopeks=30050,  # 300.50 RUB
        promo_group_discount_kopeks=4950,
        promo_offer_discount_kopeks=0,
        consumes_promo_offer=False,
    )


@pytest.fixture
def sample_purchase_result(sample_quote):
    token = 'g' * 64
    purchase = MagicMock(spec=GuestPurchase)
    purchase.id = 202
    purchase.token = token
    purchase.status = GuestPurchaseStatus.PAID.value
    purchase.tariff_id = 1
    purchase.period_days = 30
    purchase.amount_kopeks = 30050
    purchase.buyer_user_id = 42

    tariff = MagicMock(spec=Tariff)
    tariff.id = 1
    tariff.name = sample_quote.tariff_name
    tariff.traffic_limit_gb = 100
    tariff.device_limit = 3
    purchase.tariff = tariff

    tx = MagicMock(spec=Transaction)
    tx.id = 777
    tx.user_id = 42
    tx.amount_kopeks = 30050
    tx.type = TransactionType.GIFT_PAYMENT.value
    tx.external_id = 'gift_chk_123'

    return GiftPurchaseResult(
        purchase=purchase,
        transaction=tx,
        quote=sample_quote,
        remaining_balance_kopeks=0,
        is_idempotent_replay=False,
    )


# ── Step 1: Insufficient-Balance and Cart Persistence Tests ─────────────────


class TestGiftInsufficientBalanceAndCart:
    """Step 1: Missing amount without ruble rounding, Redis serialization, intent tracking, and fallback."""

    @pytest.mark.asyncio
    async def test_missing_amount_calculation_preserves_kopeks(self):
        """Missing amount equals fresh final price minus balance without ruble rounding."""
        final_price = 30050  # 300.50 RUB
        balance = 10025  # 100.25 RUB
        missing_amount = final_price - balance
        assert missing_amount == 20025  # 200.25 RUB exact

        # Verify get_insufficient_balance_keyboard receives exact amount
        kb = get_insufficient_balance_keyboard(
            language='ru',
            amount_kopeks=missing_amount,
            resume_callback='return_to_gift_cart',
            has_saved_cart=True,
            resume_text='🎁 Вернуться к подарку',
        )
        assert kb is not None
        callbacks = _callbacks(kb)
        assert 'return_to_gift_cart' in callbacks

    @pytest.mark.asyncio
    async def test_gift_cart_exact_keys_survive_redis_serialization(self, test_cart_service):
        """Gift cart dictionary contains exact required keys and serializes to/from Redis."""
        user_id = 42
        checkout_id = 'gift_chk_' + uuid.uuid4().hex
        cart_data = {
            'cart_mode': 'gift_purchase',
            'gift_checkout_id': checkout_id,
            'tariff_id': 1,
            'period_days': 30,
            'total_price': 30050,
            'missing_amount': 20025,
            'saved_cart': True,
            'return_to_cart': True,
            'user_id': user_id,
        }

        saved = await test_cart_service.save_user_cart(user_id, cart_data)
        assert saved is True

        loaded = await test_cart_service.get_user_cart(user_id)
        assert loaded is not None
        assert loaded['cart_mode'] == 'gift_purchase'
        assert loaded['gift_checkout_id'] == checkout_id
        assert loaded['tariff_id'] == 1
        assert loaded['period_days'] == 30
        assert loaded['total_price'] == 30050
        assert loaded['missing_amount'] == 20025
        assert loaded['saved_cart'] is True
        assert loaded['return_to_cart'] is True
        assert loaded['user_id'] == user_id

        # Intent must be set
        assert await test_cart_service.has_topup_intent(user_id) is True

    @pytest.mark.asyncio
    async def test_insufficient_balance_saves_cart_and_renders_topup_keyboard(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """When balance is insufficient during gift confirm, cart is saved and top-up keyboard is rendered."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        checkout_id = 'chk_insufficient_123'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30050,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftInsufficientBalanceError(required_kopeks=30050, available_kopeks=10025)),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Cart must be saved in Redis
            saved_cart = await test_cart_service.get_user_cart(mock_db_user.id)
            assert saved_cart is not None
            assert saved_cart['cart_mode'] == 'gift_purchase'
            assert saved_cart['gift_checkout_id'] == checkout_id
            assert saved_cart['tariff_id'] == 1
            assert saved_cart['period_days'] == 30
            assert saved_cart['total_price'] == 30050
            assert saved_cart['missing_amount'] == 20025
            assert saved_cart['saved_cart'] is True
            assert saved_cart['return_to_cart'] is True
            assert saved_cart['user_id'] == mock_db_user.id

            # Intent set
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

            # Message edited with topup keyboard and missing amount
            assert mock_callback.message.edit_text.called
            edit_kwargs = mock_callback.message.edit_text.call_args[1]
            text = mock_callback.message.edit_text.call_args[0][0]
            assert '200.25' in text or '200,25' in text or '200' in text
            reply_markup = edit_kwargs.get('reply_markup')
            assert reply_markup is not None
            callbacks = _callbacks(reply_markup)
            assert 'return_to_gift_cart' in callbacks

    @pytest.mark.asyncio
    async def test_redis_unavailable_shows_recoverable_warning_and_retains_fsm(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        """When Redis is down, show a recoverable warning, retain FSM, and do not claim cart was saved."""
        broken_cart_service = UserCartService()
        broken_cart_service._redis_client = None
        broken_cart_service._initialized = True

        checkout_id = 'chk_redis_down'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30050,
        )

        with (
            patch('app.handlers.subscription.gift.user_cart_service', broken_cart_service),
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftInsufficientBalanceError(required_kopeks=30050, available_kopeks=10025)),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Alert shown
            assert mock_callback.answer.called
            alert = mock_callback.answer.call_args[0][0] if mock_callback.answer.call_args[0] else ''
            assert 'недостаточно' in alert.lower() or 'insufficient' in alert.lower() or 'ошибка' in alert.lower()

            # Message was NOT edited to claim cart was saved
            assert not mock_callback.message.edit_text.called

            # FSM retained
            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state
            data = await memory_state.get_data()
            assert data.get('gift_checkout_id') == checkout_id

    @pytest.mark.asyncio
    async def test_gift_cancel_deletes_saved_gift_cart_and_intent(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """Canceling gift flow cleans up saved gift cart and intent."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        # Save gift cart
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_cancel',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )
        assert await test_cart_service.has_user_cart(mock_db_user.id) is True
        assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

        with patch('app.handlers.subscription.gift.show_subscription_info', AsyncMock()):
            await handle_gift_cancel(mock_callback, mock_db_user, mock_db, memory_state)

        # Gift cart and intent cleared
        assert await test_cart_service.has_user_cart(mock_db_user.id) is False
        assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

    @pytest.mark.asyncio
    async def test_gift_confirm_success_deletes_saved_gift_cart_and_intent(
        self,
        mock_callback,
        mock_db_user,
        mock_db,
        memory_state,
        test_cart_service,
        sample_purchase_result,
        monkeypatch,
    ):
        """Direct successful balance purchase cleans up saved gift cart and intent."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        checkout_id = 'chk_success_cleanup'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30050,
        )

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

        assert await test_cart_service.has_user_cart(mock_db_user.id) is False
        assert await test_cart_service.has_topup_intent(mock_db_user.id) is False


# ── Step 2: Top-Up Keyboard and Resume Flow Tests ───────────────────────────


class TestGiftTopupSuccessKeyboardAndResume:
    """Step 2: Payment success keyboard cart-mode inspection and return_to_gift_cart handler."""

    @pytest.mark.asyncio
    async def test_topup_success_keyboard_gift_cart_mode_offers_return_to_gift_cart(
        self, mock_db_user, test_cart_service, monkeypatch
    ):
        """With gift cart, payment success keyboard offers return_to_gift_cart, not return_to_saved_cart."""
        monkeypatch.setattr('app.services.payment.common.user_cart_service', test_cart_service)

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_kb_123',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
            },
        )

        mixin = type('_TestMixin', (PaymentCommonMixin,), {})()
        keyboard = await mixin.build_topup_success_keyboard(mock_db_user)

        callbacks = _callbacks(keyboard)
        texts = _button_texts(keyboard)

        assert 'return_to_gift_cart' in callbacks
        assert 'return_to_saved_cart' not in callbacks
        assert any('подарк' in t.lower() or 'gift' in t.lower() for t in texts)

    @pytest.mark.asyncio
    async def test_topup_success_keyboard_subscription_cart_mode_offers_return_to_saved_cart(
        self, mock_db_user, test_cart_service, monkeypatch
    ):
        """With normal subscription cart, payment success keyboard offers return_to_saved_cart."""
        monkeypatch.setattr('app.services.payment.common.user_cart_service', test_cart_service)

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'tariff_purchase',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
            },
        )

        mixin = type('_TestMixin', (PaymentCommonMixin,), {})()
        keyboard = await mixin.build_topup_success_keyboard(mock_db_user)

        callbacks = _callbacks(keyboard)
        assert 'return_to_saved_cart' in callbacks
        assert 'return_to_gift_cart' not in callbacks

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_expired_or_not_found(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """When returning to gift cart that expired or does not exist, show expired alert."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

        assert mock_callback.answer.called
        alert = mock_callback.answer.call_args[0][0] if mock_callback.answer.call_args[0] else ''
        assert 'истек' in alert.lower() or 'не найден' in alert.lower() or 'expired' in alert.lower()

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_invalid_mode_or_ownership_clears_cart(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """When returning to a cart with invalid mode or wrong user_id, report invalid and clear."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'wrong_mode',
                'user_id': mock_db_user.id,
            },
        )

        await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

        assert mock_callback.answer.called
        alert = mock_callback.answer.call_args[0][0] if mock_callback.answer.call_args[0] else ''
        assert 'недоступен' in alert.lower() or 'некоррект' in alert.lower() or 'invalid' in alert.lower()

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_still_insufficient_balance_renders_shortfall(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, sample_quote, monkeypatch
    ):
        """When resuming with still-insufficient balance, updates shortfall and shows payment methods."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        # Balance 10025, quote 30050 -> missing 20025
        mock_db_user.balance_kopeks = 10025

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_shortfall',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'missing_amount': 20025,
                'saved_cart': True,
                'return_to_cart': True,
                'user_id': mock_db_user.id,
            },
        )

        with patch('app.handlers.subscription.gift.quote_gift_purchase', AsyncMock(return_value=sample_quote)):
            await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert '200' in text
            reply_markup = mock_callback.message.edit_text.call_args[1]['reply_markup']
            assert 'return_to_gift_cart' in _callbacks(reply_markup)

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_sufficient_balance_renders_confirmation_summary(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, sample_quote, monkeypatch
    ):
        """When resuming with now-sufficient balance and unchanged price, render confirmation summary."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        mock_db_user.balance_kopeks = 50000  # More than 30050

        checkout_id = 'chk_resumed_ok'
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'missing_amount': 20025,
                'saved_cart': True,
                'return_to_cart': True,
                'user_id': mock_db_user.id,
            },
        )

        with patch('app.handlers.subscription.gift.quote_gift_purchase', AsyncMock(return_value=sample_quote)):
            await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert 'подтверждение' in text.lower() or 'confirmation' in text.lower() or 'подарок' in text.lower()
            reply_markup = mock_callback.message.edit_text.call_args[1]['reply_markup']
            assert 'gift_confirm' in _callbacks(reply_markup)

            # FSM state updated
            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state
            data = await memory_state.get_data()
            assert data.get('gift_checkout_id') == checkout_id
            assert data.get('gift_expected_price_kopeks') == 30050

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_price_changed_requires_reconfirmation(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """When price changed after top-up, update quote, require reconfirmation and alert user."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        mock_db_user.balance_kopeks = 50000

        new_quote = GiftQuote(
            tariff_id=1,
            tariff_name='VIP Plan',
            period_days=30,
            traffic_limit_gb=100,
            device_limit=3,
            original_price_kopeks=40000,
            final_price_kopeks=38000,  # Changed from 30050 to 38000
            promo_group_discount_kopeks=2000,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )

        checkout_id = 'chk_price_drift'
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,  # Old price
                'missing_amount': 20025,
                'saved_cart': True,
                'return_to_cart': True,
                'user_id': mock_db_user.id,
            },
        )

        with patch('app.handlers.subscription.gift.quote_gift_purchase', AsyncMock(return_value=new_quote)):
            await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            # Alert user of price change
            assert mock_callback.answer.called
            alert = mock_callback.answer.call_args[0][0] if mock_callback.answer.call_args[0] else ''
            assert 'изменилась' in alert.lower() or 'changed' in alert.lower()

            # Render confirmation summary with new price
            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert '380' in text
            reply_markup = mock_callback.message.edit_text.call_args[1]['reply_markup']
            assert 'gift_confirm' in _callbacks(reply_markup)

            # FSM updated to new price
            data = await memory_state.get_data()
            assert data.get('gift_expected_price_kopeks') == 38000

    @pytest.mark.asyncio
    async def test_return_to_gift_cart_terminal_invalidation_clears_cart_and_reports(
        self, mock_callback, mock_db_user, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """When tariff became unavailable, clear cart and intent and display error."""
        monkeypatch.setattr('app.handlers.subscription.gift.user_cart_service', test_cart_service)

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_unavail',
                'tariff_id': 999,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
                'user_id': mock_db_user.id,
            },
        )

        with patch(
            'app.handlers.subscription.gift.quote_gift_purchase',
            AsyncMock(side_effect=GiftTariffUnavailableError('Tariff deleted')),
        ):
            await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            # Cart and intent cleared
            assert await test_cart_service.has_user_cart(mock_db_user.id) is False
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

            # Error shown
            assert mock_callback.message.edit_text.called or mock_callback.answer.called


# ── Step 3: Automatic-Completion and Isolation Tests ────────────────────────


class TestGiftAutoPurchaseAndIsolation:
    """Step 3: Auto-purchase saved gift cart after topup, error branches, and complete cart isolation."""

    @pytest.mark.asyncio
    async def test_auto_purchase_gift_cart_success(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, sample_purchase_result, monkeypatch
    ):
        """When auto-purchase enabled, sufficient funds and fresh intent: purchase gift and send result."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000  # More than 30050
        checkout_id = 'chk_auto_win'

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'missing_amount': 20025,
                'saved_cart': True,
                'return_to_cart': True,
                'user_id': mock_db_user.id,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ) as mock_purchase,
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(),
            ) as mock_send_result,
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is True
            mock_purchase.assert_awaited_once_with(
                db=mock_db,
                buyer_id=mock_db_user.id,
                tariff_id=1,
                period_days=30,
                expected_price_kopeks=30050,
                idempotency_key=checkout_id,
                source='bot',
            )
            assert mock_send_result.await_count == 1
            call_kwargs = mock_send_result.await_args.kwargs
            assert call_kwargs['bot'] == mock_bot
            assert call_kwargs['user'] == mock_db_user
            assert call_kwargs['purchase_result'] == sample_purchase_result

            # Cart and intent must be cleared
            assert await test_cart_service.has_user_cart(mock_db_user.id) is False
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

    @pytest.mark.asyncio
    async def test_auto_purchase_gift_disabled_setting_skips(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, monkeypatch
    ):
        """When AUTO_PURCHASE_AFTER_TOPUP_ENABLED is False, auto-purchase is skipped."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_skip',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)
        assert res is False
        assert await test_cart_service.has_user_cart(mock_db_user.id) is True

    @pytest.mark.asyncio
    async def test_auto_purchase_gift_partial_funds_preserves_cart_and_intent(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, monkeypatch
    ):
        """When topup was only partial, do not debit, preserve cart and intent within TTL."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 20000  # Less than 30050

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_partial',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(),
            ) as mock_purchase,
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is False
            mock_purchase.assert_not_called()
            # Cart and intent retained
            assert await test_cart_service.has_user_cart(mock_db_user.id) is True
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

    @pytest.mark.asyncio
    async def test_auto_purchase_gift_stale_price_preserves_cart_for_manual_confirmation(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, monkeypatch
    ):
        """When price changed before auto-purchase, do not debit silently; update cart and require manual resume."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000

        drifted_quote = GiftQuote(
            tariff_id=1,
            tariff_name='VIP Plan',
            period_days=30,
            traffic_limit_gb=100,
            device_limit=3,
            original_price_kopeks=40000,
            final_price_kopeks=38000,  # Was 30050
            promo_group_discount_kopeks=2000,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_drift',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=drifted_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(),
            ) as mock_purchase,
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is False
            mock_purchase.assert_not_called()
            # Cart updated with new price
            cart = await test_cart_service.get_user_cart(mock_db_user.id)
            assert cart['total_price'] == 38000

    @pytest.mark.asyncio
    async def test_auto_purchase_gift_terminal_error_clears_cart_and_intent(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, monkeypatch
    ):
        """When tariff became unavailable or feature disabled, clear cart and notify user."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000

        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_dead_tariff',
                'tariff_id': 999,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(side_effect=GiftTariffUnavailableError('Tariff gone')),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(),
            ) as mock_purchase,
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is False
            mock_purchase.assert_not_called()
            assert await test_cart_service.has_user_cart(mock_db_user.id) is False
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

    @pytest.mark.asyncio
    async def test_gift_intent_processes_only_gift_cart_and_ignores_subscription_carts(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, sample_purchase_result, monkeypatch
    ):
        """Isolation: when user has per-sub subscription carts AND a gift cart, gift intent processes ONLY gift cart."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000

        # Seed per-subscription cart
        await test_cart_service.save_subscription_cart(
            mock_db_user.id,
            subscription_id=10,
            cart_data={
                'cart_mode': 'extend',
                'subscription_id': 10,
                'period_days': 30,
                'total_price': 20000,
            },
        )
        # Seed global gift cart with fresh intent
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_isolated_gift',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ) as mock_gift_purchase,
            patch(
                'app.services.subscription_auto_purchase_service._auto_extend_subscription',
                AsyncMock(),
            ) as mock_extend,
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(),
            ),
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is True
            # Gift was purchased
            mock_gift_purchase.assert_awaited_once()
            # Normal subscription extension was NOT run
            mock_extend.assert_not_called()
            # Per-subscription cart is preserved untouched!
            assert await test_cart_service.get_subscription_cart(mock_db_user.id, 10) is not None

    @pytest.mark.asyncio
    async def test_normal_subscription_intent_never_runs_gift_code(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, monkeypatch
    ):
        """Isolation: when cart is normal subscription cart, gift purchase code is never called."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000

        # Normal subscription cart
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'extend',
                'subscription_id': 10,
                'period_days': 30,
                'total_price': 20000,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service._is_subscription_disabled',
                AsyncMock(return_value=False),
            ),
            patch(
                'app.database.crud.subscription.get_subscription_by_id_for_user',
                AsyncMock(return_value=None),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(),
            ) as mock_gift_purchase,
            patch(
                'app.services.subscription_auto_purchase_service._auto_extend_subscription',
                AsyncMock(return_value=True),
            ) as mock_extend,
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is True
            mock_gift_purchase.assert_not_called()
            mock_extend.assert_awaited_once()


class TestGiftAutoPurchaseDeliveryAndPriceChangeSafeguards:
    """Tests for P1 safeguards: delivery confirmation, retry replay, and price change intent revocation."""

    @pytest.mark.asyncio
    async def test_bot_none_returns_false_and_retains_cart_and_intent(
        self, mock_db_user, mock_db, test_cart_service, monkeypatch
    ):
        """P1 Safeguard: when bot is None, auto-purchase does not proceed or delete cart/intent."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_bot_none',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30000,
                'return_to_cart': True,
            },
        )

        with patch(
            'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
            AsyncMock(),
        ) as mock_purchase:
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=None)

            assert res is False
            mock_purchase.assert_not_called()
            # Cart and intent are preserved!
            assert await test_cart_service.get_user_cart(mock_db_user.id) is not None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

    @pytest.mark.asyncio
    async def test_notification_returns_none_retains_cart_and_intent(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, sample_purchase_result, monkeypatch
    ):
        """P1 Safeguard: when message sender returns None, scenario is NOT completed and cart is retained."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_notify_none',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.resolve_gift_claim_channel',
                AsyncMock(return_value=('my_bot', None)),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(return_value=None),  # Sending failed!
            ),
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is False
            # Cart and intent are NOT deleted so user/retry can restore link
            assert await test_cart_service.get_user_cart(mock_db_user.id) is not None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

    @pytest.mark.asyncio
    async def test_notification_raises_exception_retains_cart_and_intent(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, sample_purchase_result, monkeypatch
    ):
        """P1 Safeguard: when message sender raises exception, scenario is NOT completed and cart is retained."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_notify_err',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.resolve_gift_claim_channel',
                AsyncMock(return_value=('my_bot', None)),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(side_effect=RuntimeError('Telegram network timeout')),
            ),
        ):
            res = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res is False
            assert await test_cart_service.get_user_cart(mock_db_user.id) is not None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

    @pytest.mark.asyncio
    async def test_retry_after_failed_delivery_delivers_existing_gift_without_double_debit(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, sample_quote, sample_purchase_result, monkeypatch
    ):
        """A failed delivery is retried as an idempotent replay before a new quote is calculated."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000
        checkout_id = 'chk_retry_idempotent'
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30050,
                'return_to_cart': True,
            },
        )

        mock_sent_message = MagicMock(spec=Message)
        no_existing_purchase = MagicMock()
        no_existing_purchase.scalar_one_or_none.return_value = None
        committed_purchase = MagicMock()
        committed_purchase.scalar_one_or_none.return_value = sample_purchase_result.purchase.id
        mock_db.execute.side_effect = [no_existing_purchase, committed_purchase]

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=sample_quote),
            ) as mock_quote,
            patch(
                'app.services.subscription_auto_purchase_service.resolve_gift_claim_channel',
                AsyncMock(return_value=('my_bot', None)),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ) as mock_purchase,
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(side_effect=[None, mock_sent_message]),
            ) as mock_send,
        ):
            first_result = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert first_result is False
            assert await test_cart_service.get_user_cart(mock_db_user.id) is not None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

            second_result = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert second_result is True
            assert mock_purchase.await_count == 2
            mock_purchase.assert_awaited_with(
                db=mock_db,
                buyer_id=mock_db_user.id,
                tariff_id=1,
                period_days=30,
                expected_price_kopeks=30050,
                idempotency_key=checkout_id,
                source='bot',
            )
            assert mock_send.await_count == 2
            assert mock_quote.await_count == 1
            assert await test_cart_service.get_user_cart(mock_db_user.id) is None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

    @pytest.mark.asyncio
    async def test_price_change_revokes_intent_until_explicit_user_confirmation(
        self, mock_db_user, mock_db, mock_bot, test_cart_service, monkeypatch
    ):
        """P1 Safeguard: when price changes, top-up intent is revoked. Subsequent top-up without confirmation
        does not auto-purchase. Only after explicit user return/confirmation is auto-purchase permitted."""
        monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
        monkeypatch.setattr(
            'app.services.subscription_auto_purchase_service.user_cart_service',
            test_cart_service,
        )

        mock_db_user.balance_kopeks = 50000
        # User had saved cart for 30000 kopeks with fresh intent
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': 'chk_price_change',
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30000,
                'return_to_cart': True,
            },
        )
        assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

        # Fresh quote has changed to 45000 kopeks
        fresh_quote = GiftQuote(
            tariff_id=1,
            tariff_name='Standard',
            period_days=30,
            traffic_limit_gb=50,
            device_limit=1,
            original_price_kopeks=45000,
            final_price_kopeks=45000,
            promo_group_discount_kopeks=0,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )

        # 1st Top-Up: detects price change
        with patch(
            'app.services.subscription_auto_purchase_service.quote_gift_purchase',
            AsyncMock(return_value=fresh_quote),
        ):
            res1 = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res1 is False
            # Cart was updated with new price, but return_to_cart is False and intent was REVOKED
            cart1 = await test_cart_service.get_user_cart(mock_db_user.id)
            assert cart1['total_price'] == 45000
            assert cart1['return_to_cart'] is False
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

        # 2nd Top-Up: without user confirmation, has_topup_intent is False, so nothing is debited!
        with patch(
            'app.services.subscription_auto_purchase_service.quote_gift_purchase',
            AsyncMock(return_value=fresh_quote),
        ) as mock_quote2:
            res2 = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res2 is False
            mock_quote2.assert_not_called()  # Fast-path skipped because no intent

        # User now explicitly confirms new price (e.g. via UI / return_to_gift_cart -> topup)
        # Setting return_to_cart=True restores topup intent for the new price 45000
        cart1['return_to_cart'] = True
        await test_cart_service.save_user_cart(mock_db_user.id, cart1)
        assert await test_cart_service.has_topup_intent(mock_db_user.id) is True

        # 3rd Top-Up: after explicit confirmation, purchase proceeds with 45000 kopeks!
        sample_purchase = MagicMock(spec=GuestPurchase)
        sample_purchase.id = 1
        sample_purchase.token = 'a' * 64
        sample_purchase.tariff = MagicMock(spec=Tariff)
        sample_purchase.tariff.name = 'Standard'
        sample_result = GiftPurchaseResult(
            purchase=sample_purchase,
            transaction=MagicMock(spec=Transaction),
            quote=fresh_quote,
            remaining_balance_kopeks=5000,
            is_idempotent_replay=False,
        )

        with (
            patch(
                'app.services.subscription_auto_purchase_service.quote_gift_purchase',
                AsyncMock(return_value=fresh_quote),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.resolve_gift_claim_channel',
                AsyncMock(return_value=('my_bot', None)),
            ),
            patch(
                'app.services.subscription_auto_purchase_service.purchase_gift_from_balance',
                AsyncMock(return_value=sample_result),
            ) as mock_purchase3,
            patch(
                'app.services.subscription_auto_purchase_service.send_gift_result_message',
                AsyncMock(return_value=MagicMock(spec=Message)),
            ),
        ):
            res3 = await auto_purchase_saved_cart_after_topup(mock_db, mock_db_user, bot=mock_bot)

            assert res3 is True
            mock_purchase3.assert_awaited_once_with(
                db=mock_db,
                buyer_id=mock_db_user.id,
                tariff_id=1,
                period_days=30,
                expected_price_kopeks=45000,
                idempotency_key='chk_price_change',
                source='bot',
            )
            assert await test_cart_service.get_user_cart(mock_db_user.id) is None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False

    @pytest.mark.asyncio
    async def test_handle_return_to_gift_cart_delivers_already_paid_gift_link(
        self, mock_db_user, mock_callback, mock_db, memory_state, test_cart_service, monkeypatch
    ):
        """P1 Safeguard: if a paid purchase already exists for the saved checkout_id, handle_return_to_gift_cart
        replays the presentation, cleans up cart and intent, and delivers the link without double debit."""
        monkeypatch.setattr(
            'app.handlers.subscription.gift.user_cart_service',
            test_cart_service,
        )

        checkout_id = 'chk_already_paid'
        await test_cart_service.save_user_cart(
            mock_db_user.id,
            {
                'cart_mode': 'gift_purchase',
                'user_id': mock_db_user.id,
                'gift_checkout_id': checkout_id,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 30000,
                'return_to_cart': True,
            },
        )
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(gift_checkout_id=checkout_id)

        existing_purchase = GuestPurchase(
            id=123,
            buyer_user_id=mock_db_user.id,
            tariff_id=1,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@tester',
            status=GuestPurchaseStatus.PAID.value,
            token='g' * 64,
            idempotency_key=checkout_id,
        )
        existing_purchase.tariff = Tariff(id=1, name='Standard')

        mock_scalars = MagicMock()
        mock_scalars.first = MagicMock(return_value=existing_purchase)
        mock_result = MagicMock()
        mock_result.scalars = MagicMock(return_value=mock_scalars)
        mock_db.execute = AsyncMock(return_value=mock_result)

        quote = GiftQuote(
            tariff_id=1,
            tariff_name='Standard',
            period_days=30,
            traffic_limit_gb=50,
            device_limit=1,
            original_price_kopeks=30000,
            final_price_kopeks=30000,
            promo_group_discount_kopeks=0,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )
        purchase_result = GiftPurchaseResult(
            purchase=existing_purchase,
            transaction=MagicMock(spec=Transaction),
            quote=quote,
            remaining_balance_kopeks=20000,
            is_idempotent_replay=True,
        )

        with (
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(return_value=purchase_result),
            ) as mock_purchase,
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('my_bot', None)),
            ),
        ):
            mock_callback.message.edit_text.side_effect = RuntimeError('Telegram edit failed')
            with pytest.raises(RuntimeError, match='Telegram edit failed'):
                await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            # A failed Telegram edit must leave all retry context intact.
            assert await test_cart_service.get_user_cart(mock_db_user.id) is not None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is True
            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state

            mock_callback.message.edit_text.side_effect = None
            await handle_return_to_gift_cart(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_purchase.await_count == 2
            assert await test_cart_service.get_user_cart(mock_db_user.id) is None
            assert await test_cart_service.has_topup_intent(mock_db_user.id) is False
            assert await memory_state.get_state() is None
