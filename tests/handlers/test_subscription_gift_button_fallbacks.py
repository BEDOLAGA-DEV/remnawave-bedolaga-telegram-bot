"""Tests verifying fallback behavior for all gift inline buttons (Task 4, Step 7).

Every new gift inline button must resolve its text through `texts.t(key, default)`
with a non-empty Russian default label. When a locale key is missing or stripped,
the intended default label must be rendered without breaking callback_data or URLs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.database.models import User
from app.handlers.subscription.gift import (
    _render_confirmation_summary,
    _render_period_selection,
    _render_tariff_catalog,
)
from app.handlers.subscription.my_subscriptions import _build_subscriptions_keyboard
from app.keyboards.inline import get_subscription_keyboard
from app.localization.texts import Texts, get_texts
from app.services.gift_purchase_service import GiftQuote, GiftTariffOffer


@pytest.fixture
def mock_user() -> User:
    user = MagicMock(spec=User)
    user.id = 1
    user.language = 'ru'
    user.balance_kopeks = 50000
    return user


@pytest.fixture(autouse=True)
def simulate_missing_gift_keys(monkeypatch):
    """Simulate missing GIFT_* keys in locale files while preserving other keys."""
    orig_get_value = Texts._get_value

    def fake_get_value(self, item: str, warn: bool = True):
        if item.startswith('GIFT_'):
            raise KeyError(item)
        return orig_get_value(self, item, warn=warn)

    monkeypatch.setattr(Texts, '_get_value', fake_get_value)


class TestGiftButtonFallbacks:
    """Test that all gift buttons have non-empty default fallbacks."""

    def test_gift_subscription_button_fallback_in_single_keyboard(self):
        """Test GIFT_SUBSCRIPTION_BUTTON fallback in get_subscription_keyboard."""
        kb = get_subscription_keyboard(language='ru', has_subscription=False, gift_enabled=True)
        gift_buttons = [b for row in kb.inline_keyboard for b in row if b.callback_data == 'subscription_gift']
        assert len(gift_buttons) == 1
        assert gift_buttons[0].text == '🎁 Подарить подписку'

    def test_gift_subscription_button_fallback_in_multi_keyboard(self):
        """Test GIFT_SUBSCRIPTION_BUTTON fallback in _build_subscriptions_keyboard."""
        subs = [SimpleNamespace(id=1, tariff=SimpleNamespace(name='Pro'))]
        kb = _build_subscriptions_keyboard(subs, language='ru', gift_enabled=True)
        gift_buttons = [b for row in kb.inline_keyboard for b in row if b.callback_data == 'subscription_gift']
        assert len(gift_buttons) == 1
        assert gift_buttons[0].text == '🎁 Подарить подписку'

    def test_gift_catalog_buttons_fallback(self, mock_user):
        """Test tariff choice and cancel button fallbacks in _render_tariff_catalog."""
        offers = [
            GiftTariffOffer(
                tariff_id=1,
                tariff_name='Standard',
                tariff_description='Desc',
                traffic_limit_gb=100,
                device_limit=2,
                display_order=1,
                quotes=(GiftQuote(1, 'Standard', 30, 100, 2, 30000, 30000, 0, 0, False),),
            )
        ]
        _, kb = _render_tariff_catalog(mock_user, offers)
        buttons = [b for row in kb.inline_keyboard for b in row]

        tariff_btn = next(b for b in buttons if b.callback_data == 'gift_tariff:1')
        assert tariff_btn.text == 'Standard'

        enter_code_btn = next(b for b in buttons if b.callback_data == 'gift_enter_code')
        assert enter_code_btn.text == '🎁 Активировать код'

        cancel_btn = next(b for b in buttons if b.callback_data == 'gift_cancel')
        assert cancel_btn.text == '❌ Отмена'

    def test_gift_period_selection_buttons_fallback(self, mock_user):
        """Test period choice, discount, back to tariffs, and cancel fallbacks in _render_period_selection."""
        offer = GiftTariffOffer(
            tariff_id=1,
            tariff_name='Standard',
            tariff_description='Desc',
            traffic_limit_gb=100,
            device_limit=2,
            display_order=1,
            quotes=(
                GiftQuote(1, 'Standard', 30, 100, 2, 30000, 30000, 0, 0, False),
                GiftQuote(1, 'Standard', 90, 100, 2, 90000, 80000, 10000, 0, False),
            ),
        )
        _, kb = _render_period_selection(mock_user, offer)
        buttons = [b for row in kb.inline_keyboard for b in row]

        p30_btn = next(b for b in buttons if b.callback_data == 'gift_period:1:30')
        assert '30 дн.' in p30_btn.text
        assert '300' in p30_btn.text

        p90_btn = next(b for b in buttons if b.callback_data == 'gift_period:1:90')
        assert '90 дн.' in p90_btn.text
        assert '800' in p90_btn.text
        assert '-11%' in p90_btn.text

        back_btn = next(b for b in buttons if b.callback_data == 'gift_back_tariffs')
        assert back_btn.text == '◀️ К тарифам'

        cancel_btn = next(b for b in buttons if b.callback_data == 'gift_cancel')
        assert cancel_btn.text == '❌ Отмена'

    def test_gift_confirmation_summary_buttons_fallback(self, mock_user):
        """Test confirm, back to periods, and cancel fallbacks in _render_confirmation_summary."""
        quote = GiftQuote(1, 'Standard', 30, 100, 2, 30000, 30000, 0, 0, False)
        _, kb = _render_confirmation_summary(mock_user, quote)
        buttons = [b for row in kb.inline_keyboard for b in row]

        confirm_btn = next(b for b in buttons if b.callback_data == 'gift_confirm')
        assert confirm_btn.text == '✅ Подтвердить покупку'

        back_btn = next(b for b in buttons if b.callback_data == 'gift_back_periods')
        assert back_btn.text == '◀️ К периодам'

        cancel_btn = next(b for b in buttons if b.callback_data == 'gift_cancel')
        assert cancel_btn.text == '❌ Отмена'

    def test_gift_history_buttons_fallback(self, mock_user):
        """Test history list and pagination fallbacks in _render_history_list."""
        from app.handlers.subscription.gift import _render_history_list
        from app.services.gift_history_service import GiftHistoryItem

        items = [
            GiftHistoryItem(
                purchase_id=1,
                token='t' * 64,
                status='PAID',
                tariff_id=1,
                tariff_name='Basic',
                period_days=30,
                traffic_limit_gb=100,
                device_limit=2,
                created_at=None,
                paid_at=None,
                delivered_at=None,
            )
        ]
        _, kb = _render_history_list(mock_user, items, page=2, total_count=15)
        buttons = [b for row in kb.inline_keyboard for b in row]

        item_btn = next(b for b in buttons if b.callback_data == 'gift_my_open:1')
        assert 'Basic — 30 дн.' in item_btn.text

        prev_btn = next(b for b in buttons if b.callback_data == 'gift_my_page:1')
        assert prev_btn.text == '⬅️ Предыдущая'

        next_btn = next(b for b in buttons if b.callback_data == 'gift_my_page:3')
        assert next_btn.text == 'Следующая ➡️'

        back_btn = next(b for b in buttons if b.callback_data == 'gift_back_tariffs')
        assert back_btn.text == '◀️ Назад'

    def test_reusable_button_keys_fallback_defaults(self):
        """Verify fallback strings for shared result, cart, and activation buttons."""
        texts = get_texts('ru')
        assert texts.t('GIFT_MY_BUTTON', '🎁 Мои подарки') == '🎁 Мои подарки'
        assert texts.t('GIFT_MY_PREV_PAGE_BUTTON', '⬅️ Предыдущая') == '⬅️ Предыдущая'
        assert texts.t('GIFT_MY_NEXT_PAGE_BUTTON', 'Следующая ➡️') == 'Следующая ➡️'
        assert texts.t('GIFT_MY_BACK_BUTTON', '◀️ К списку подарков') == '◀️ К списку подарков'
        assert texts.t('GIFT_MY_BACK_TO_CATALOG_BUTTON', '◀️ Назад') == '◀️ Назад'
        assert texts.t('GIFT_SEND_BUTTON', '🎁 Отправить подарок') == '🎁 Отправить подарок'
        assert texts.t('GIFT_OPEN_BUTTON', '🔗 Открыть подарок') == '🔗 Открыть подарок'
        assert texts.t('GIFT_BACK_TO_SUBSCRIPTION_BUTTON', '◀️ К подписке') == '◀️ К подписке'
        assert texts.t('GIFT_RETURN_TO_CART_BUTTON', '🎁 Вернуться к подарку') == '🎁 Вернуться к подарку'
        assert texts.t('GIFT_ENTER_CODE_BUTTON', '🎁 Активировать код') == '🎁 Активировать код'
        assert texts.t('GIFT_ACTIVATION_CANCEL_BUTTON', '❌ Отмена') == '❌ Отмена'
