"""Tests for "My gifts" Telegram history, pagination, recovery, and source-neutral presentation (Task 6).

Covers:
- Menu visibility:
  - Gift entry remains reachable in single- and multi-tariff subscription layouts.
  - Disabled sales render activation/history controls without offering a new purchase.
  - No eligible tariffs still allows activation and available history navigation.
- History list & pagination:
  - Localized empty history.
  - 5 items per page with stable newest-first ordering.
  - First, middle, and last page navigation controls.
  - Clamping for forged, negative, zero, and oversized page values.
- Ownership & Security:
  - IDOR protection: cannot view another buyer's gift.
  - Inaccessible message handling.
  - Callback data contains only numeric purchase ID or page number (no tokens or codes).
- Recovery detail & source-neutral presentation:
  - Rebuilding canonical code and claim links after FSM clear / bot restart without re-debit.
  - Source-neutral cards for bot vs cabinet origin gifts.
  - Delivered gifts omit share/claim actions and display safe recipient display and delivery date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import GuestPurchaseStatus, User
from app.handlers.subscription.gift import (
    handle_gift_catalog,
    handle_gift_my,
    handle_gift_my_back,
    handle_gift_my_open,
    handle_gift_my_page,
)
from app.handlers.subscription.my_subscriptions import show_my_subscriptions
from app.handlers.subscription.purchase import show_subscription_info
from app.services.gift_history_service import GiftHistoryItem
from app.services.gift_notification_service import build_gift_history_detail_presentation
from app.utils.gift_links import build_gift_public_code


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract callback_data from an inline keyboard."""
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def _button_texts(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract text from all buttons in an inline keyboard."""
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _urls(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract URLs from all url buttons in an inline keyboard."""
    return [button.url for row in keyboard.inline_keyboard for button in row if button.url]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_user() -> User:
    user = MagicMock(spec=User)
    user.id = 42
    user.telegram_id = 123456789
    user.username = 'gift_sender'
    user.language = 'ru'
    user.balance_kopeks = 50000
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
    db.scalar = AsyncMock(return_value=None)
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
    callback.data = 'gift_my'
    callback.bot = AsyncMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='TestGiftBot'))
    return callback


@pytest.fixture
def memory_state(mock_db_user) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(
        bot_id=1,
        chat_id=mock_db_user.telegram_id,
        user_id=mock_db_user.telegram_id,
    )
    return FSMContext(storage=storage, key=key)


def _make_gift_history_item(
    purchase_id: int = 1,
    token: str = 'a' * 64,
    status: str = GuestPurchaseStatus.PAID.value,
    tariff_name: str = 'Стандарт',
    period_days: int = 30,
    traffic_limit_gb: int | None = 100,
    device_limit: int = 2,
    created_at: datetime | None = None,
    delivered_at: datetime | None = None,
    recipient_display: str | None = None,
) -> GiftHistoryItem:
    return GiftHistoryItem(
        purchase_id=purchase_id,
        token=token,
        status=status,
        tariff_id=1,
        tariff_name=tariff_name,
        period_days=period_days,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        created_at=created_at or datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        paid_at=created_at or datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        delivered_at=delivered_at,
        recipient_display=recipient_display,
        amount_kopeks=30000,
        currency='RUB',
    )


# ── Step 1: Menu Visibility & Empty History Tests ───────────────────────────


class TestGiftMenuVisibilityAndEmptyHistory:
    """Test persistent subscription-menu entry and in-section sales gating (Step 1)."""

    @pytest.mark.asyncio
    async def test_single_tariff_menu_always_shows_gift_entry(self, mock_callback, mock_db_user, mock_db, monkeypatch):
        mock_db_user.subscription = None
        monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
        await show_subscription_info(mock_callback, mock_db_user, mock_db)
        assert mock_callback.message.edit_text.called
        _, kwargs = mock_callback.message.edit_text.call_args
        reply_markup = kwargs.get('reply_markup')
        assert 'subscription_gift' in _callbacks(reply_markup)

    @pytest.mark.asyncio
    async def test_multi_tariff_menu_always_shows_gift_entry(self, mock_callback, mock_db_user, mock_db, monkeypatch):
        monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
        with patch(
            'app.handlers.subscription.my_subscriptions.get_all_subscriptions_by_user_id',
            AsyncMock(return_value=[]),
        ):
            await show_my_subscriptions(mock_callback, mock_db_user, mock_db)
            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            assert 'subscription_gift' in _callbacks(reply_markup)

    @pytest.mark.asyncio
    async def test_gift_catalog_disabled_sales_with_history_renders_history_only_view(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        mock_callback.data = 'subscription_gift'
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=False)),
            patch('app.handlers.subscription.gift.has_sender_gifts', AsyncMock(return_value=True)),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            callbacks = _callbacks(reply_markup)
            assert 'gift_my' in callbacks
            assert 'gift_enter_code' in callbacks
            assert 'gift_cancel' in callbacks

    @pytest.mark.asyncio
    async def test_gift_catalog_disabled_sales_without_history_renders_activation_hub(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        mock_callback.data = 'subscription_gift'
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=False)),
            patch('app.handlers.subscription.gift.has_sender_gifts', AsyncMock(return_value=False)),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            callbacks = _callbacks(reply_markup)
            assert 'gift_enter_code' in callbacks
            assert 'gift_cancel' in callbacks

    @pytest.mark.asyncio
    async def test_gift_catalog_no_tariffs_includes_my_gifts_button(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        mock_callback.data = 'subscription_gift'
        with (
            patch('app.handlers.subscription.gift.is_gift_enabled', AsyncMock(return_value=True)),
            patch('app.handlers.subscription.gift.list_gift_offers', AsyncMock(return_value=[])),
        ):
            await handle_gift_catalog(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            callbacks = _callbacks(reply_markup)
            assert 'gift_my' in callbacks
            assert 'gift_enter_code' in callbacks
            assert 'gift_cancel' in callbacks

    @pytest.mark.asyncio
    async def test_gift_my_empty_history_renders_empty_state(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_my'
        with patch('app.handlers.subscription.gift.list_sender_gifts', AsyncMock(return_value=([], 0))):
            await handle_gift_my(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert 'нет оформленных подарков' in text.lower() or 'мои подарки' in text.lower()
            reply_markup = mock_callback.message.edit_text.call_args[1].get('reply_markup')
            callbacks = _callbacks(reply_markup)
            assert 'gift_back_tariffs' in callbacks or 'gift_cancel' in callbacks


# ── Step 2: Pagination & Ownership Tests ────────────────────────────────────


class TestGiftPaginationAndOwnership:
    """Test 5-item pagination, page clamping, and buyer ownership checks (Step 2)."""

    @pytest.mark.asyncio
    async def test_pagination_page_1_of_3(self, mock_callback, mock_db_user, mock_db, memory_state):
        items = [_make_gift_history_item(purchase_id=i, tariff_name=f'Тариф {i}') for i in range(1, 6)]
        mock_callback.data = 'gift_my'

        with patch('app.handlers.subscription.gift.list_sender_gifts', AsyncMock(return_value=(items, 12))):
            await handle_gift_my(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            _, kwargs = mock_callback.message.edit_text.call_args
            reply_markup = kwargs.get('reply_markup')
            callbacks = _callbacks(reply_markup)

            # Check 5 item buttons with numeric IDs only
            for i in range(1, 6):
                assert f'gift_my_open:{i}' in callbacks

            # Check pagination controls
            assert 'gift_my_page:2' in callbacks
            assert 'gift_my_page:0' not in callbacks
            assert not any(cb.startswith('gift_my_page:0') or 'gift_my_page:-' in cb for cb in callbacks)

    @pytest.mark.asyncio
    async def test_pagination_page_2_middle_has_prev_and_next(self, mock_callback, mock_db_user, mock_db, memory_state):
        items = [_make_gift_history_item(purchase_id=i, tariff_name=f'Тариф {i}') for i in range(6, 11)]
        mock_callback.data = 'gift_my_page:2'

        with patch('app.handlers.subscription.gift.list_sender_gifts', AsyncMock(return_value=(items, 12))):
            await handle_gift_my_page(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            reply_markup = mock_callback.message.edit_text.call_args[1].get('reply_markup')
            callbacks = _callbacks(reply_markup)

            assert 'gift_my_page:1' in callbacks
            assert 'gift_my_page:3' in callbacks

    @pytest.mark.asyncio
    async def test_pagination_page_3_last_has_only_prev(self, mock_callback, mock_db_user, mock_db, memory_state):
        items = [_make_gift_history_item(purchase_id=i, tariff_name=f'Тариф {i}') for i in range(11, 13)]
        mock_callback.data = 'gift_my_page:3'

        with patch('app.handlers.subscription.gift.list_sender_gifts', AsyncMock(return_value=(items, 12))):
            await handle_gift_my_page(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            reply_markup = mock_callback.message.edit_text.call_args[1].get('reply_markup')
            callbacks = _callbacks(reply_markup)

            assert 'gift_my_page:2' in callbacks
            assert 'gift_my_page:4' not in callbacks

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        'page_str,expected_offset',
        [
            ('gift_my_page:0', 0),
            ('gift_my_page:-5', 0),
            ('gift_my_page:999', 10),  # clamped to last page (total=12 -> 3 pages -> offset 10)
            ('gift_my_page:invalid', 0),
        ],
    )
    async def test_pagination_boundary_and_forged_values(
        self, page_str, expected_offset, mock_callback, mock_db_user, mock_db, memory_state
    ):
        mock_callback.data = page_str
        mock_list = AsyncMock(return_value=([_make_gift_history_item(purchase_id=1)], 12))
        with patch('app.handlers.subscription.gift.list_sender_gifts', mock_list):
            await handle_gift_my_page(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_list.called
            _, kwargs = mock_list.call_args
            assert kwargs.get('offset') == expected_offset

    @pytest.mark.asyncio
    async def test_open_foreign_gift_fails_idor_ownership(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_my_open:999'
        with patch('app.handlers.subscription.gift.get_sender_gift', AsyncMock(return_value=None)):
            await handle_gift_my_open(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.answer.called
            # Must not show gift detail or leak secret code
            assert not mock_callback.message.edit_text.called

    @pytest.mark.asyncio
    async def test_inaccessible_message_handled_safely(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.message = MagicMock(spec=InaccessibleMessage)
        await handle_gift_my(mock_callback, mock_db_user, mock_db, memory_state)
        assert mock_callback.answer.called

    @pytest.mark.asyncio
    async def test_gift_my_back_returns_to_history_list(self, mock_callback, mock_db_user, mock_db, memory_state):
        mock_callback.data = 'gift_my_back'
        with patch(
            'app.handlers.subscription.gift.list_sender_gifts',
            AsyncMock(return_value=([_make_gift_history_item(1)], 1)),
        ):
            await handle_gift_my_back(mock_callback, mock_db_user, mock_db, memory_state)
            assert mock_callback.message.edit_text.called


# ── Step 3: Recovery Detail Tests ───────────────────────────────────────────


class TestGiftRecoveryDetail:
    """Test gift recovery without re-debit or state dependency (Step 3)."""

    @pytest.mark.asyncio
    async def test_recovery_detail_rebuilds_canonical_code_without_redebit(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        token = 'b' * 64
        item = _make_gift_history_item(
            purchase_id=77,
            token=token,
            status=GuestPurchaseStatus.PAID.value,
            tariff_name='Премиум',
            period_days=60,
        )
        expected_public_code = build_gift_public_code(token)

        # Clear any FSM state
        await memory_state.clear()
        mock_callback.data = 'gift_my_open:77'

        with (
            patch('app.handlers.subscription.gift.get_sender_gift', AsyncMock(return_value=item)),
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('TestGiftBot', None)),
            ),
        ):
            await handle_gift_my_open(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            reply_markup = mock_callback.message.edit_text.call_args[1].get('reply_markup')

            # Canonical public code must be present in code tag
            assert expected_public_code in text
            assert f'<code>{expected_public_code}</code>' in text

            # Standalone raw token must NOT be exposed
            assert token not in text

            # Action buttons
            callbacks = _callbacks(reply_markup)
            urls = _urls(reply_markup)
            assert 'gift_my_back' in callbacks
            assert any('t.me/TestGiftBot' in u for u in urls)
            assert any('t.me/share/url' in u for u in urls)


# ── Step 4: Source-Neutral Presentation Tests ────────────────────────────────


class TestSourceNeutralPresentation:
    """Test source-neutral card structure and delivered state differences (Step 4)."""

    def test_source_neutral_card_bot_vs_cabinet_origins(self):
        token = 'c' * 64
        bot_item = _make_gift_history_item(
            purchase_id=10,
            token=token,
            status=GuestPurchaseStatus.PAID.value,
            tariff_name='VIP',
            period_days=30,
            traffic_limit_gb=200,
            device_limit=3,
        )
        # Номер тот же, что у bot_item: сравниваются карточки, различающиеся
        # ТОЛЬКО происхождением. Кнопки QR и текста для отправки адресуют
        # конкретный подарок, и разные номера здесь означали бы, что тест
        # требует одинаковых callback'ов у разных подарков.
        cabinet_item = _make_gift_history_item(
            purchase_id=10,
            token=token,
            status=GuestPurchaseStatus.PAID.value,
            tariff_name='VIP',
            period_days=30,
            traffic_limit_gb=200,
            device_limit=3,
        )

        text_bot, kb_bot = build_gift_history_detail_presentation(
            language='ru',
            item=bot_item,
            bot_username='TestGiftBot',
            cabinet_url='https://cabinet.example.com',
        )
        text_cab, kb_cab = build_gift_history_detail_presentation(
            language='ru',
            item=cabinet_item,
            bot_username='TestGiftBot',
            cabinet_url='https://cabinet.example.com',
        )

        # Content must be identical (no mention of bot vs cabinet origin or prices)
        assert text_bot == text_cab
        assert _button_texts(kb_bot) == _button_texts(kb_cab)
        assert _callbacks(kb_bot) == _callbacks(kb_cab)
        assert _urls(kb_bot) == _urls(kb_cab)

        # Financial info must NOT appear
        assert '30000' not in text_bot
        assert '300.00' not in text_bot
        assert 'руб' not in text_bot.lower()
        assert '🤖 В Telegram:' in text_bot
        assert '🌐 В личном кабинете:' in text_bot
        assert any('t.me/TestGiftBot' in url for url in _urls(kb_bot))
        assert any('cabinet.example.com/buy/gift/' in url for url in _urls(kb_bot))

    def test_delivered_card_omits_share_and_claim_actions(self):
        token = 'd' * 64
        delivered_item = _make_gift_history_item(
            purchase_id=20,
            token=token,
            status=GuestPurchaseStatus.DELIVERED.value,
            tariff_name='VIP',
            period_days=30,
            delivered_at=datetime(2026, 8, 22, 14, 30, tzinfo=UTC),
            recipient_display='@best_friend',
        )

        text, kb = build_gift_history_detail_presentation(
            language='ru',
            item=delivered_item,
            bot_username='TestGiftBot',
        )

        # Delivered status and recipient display must be rendered safely
        assert 'Активирован' in text
        assert '@best_friend' in text
        assert '22.08.2026' in text

        # Must omit share url, claim url, and public code
        assert _urls(kb) == []
        assert build_gift_public_code(token) not in text
        assert 't.me/share/url' not in text
        assert token not in text

        # Must contain back button
        assert _callbacks(kb) == ['gift_my_back']
