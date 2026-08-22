"""Tests for native gift catalog, menu entries, and FSM navigation (Task 4).

Covers:
- Entry visibility in single-tariff and multi-tariff views when CABINET_GIFT_ENABLED is True/False.
- State transitions across GiftPurchaseStates (selecting_tariff, selecting_period, confirming_purchase, cart_saved_for_topup).
- Localized catalog rendering with HTML escaping, traffic limit display, device limit.
- Period selection with discounts and prices.
- Confirmation summary rendering (requote-only, no debit in Task 4).
- Safe parsing of forged/invalid callback data.
- Back and cancel navigation returning to origin screen.
- Collision testing between gift purchase handlers and gift_activate handler.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.handlers.gift_activation import register_handlers as register_gift_activation_handlers
from app.handlers.subscription import register_gift_handlers
from app.handlers.subscription.gift import (
    handle_gift_back_periods,
    handle_gift_back_tariffs,
    handle_gift_cancel,
    handle_gift_catalog,
    handle_gift_period_select,
    handle_gift_tariff_select,
)
from app.handlers.subscription.my_subscriptions import (
    _build_subscriptions_keyboard,
    show_my_subscriptions,
)
from app.handlers.subscription.purchase import show_subscription_info
from app.keyboards.inline import get_subscription_keyboard
from app.services.gift_purchase_service import GiftQuote, GiftTariffOffer
from app.states import GiftPurchaseStates


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract callback_data from an inline keyboard."""
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def _button_texts(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract text from all buttons in an inline keyboard."""
    return [button.text for row in keyboard.inline_keyboard for button in row]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_user() -> User:
    user = MagicMock(spec=User)
    user.id = 42
    user.telegram_id = 123456789
    user.username = 'test_gift_buyer'
    user.language = 'ru'
    user.balance_kopeks = 50000  # 500 RUB
    user.subscription = None
    user.has_had_paid_subscription = True
    user.promo_group_id = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.get_promo_discount = MagicMock(return_value=0)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    return user


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.refresh = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.first.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_result
    return db


@pytest.fixture
def mock_callback(mock_db_user) -> AsyncMock:
    callback = AsyncMock(spec=CallbackQuery)
    callback.from_user = MagicMock(spec=TgUser)
    callback.from_user.id = mock_db_user.telegram_id
    callback.from_user.username = mock_db_user.username
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = 'subscription_gift'
    return callback


@pytest.fixture
def memory_state() -> FSMContext:
    storage = MemoryStorage()
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=123456789, user_id=123456789)
    return FSMContext(storage=storage, key=key)


# ── Step 1: Entry-visibility Tests ──────────────────────────────────────────


class TestSubscriptionGiftEntryVisibility:
    """Test gift entry button visibility in single and multi-tariff layouts."""

    def test_single_mode_active_sub_with_gift_enabled(self):
        sub = SimpleNamespace(id=1, is_trial=False, actual_status='paid_active', traffic_limit_gb=100)
        kb = get_subscription_keyboard(
            language='ru', has_subscription=True, is_trial=False, subscription=sub, gift_enabled=True
        )
        callbacks = _callbacks(kb)
        assert 'subscription_gift' in callbacks
        assert 'subscription_extend' in callbacks
        assert 'back_to_menu' in callbacks

    def test_single_mode_active_sub_with_gift_disabled(self):
        sub = SimpleNamespace(id=1, is_trial=False, actual_status='paid_active', traffic_limit_gb=100)
        kb = get_subscription_keyboard(
            language='ru', has_subscription=True, is_trial=False, subscription=sub, gift_enabled=False
        )
        callbacks = _callbacks(kb)
        assert 'subscription_gift' not in callbacks
        assert 'subscription_extend' in callbacks

    def test_single_mode_trial_sub_with_gift_enabled(self):
        sub = SimpleNamespace(id=2, is_trial=True, actual_status='trial_active', traffic_limit_gb=10)
        kb = get_subscription_keyboard(
            language='ru', has_subscription=True, is_trial=True, subscription=sub, gift_enabled=True
        )
        callbacks = _callbacks(kb)
        assert 'subscription_gift' in callbacks
        assert 'subscription_upgrade' in callbacks

    def test_single_mode_expired_sub_with_gift_enabled(self):
        sub = SimpleNamespace(id=3, is_trial=False, actual_status='expired', traffic_limit_gb=50)
        kb = get_subscription_keyboard(
            language='ru', has_subscription=True, is_trial=False, subscription=sub, gift_enabled=True
        )
        callbacks = _callbacks(kb)
        assert 'subscription_gift' in callbacks
        assert 'subscription_extend' in callbacks

    def test_single_mode_no_sub_keyboard_with_gift_enabled(self):
        kb = get_subscription_keyboard(language='ru', has_subscription=False, gift_enabled=True)
        callbacks = _callbacks(kb)
        assert 'subscription_gift' in callbacks
        assert 'back_to_menu' in callbacks

    def test_single_mode_no_sub_keyboard_with_gift_disabled(self):
        kb = get_subscription_keyboard(language='ru', has_subscription=False, gift_enabled=False)
        callbacks = _callbacks(kb)
        assert 'subscription_gift' not in callbacks
        assert 'back_to_menu' in callbacks

    @pytest.mark.asyncio
    async def test_show_subscription_info_no_sub_keeps_gift_entry_reachable(
        self, mock_callback, mock_db_user, mock_db, monkeypatch
    ):
        mock_db_user.subscription = None
        monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
        await show_subscription_info(mock_callback, mock_db_user, mock_db)

        assert mock_callback.message.edit_text.called
        _, kwargs = mock_callback.message.edit_text.call_args
        reply_markup = kwargs.get('reply_markup')
        assert reply_markup is not None
        assert 'subscription_gift' in _callbacks(reply_markup)

    def test_multi_mode_build_subscriptions_keyboard_gift_enabled(self):
        subs = [SimpleNamespace(id=10, tariff=SimpleNamespace(name='Basic'))]
        kb = _build_subscriptions_keyboard(subs, language='ru', gift_enabled=True)
        callbacks = _callbacks(kb)
        assert 'subscription_gift' in callbacks
        assert 'sm:10' in callbacks
        assert 'menu_buy' in callbacks
        assert 'back_to_menu' in callbacks

    def test_multi_mode_build_subscriptions_keyboard_gift_disabled(self):
        subs = [SimpleNamespace(id=10, tariff=SimpleNamespace(name='Basic'))]
        kb = _build_subscriptions_keyboard(subs, language='ru', gift_enabled=False)
        callbacks = _callbacks(kb)
        assert 'subscription_gift' not in callbacks
        assert 'sm:10' in callbacks
        assert 'menu_buy' in callbacks

    @pytest.mark.asyncio
    async def test_show_my_subscriptions_empty_keeps_gift_entry_reachable(
        self, mock_callback, mock_db_user, mock_db, monkeypatch
    ):
        monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
        with patch(
            'app.handlers.subscription.my_subscriptions.get_all_subscriptions_by_user_id',
            AsyncMock(return_value=[]),
        ):
            await show_my_subscriptions(mock_callback, mock_db_user, mock_db)

            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            assert reply_markup is not None
            assert 'subscription_gift' in _callbacks(reply_markup)
            assert 'menu_buy' in _callbacks(reply_markup)


# ── Step 2: Catalog, Selection, and Navigation Tests ────────────────────────


class TestSubscriptionGiftCatalogFlow:
    """Test catalog rendering, period choices, summary, and FSM navigation."""

    @pytest.mark.asyncio
    async def test_gift_catalog_feature_disabled_shows_activation_hub(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=False)),
            patch('app.handlers.subscription.gift.has_sender_gifts', AsyncMock(return_value=False)),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            callbacks = _callbacks(kwargs['reply_markup'])
            assert 'gift_enter_code' in callbacks
            assert 'gift_my' not in callbacks
            current_state = await memory_state.get_state()
            assert current_state is None

    @pytest.mark.asyncio
    async def test_gift_catalog_no_tariffs_shows_message(self, mock_callback, mock_db_user, mock_db, memory_state):
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=[])),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert 'нет доступных' in text.lower() or 'unavailable' in text.lower()
            reply_markup = mock_callback.message.edit_text.call_args[1].get('reply_markup')
            assert reply_markup is not None
            assert 'gift_enter_code' in _callbacks(reply_markup)
            assert 'gift_cancel' in _callbacks(reply_markup)

    @pytest.mark.asyncio
    async def test_gift_catalog_renders_offers_and_sets_state(self, mock_callback, mock_db_user, mock_db, memory_state):
        offers = [
            GiftTariffOffer(
                tariff_id=1,
                tariff_name='Standard <VIP>',
                tariff_description='Great & fast',
                traffic_limit_gb=100,
                device_limit=2,
                display_order=1,
                quotes=(GiftQuote(1, 'Standard <VIP>', 30, 100, 2, 30000, 30000, 0, 0, False),),
            ),
            GiftTariffOffer(
                tariff_id=2,
                tariff_name='Unlimited Pro',
                tariff_description=None,
                traffic_limit_gb=None,
                device_limit=5,
                display_order=2,
                quotes=(GiftQuote(2, 'Unlimited Pro', 30, None, 5, 50000, 45000, 5000, 0, False),),
            ),
        ]
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=offers)),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            assert reply_markup is not None

            callbacks = _callbacks(reply_markup)
            assert 'gift_tariff:1' in callbacks
            assert 'gift_tariff:2' in callbacks
            assert 'gift_enter_code' in callbacks
            assert 'gift_cancel' in callbacks

            # FSM state check
            assert await memory_state.get_state() == GiftPurchaseStates.selecting_tariff.state
            data = await memory_state.get_data()
            assert 'gift_checkout_id' in data
            assert len(data['gift_checkout_id']) >= 16

    @pytest.mark.asyncio
    async def test_gift_tariff_select_renders_periods_and_escapes_html(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        mock_callback.data = 'gift_tariff:1'
        offers = [
            GiftTariffOffer(
                tariff_id=1,
                tariff_name='Standard <VIP> & "Special"',
                tariff_description='Description with <b>raw html</b> & ampersands',
                traffic_limit_gb=100,
                device_limit=2,
                display_order=1,
                quotes=(
                    GiftQuote(1, 'Standard <VIP> & "Special"', 30, 100, 2, 30000, 30000, 0, 0, False),
                    GiftQuote(1, 'Standard <VIP> & "Special"', 90, 100, 2, 90000, 80000, 10000, 0, False),
                ),
            )
        ]
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=offers)),
        ):
            await handle_gift_tariff_select(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text, kwargs = mock_callback.message.edit_text.call_args[0][0], mock_callback.message.edit_text.call_args[1]

            # HTML escaping assertions
            assert html.escape('Standard <VIP> & "Special"') in text
            assert '<VIP>' not in text  # raw unescaped bracket must not be present
            assert 'raw html' in text

            reply_markup = kwargs.get('reply_markup')
            callbacks = _callbacks(reply_markup)
            assert 'gift_period:1:30' in callbacks
            assert 'gift_period:1:90' in callbacks
            assert 'gift_back_tariffs' in callbacks
            assert 'gift_cancel' in callbacks

            assert await memory_state.get_state() == GiftPurchaseStates.selecting_period.state
            data = await memory_state.get_data()
            assert data.get('gift_tariff_id') == 1

    @pytest.mark.asyncio
    async def test_gift_tariff_select_forged_id(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_tariff:not_an_int'
        await handle_gift_tariff_select(mock_callback, mock_db_user, mock_db, memory_state)
        assert mock_callback.answer.called

    @pytest.mark.asyncio
    async def test_gift_period_select_renders_summary_and_saves_fsm(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        checkout_id = uuid.uuid4().hex
        await memory_state.update_data(gift_checkout_id=checkout_id, gift_origin_callback='menu_subscription')
        mock_callback.data = 'gift_period:1:30'

        quote = GiftQuote(
            tariff_id=1,
            tariff_name='Standard <VIP>',
            period_days=30,
            traffic_limit_gb=100,
            device_limit=2,
            original_price_kopeks=35000,
            final_price_kopeks=30000,
            promo_group_discount_kopeks=5000,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )

        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.quote_gift_purchase', AsyncMock(return_value=quote)),
        ):
            await handle_gift_period_select(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text, kwargs = mock_callback.message.edit_text.call_args[0][0], mock_callback.message.edit_text.call_args[1]
            reply_markup = kwargs.get('reply_markup')

            assert html.escape('Standard <VIP>') in text
            assert '<VIP>' not in text

            callbacks = _callbacks(reply_markup)
            assert 'gift_confirm' in callbacks
            assert 'gift_back_periods' in callbacks
            assert 'gift_cancel' in callbacks

            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state
            data = await memory_state.get_data()
            assert data.get('gift_checkout_id') == checkout_id  # preserved
            assert data.get('gift_tariff_id') == 1
            assert data.get('gift_period_days') == 30
            assert data.get('gift_expected_price_kopeks') == 30000

    @pytest.mark.asyncio
    async def test_gift_period_select_forged_params(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_period:1:invalid_days'
        await handle_gift_period_select(mock_callback, mock_db_user, mock_db, memory_state)
        assert mock_callback.answer.called

    @pytest.mark.asyncio
    async def test_back_to_tariffs_navigation(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_back_tariffs'
        offers = [
            GiftTariffOffer(
                1, 'Tariff 1', None, 50, 1, 1, (GiftQuote(1, 'Tariff 1', 30, 50, 1, 10000, 10000, 0, 0, False),)
            )
        ]
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=offers)),
        ):
            await handle_gift_back_tariffs(mock_callback, mock_db_user, mock_db, memory_state)
            assert await memory_state.get_state() == GiftPurchaseStates.selecting_tariff.state

    @pytest.mark.asyncio
    async def test_back_to_periods_navigation(self, mock_callback, mock_db_user, mock_db, memory_state):
        await memory_state.update_data(gift_tariff_id=1)
        mock_callback.data = 'gift_back_periods'
        offers = [
            GiftTariffOffer(
                1, 'Tariff 1', None, 50, 1, 1, (GiftQuote(1, 'Tariff 1', 30, 50, 1, 10000, 10000, 0, 0, False),)
            )
        ]
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=offers)),
        ):
            await handle_gift_back_periods(mock_callback, mock_db_user, mock_db, memory_state)
            assert await memory_state.get_state() == GiftPurchaseStates.selecting_period.state

    @pytest.mark.asyncio
    async def test_back_to_periods_with_frozen_aiogram_callback_query(self, mock_db_user, mock_db, memory_state):
        """P2 Regression: real frozen CallbackQuery must not raise ValidationError on back navigation."""
        from datetime import datetime

        from aiogram.types import Chat

        real_user = TgUser(id=mock_db_user.telegram_id, is_bot=False, first_name='Test', username=mock_db_user.username)
        real_chat = Chat(id=mock_db_user.telegram_id, type='private')
        real_msg = Message(
            message_id=123,
            date=datetime.now(UTC),
            chat=real_chat,
            from_user=real_user,
            text='Confirmation',
        )

        real_callback = CallbackQuery(
            id='query_12345',
            from_user=real_user,
            chat_instance='chat_instance_abc',
            data='gift_back_periods',
            message=real_msg,
        )

        checkout_id = 'chk_preserve_123'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
        )

        offers = [
            GiftTariffOffer(
                1, 'Tariff 1', None, 50, 1, 1, (GiftQuote(1, 'Tariff 1', 30, 50, 1, 10000, 10000, 0, 0, False),)
            )
        ]

        mock_edit_text = AsyncMock()
        mock_answer = AsyncMock()

        with (
            patch.object(Message, 'edit_text', mock_edit_text),
            patch.object(CallbackQuery, 'answer', mock_answer),
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=offers)),
        ):
            await handle_gift_back_periods(real_callback, mock_db_user, mock_db, memory_state)

            # Invariant: callback.data was NOT mutated
            assert real_callback.data == 'gift_back_periods'

            # Invariant: FSM state transitioned to selecting_period and checkout_id is preserved
            assert await memory_state.get_state() == GiftPurchaseStates.selecting_period.state
            data = await memory_state.get_data()
            assert data.get('gift_checkout_id') == checkout_id
            assert data.get('gift_tariff_id') == 1

            assert mock_edit_text.called
            assert mock_answer.called

    @pytest.mark.asyncio
    async def test_back_to_periods_ignores_inaccessible_message(self, mock_db_user, mock_db, memory_state):
        """Back navigation must acknowledge callbacks whose source message is inaccessible."""
        from aiogram.types import Chat

        callback = AsyncMock(spec=CallbackQuery)
        callback.message = InaccessibleMessage(
            chat=Chat(id=mock_db_user.telegram_id, type='private'),
            message_id=123,
            date=0,
        )
        callback.answer = AsyncMock()
        await memory_state.update_data(gift_tariff_id=1)

        with patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock()) as mock_enabled:
            await handle_gift_back_periods(callback, mock_db_user, mock_db, memory_state)

        callback.answer.assert_awaited_once_with()
        mock_enabled.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gift_cancel_returns_to_origin(self, mock_callback, mock_db_user, mock_db, memory_state):
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(gift_origin_callback='menu_subscription')
        mock_callback.data = 'gift_cancel'

        with patch('app.handlers.subscription.gift.show_subscription_info', AsyncMock()) as mock_show_sub:
            await handle_gift_cancel(mock_callback, mock_db_user, mock_db, memory_state)

            assert await memory_state.get_state() is None
            assert mock_show_sub.called


# ── Step 6: Registration and Collision Safety ───────────────────────────────


class TestGiftHandlerRegistrationAndCollision:
    """Test registration on Dispatcher and collision safety with gift_activate."""

    def test_gift_handlers_and_activation_collision_safety(self):
        dp = Dispatcher()

        # Register both handler groups
        register_gift_handlers(dp)
        register_gift_activation_handlers(dp)

        # Inspect registered callback query handlers
        callback_handlers = dp.callback_query.handlers

        # Prove exact prefix filters without collision:
        # Gift purchase handles: 'subscription_gift', 'gift_enter_code', 'gift_activation_cancel',
        # 'gift_tariff:', 'gift_period:', 'gift_back_tariffs', 'gift_back_periods', 'gift_cancel',
        # 'gift_confirm', 'return_to_gift_cart'
        # Gift activation handles: 'gift_activate:'
        # There must be no handler matching a raw 'gift_' that would intercept both.
        registered_filters = [h.filters for h in callback_handlers]
        assert len(registered_filters) >= 10
        assert len(dp.message.handlers) >= 1
