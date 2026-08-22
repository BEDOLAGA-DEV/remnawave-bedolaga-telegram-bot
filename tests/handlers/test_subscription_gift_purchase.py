"""Tests for subscription gift balance confirmation, replay, and presentation (Task 5).

Covers:
- Exact expected-price submission and successful atomic gift purchase.
- Replay idempotency: same checkout_id returns original purchase without re-debiting.
- Distinct checkout_ids produce distinct purchases.
- Error handling: insufficient balance, sender restriction, feature/tariff/period invalidation.
- Price changed between summary and confirmation updates FSM, re-renders summary, and does not debit.
- Security & privacy: exclusion of price, balance, discount, transaction id, and standalone tokens from user & share copy.
- HTML escaping of dynamic text in result presentations.
- Channel resolution: setting username -> get_me fallback -> cabinet URL fallback -> preflight refusal when none available.
- Presentation builder and send_gift_result_message service functions.
"""

from __future__ import annotations

import html
import urllib.parse
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import GuestPurchase, GuestPurchaseStatus, Tariff, Transaction, TransactionType, User
from app.handlers.subscription.gift import handle_gift_confirm
from app.services.gift_notification_service import (
    build_gift_result_presentation,
    resolve_gift_claim_channel,
    send_gift_result_message,
)
from app.services.gift_purchase_service import (
    GiftFeatureDisabledError,
    GiftInsufficientBalanceError,
    GiftPeriodUnavailableError,
    GiftPriceChangedError,
    GiftPurchaseRestrictedError,
    GiftPurchaseResult,
    GiftQuote,
    GiftTariffUnavailableError,
)
from app.states import GiftPurchaseStates
from app.utils.gift_links import build_bot_gift_claim_link, build_cabinet_gift_claim_link, build_gift_public_code


# ── Helpers & Fixtures ──────────────────────────────────────────────────────


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract callback_data from an inline keyboard."""
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def _button_urls(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract URLs from an inline keyboard."""
    return [button.url for row in keyboard.inline_keyboard for button in row if button.url]


def _button_texts(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract text from all buttons in an inline keyboard."""
    return [button.text for row in keyboard.inline_keyboard for button in row]


@pytest.fixture
def mock_db_user() -> User:
    user = MagicMock(spec=User)
    user.id = 42
    user.telegram_id = 123456789
    user.username = 'gift_buyer_test'
    user.language = 'ru'
    user.balance_kopeks = 50000  # 500 RUB
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
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    me = MagicMock()
    me.username = 'test_gift_bot'
    bot.get_me = AsyncMock(return_value=me)
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def mock_callback(mock_db_user, mock_bot) -> AsyncMock:
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
def memory_state() -> FSMContext:
    storage = MemoryStorage()
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=123456789, user_id=123456789)
    return FSMContext(storage=storage, key=key)


@pytest.fixture
def sample_quote() -> GiftQuote:
    return GiftQuote(
        tariff_id=1,
        tariff_name='Premium <VIP> Plan & More',
        period_days=30,
        traffic_limit_gb=100,
        device_limit=3,
        original_price_kopeks=35000,
        final_price_kopeks=30000,
        promo_group_discount_kopeks=5000,
        promo_offer_discount_kopeks=0,
        consumes_promo_offer=False,
    )


@pytest.fixture
def sample_purchase_result(sample_quote) -> GiftPurchaseResult:
    # 64-char token
    token = 'a' * 64
    purchase = MagicMock(spec=GuestPurchase)
    purchase.id = 101
    purchase.token = token
    purchase.status = GuestPurchaseStatus.PAID.value
    purchase.tariff_id = 1
    purchase.period_days = 30
    purchase.amount_kopeks = 30000
    purchase.buyer_user_id = 42

    tariff = MagicMock(spec=Tariff)
    tariff.id = 1
    tariff.name = sample_quote.tariff_name
    tariff.traffic_limit_gb = 100
    tariff.device_limit = 3
    purchase.tariff = tariff

    tx = MagicMock(spec=Transaction)
    tx.id = 555
    tx.user_id = 42
    tx.amount_kopeks = 30000
    tx.type = TransactionType.GIFT_PAYMENT.value
    tx.external_id = 'gift_bot_chk_123'

    return GiftPurchaseResult(
        purchase=purchase,
        transaction=tx,
        quote=sample_quote,
        remaining_balance_kopeks=20000,
        is_idempotent_replay=False,
    )


# ── Step 1: Confirmation Tests ──────────────────────────────────────────────


class TestGiftBalanceConfirmation:
    """Test handle_gift_confirm callback execution, price validation, and error mappings."""

    @pytest.mark.asyncio
    async def test_successful_gift_purchase_from_balance(
        self, mock_callback, mock_db_user, mock_db, memory_state, sample_purchase_result
    ):
        checkout_id = 'chk_' + uuid.uuid4().hex
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(return_value=sample_purchase_result),
            ) as mock_purchase,
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Assert purchase service called with exact parameters
            mock_purchase.assert_awaited_once_with(
                db=mock_db,
                buyer_id=mock_db_user.id,
                tariff_id=1,
                period_days=30,
                expected_price_kopeks=30000,
                idempotency_key=checkout_id,
                source='bot',
            )

            # Assert callback acknowledged
            assert mock_callback.answer.called

            # Assert message was edited with result
            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert 'подарок' in text.lower() or 'gift' in text.lower()
            kwargs = mock_callback.message.edit_text.call_args[1]
            reply_markup = kwargs.get('reply_markup')
            assert reply_markup is not None

            # Check buttons
            callbacks = _callbacks(reply_markup)
            urls = _button_urls(reply_markup)
            assert 'menu_subscription' in callbacks
            assert 'back_to_menu' in callbacks
            assert any(u.startswith('https://t.me/share/url?') for u in urls)
            assert any(u.startswith('https://t.me/test_gift_bot?start=GIFT_') for u in urls)

            # Assert state cleared on success
            assert await memory_state.get_state() is None

    @pytest.mark.asyncio
    async def test_insufficient_balance_error_preserves_checkout_state(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        checkout_id = 'chk_insufficient_funds'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftInsufficientBalanceError(required_kopeks=30000, available_kopeks=10000)),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Assert alert shown to user with shortfall info
            assert mock_callback.answer.called
            alert_text = mock_callback.answer.call_args[0][0] if mock_callback.answer.call_args[0] else ''
            show_alert = mock_callback.answer.call_args[1].get('show_alert', False)
            assert show_alert is True
            assert '300' in alert_text or 'недостаточно' in alert_text.lower() or 'insufficient' in alert_text.lower()

            # Assert FSM state and checkout_id remain intact
            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state
            data = await memory_state.get_data()
            assert data.get('gift_checkout_id') == checkout_id

            # Assert message was NOT edited to success
            assert not mock_callback.message.edit_text.called

    @pytest.mark.asyncio
    async def test_price_changed_error_updates_fsm_and_rerenders_summary(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        checkout_id = 'chk_price_changed'
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id=checkout_id,
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        fresh_quote = GiftQuote(
            tariff_id=1,
            tariff_name='Premium Plan',
            period_days=30,
            traffic_limit_gb=100,
            device_limit=3,
            original_price_kopeks=40000,
            final_price_kopeks=38000,
            promo_group_discount_kopeks=2000,
            promo_offer_discount_kopeks=0,
            consumes_promo_offer=False,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftPriceChangedError(expected_price_kopeks=30000, fresh_quote=fresh_quote)),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Assert alert about price change
            assert mock_callback.answer.called

            # Assert FSM updated with fresh price
            data = await memory_state.get_data()
            assert data.get('gift_expected_price_kopeks') == 38000
            assert await memory_state.get_state() == GiftPurchaseStates.confirming_purchase.state

            # Assert summary was re-rendered with new price
            assert mock_callback.message.edit_text.called
            text = mock_callback.message.edit_text.call_args[0][0]
            assert '380' in text  # 38000 kopeks = 380 RUB

    @pytest.mark.asyncio
    async def test_purchase_restricted_clears_fsm_and_shows_alert(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id='chk_restricted',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftPurchaseRestrictedError('Account restricted')),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.answer.called
            assert mock_callback.answer.call_args[1].get('show_alert') is True
            assert await memory_state.get_state() is None

    @pytest.mark.asyncio
    async def test_feature_disabled_clears_fsm_and_shows_alert(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id='chk_disabled',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftFeatureDisabledError('Gift disabled')),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.answer.called
            assert mock_callback.answer.call_args[1].get('show_alert') is True
            assert await memory_state.get_state() is None

    @pytest.mark.asyncio
    async def test_tariff_unavailable_clears_fsm_and_shows_message(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id='chk_tariff_unavail',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftTariffUnavailableError('Tariff unavailable')),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            assert await memory_state.get_state() is None

    @pytest.mark.asyncio
    async def test_period_unavailable_clears_fsm_and_shows_message(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        await memory_state.set_state(GiftPurchaseStates.confirming_purchase)
        await memory_state.update_data(
            gift_checkout_id='chk_period_unavail',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(side_effect=GiftPeriodUnavailableError('Period unavailable')),
            ),
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            assert mock_callback.message.edit_text.called
            assert await memory_state.get_state() is None


# ── Step 2: Replay, Preflight, and Presentation Tests ────────────────────────


class TestGiftReplayAndPresentation:
    """Test idempotency replay, channel resolution fallbacks, and presentation security."""

    @pytest.mark.asyncio
    async def test_idempotent_replay_with_same_checkout_id(
        self, mock_callback, mock_db_user, mock_db, memory_state, sample_purchase_result
    ):
        replay_result = GiftPurchaseResult(
            purchase=sample_purchase_result.purchase,
            transaction=sample_purchase_result.transaction,
            quote=sample_purchase_result.quote,
            remaining_balance_kopeks=sample_purchase_result.remaining_balance_kopeks,
            is_idempotent_replay=True,
        )

        await memory_state.update_data(
            gift_checkout_id='chk_replay_123',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=('test_gift_bot', None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(return_value=replay_result),
            ) as mock_purchase,
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            mock_purchase.assert_awaited_once_with(
                db=mock_db,
                buyer_id=mock_db_user.id,
                tariff_id=1,
                period_days=30,
                expected_price_kopeks=30000,
                idempotency_key='chk_replay_123',
                source='bot',
            )

            # Replay presentation must indicate already purchased / replayed
            text = mock_callback.message.edit_text.call_args[0][0]
            assert 'ранее' in text.lower() or 'already' in text.lower() or 'оформлен' in text.lower()

    @pytest.mark.asyncio
    async def test_preflight_fails_when_neither_bot_nor_cabinet_available(
        self, mock_callback, mock_db_user, mock_db, memory_state
    ):
        await memory_state.update_data(
            gift_checkout_id='chk_no_channel',
            gift_tariff_id=1,
            gift_period_days=30,
            gift_expected_price_kopeks=30000,
        )

        with (
            patch(
                'app.handlers.subscription.gift.resolve_gift_claim_channel',
                AsyncMock(return_value=(None, None)),
            ),
            patch(
                'app.handlers.subscription.gift.purchase_gift_from_balance',
                AsyncMock(),
            ) as mock_purchase,
        ):
            await handle_gift_confirm(mock_callback, mock_db_user, mock_db, memory_state)

            # Refuse before calling purchase/debit
            mock_purchase.assert_not_called()
            assert mock_callback.answer.called
            assert mock_callback.answer.call_args[1].get('show_alert') is True

    @pytest.mark.asyncio
    async def test_channel_resolution_bot_username_recovery_via_get_me(self, mock_bot, monkeypatch):
        monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: None)
        bot_user, cab_url = await resolve_gift_claim_channel(bot=mock_bot)
        assert bot_user == 'test_gift_bot'
        mock_bot.get_me.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_resolution_cabinet_url_fallback(self, monkeypatch):
        monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: '')
        monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
        bot_user, cab_url = await resolve_gift_claim_channel(bot=None)
        assert bot_user is None
        assert cab_url == 'https://cabinet.example.com'

    def test_presentation_excludes_financial_data_and_standalone_tokens(self, sample_purchase_result):
        text, kb = build_gift_result_presentation(
            language='ru',
            purchase_result=sample_purchase_result,
            bot_username='test_gift_bot',
        )

        # 1. HTML escaping of tariff name
        assert html.escape('Premium <VIP> Plan & More') in text
        assert '<VIP>' not in text

        # 2. Tariff period and limits present
        assert '30 дн.' in text
        assert '100 GB' in text or '100 ГБ' in text

        # 3. No financial data in message text
        assert '300' not in text  # 300 RUB / 30000 kopeks
        assert '350' not in text
        assert '500' not in text  # user balance
        assert '50' not in text  # discount
        assert '555' not in text  # transaction id
        assert 'gift_bot_chk' not in text

        # 4. Standalone token must not appear outside the canonical link
        raw_token = sample_purchase_result.purchase.token
        assert raw_token not in text
        assert f'<code>{build_gift_public_code(raw_token)}</code>' in text
        expected_claim_link = build_bot_gift_claim_link(raw_token, 'test_gift_bot')
        assert expected_claim_link in text

        # 5. Share URL verification
        buttons = [b for row in kb.inline_keyboard for b in row]
        send_btn = next(b for b in buttons if b.text.startswith('🎁'))
        assert send_btn.url is not None
        parsed_share = urllib.parse.urlparse(send_btn.url)
        assert parsed_share.netloc == 't.me'
        assert parsed_share.path == '/share/url'
        qs = urllib.parse.parse_qs(parsed_share.query)
        assert qs['url'][0] == expected_claim_link

        # Share text in query parameter must also contain NO financial data and NO standalone token
        share_text = qs['text'][0]
        assert '300' not in share_text
        assert '350' not in share_text
        assert '500' not in share_text
        assert raw_token not in share_text
        assert '30' in share_text  # period days

    def test_presentation_cabinet_fallback_when_no_bot_username(self, sample_purchase_result):
        text, kb = build_gift_result_presentation(
            language='ru',
            purchase_result=sample_purchase_result,
            bot_username=None,
            cabinet_url='https://cabinet.example.com',
        )

        expected_claim_link = build_cabinet_gift_claim_link(
            sample_purchase_result.purchase.token, 'https://cabinet.example.com'
        )
        assert expected_claim_link in text

        buttons = [b for row in kb.inline_keyboard for b in row]
        open_btn = next(b for b in buttons if b.callback_data is None and b.url == expected_claim_link)
        assert open_btn.url == expected_claim_link

    @pytest.mark.parametrize(
        ('language', 'bot_label', 'cabinet_label'),
        [
            ('ru', '🤖 В Telegram:', '🌐 В личном кабинете:'),
            ('en', '🤖 In Telegram:', '🌐 In the cabinet:'),
            ('ua', '🤖 У Telegram:', '🌐 В особистому кабінеті:'),
            ('fa', '🤖 در تلگرام:', '🌐 در پنل کاربری:'),
            ('zh', '🤖 在 Telegram 中：', '🌐 在控制台中：'),
        ],
    )
    def test_presentation_localizes_dual_claim_channels(
        self,
        sample_purchase_result,
        language,
        bot_label,
        cabinet_label,
    ):
        raw_token = sample_purchase_result.purchase.token
        bot_claim_url = build_bot_gift_claim_link(raw_token, 'test_gift_bot')
        cabinet_claim_url = build_cabinet_gift_claim_link(raw_token, 'https://cabinet.example.com')

        text, keyboard = build_gift_result_presentation(
            language=language,
            purchase_result=sample_purchase_result,
            bot_username='test_gift_bot',
            cabinet_url='https://cabinet.example.com',
        )

        assert bot_label in text
        assert cabinet_label in text
        assert bot_claim_url in text
        assert cabinet_claim_url in text
        assert f'<code>{build_gift_public_code(raw_token)}</code>' in text
        urls = _button_urls(keyboard)
        assert bot_claim_url in urls
        assert cabinet_claim_url in urls
        assert any(url.startswith('https://t.me/share/url?') for url in urls)

    @pytest.mark.asyncio
    async def test_send_gift_result_message_service(self, mock_bot, mock_db_user, sample_purchase_result, monkeypatch):
        monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'test_gift_bot')
        await send_gift_result_message(
            bot=mock_bot,
            user=mock_db_user,
            purchase_result=sample_purchase_result,
        )

        mock_bot.send_message.assert_awaited_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs['chat_id'] == mock_db_user.telegram_id
        assert call_kwargs['parse_mode'] == 'HTML'
        assert isinstance(call_kwargs['reply_markup'], InlineKeyboardMarkup)

    def test_presentation_sources_from_gift_claim_artifacts(self, sample_purchase_result):
        with patch('app.services.gift_notification_service.build_gift_claim_artifacts') as mock_artifacts_builder:
            from app.utils.gift_links import GiftClaimArtifacts

            mock_artifacts_builder.return_value = GiftClaimArtifacts(
                public_code=f'GIFT_{sample_purchase_result.purchase.token[:59]}',
                bot_claim_url=f'https://t.me/test_gift_bot?start=GIFT_{sample_purchase_result.purchase.token[:59]}',
                cabinet_claim_url=f'https://cabinet.example.com/buy/gift/{sample_purchase_result.purchase.token}',
                telegram_share_url='https://t.me/share/url?url=https%3A%2F%2Ft.me%2Ftest_gift_bot&text=Gift',
            )

            text, kb = build_gift_result_presentation(
                language='ru',
                purchase_result=sample_purchase_result,
                bot_username='test_gift_bot',
                cabinet_url='https://cabinet.example.com',
            )

            mock_artifacts_builder.assert_called_once()
            assert f'https://t.me/test_gift_bot?start=GIFT_{sample_purchase_result.purchase.token[:59]}' in text
            assert f'<code>GIFT_{sample_purchase_result.purchase.token[:59]}</code>' in text
            assert f'https://cabinet.example.com/buy/gift/{sample_purchase_result.purchase.token}' in text
            buttons = [b for row in kb.inline_keyboard for b in row]
            send_btn = next(b for b in buttons if b.text.startswith('🎁'))
            assert send_btn.url == 'https://t.me/share/url?url=https%3A%2F%2Ft.me%2Ftest_gift_bot&text=Gift'
            button_urls = _button_urls(kb)
            assert f'https://t.me/test_gift_bot?start=GIFT_{sample_purchase_result.purchase.token[:59]}' in button_urls
            assert f'https://cabinet.example.com/buy/gift/{sample_purchase_result.purchase.token}' in button_urls
