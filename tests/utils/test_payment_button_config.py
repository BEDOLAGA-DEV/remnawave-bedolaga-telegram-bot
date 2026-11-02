import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.keyboards.inline import _apply_payment_name_overrides, get_payment_methods_keyboard
from app.services import payment_method_config_service as config_service


@pytest.fixture(autouse=True)
def display_cache(monkeypatch):
    monkeypatch.setattr(
        config_service, '_display_name_overrides', {'yookassa': 'Gateway', 'telegram_stars': 'Custom Stars'}
    )
    monkeypatch.setattr(
        config_service, '_display_sort_orders', {'yookassa': 0, 'telegram_stars': 1, 'tribute': 2}, raising=False
    )


@pytest.mark.parametrize(
    ('language', 'sbp', 'card'),
    [
        ('ru', '🏦 Оплата по СБП (Gateway)', '💳 Банковская карта (Gateway)'),
        ('en', '🏦 Pay via SBP (Gateway)', '💳 Bank card (Gateway)'),
        ('ua', '🏦 Оплата через СБП (Gateway)', '💳 Банківська картка (Gateway)'),
        ('fa', '🏦 پرداخت سریع (Gateway)', '💳 کارت بانکی (Gateway)'),
        ('zh', '🏦 通过 SBP 支付（Gateway）', '💳 银行卡（Gateway）'),
    ],
)
@pytest.mark.parametrize('amount', [0, 12345])
def test_yookassa_override_keeps_card_and_sbp_distinguishable(amount, language, sbp, card):
    def callback(method):
        return f'topup_amount|{method}|{amount}' if amount else f'topup_{method}'

    keyboard = [
        [InlineKeyboardButton(text='🏬 Оплатить по СБП (YooKassa)', callback_data=callback('yookassa_sbp'))],
        [InlineKeyboardButton(text='💳 Банковская карта (YooKassa)', callback_data=callback('yookassa'))],
        [InlineKeyboardButton(text='⭐ Telegram Stars', callback_data=callback('stars'))],
        [InlineKeyboardButton(text='Back', callback_data='menu_balance')],
    ]
    before = [row[0].callback_data for row in keyboard]
    _apply_payment_name_overrides(keyboard, language)
    assert keyboard[0][0].text == sbp
    assert keyboard[1][0].text == card
    assert keyboard[2][0].text == 'Custom Stars'
    assert keyboard[3][0].text == 'Back'
    assert [row[0].callback_data for row in keyboard] == before


@pytest.mark.parametrize('amount', [0, 12345])
def test_saved_order_groups_yookassa_before_stars_without_changing_callbacks(monkeypatch, amount):
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)
    monkeypatch.setattr(settings, 'YOOKASSA_SBP_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_yookassa_enabled', lambda self: True)
    markup = get_payment_methods_keyboard(amount, 'ru')
    assert isinstance(markup, InlineKeyboardMarkup)
    expected = [
        f'topup_amount|{method}|{amount}' if amount else f'topup_{method}'
        for method in ['yookassa_sbp', 'yookassa', 'stars', 'tribute']
    ]
    actual = [
        button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data in expected
    ]
    assert actual == expected
    assert markup.inline_keyboard[-1][0].callback_data == 'menu_balance'
