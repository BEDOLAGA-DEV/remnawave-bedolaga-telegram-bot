from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import simple_subscription
from app.handlers.subscription import common, my_subscriptions
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_subscription_keyboard_uses_selected_language_and_preserves_actions(language):
    subscription = SimpleNamespace(actual_status='expired')
    keyboard = my_subscriptions._build_subscription_detail_keyboard(42, subscription, language)
    buttons = {button.callback_data: button.text for row in keyboard.inline_keyboard for button in row}
    texts = get_texts(language)
    assert buttons == {
        'se:42': texts.t('SUBSCRIPTION_UI_RENEW_BUTTON'),
        'sub_del:42': texts.t('SUBSCRIPTION_UI_DELETE_BUTTON'),
        'my_subscriptions': texts.t('SUBSCRIPTION_UI_BACK_TO_LIST'),
    }
    if language != 'ru':
        assert buttons['se:42'] != get_texts('ru').t('SUBSCRIPTION_UI_RENEW_BUTTON')


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_stale_classic_checkout_alert_uses_user_language(monkeypatch, language):
    monkeypatch.setattr(type(simple_subscription.settings), 'is_tariffs_mode', lambda self: True)
    callback = SimpleNamespace(answer=AsyncMock())
    assert await simple_subscription._reject_classic_checkout_in_tariff_mode(callback, language)
    callback.answer.assert_awaited_once_with(
        get_texts(language).t('SUBSCRIPTION_UI_OPEN_PURCHASE_OR_RENEW'), show_alert=True
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_payment_status_early_error_uses_callers_language(language):
    callback = SimpleNamespace(data='check_simple_wata_invalid', answer=AsyncMock())
    await simple_subscription.check_simple_wata_payment_status(
        callback, AsyncMock(), db_user=SimpleNamespace(language=language)
    )
    callback.answer.assert_awaited_once_with(
        get_texts(language).t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID'), show_alert=True
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_traffic_display_localizes_units_without_changing_amount(language):
    texts = get_texts(language)
    assert common.format_traffic_display(25, language=language) == texts.t('SUBSCRIPTION_UI_GB_VALUE').format(amount=25)
    assert common.format_traffic_display(0, language=language) == texts.t('SUBSCRIPTION_UI_UNLIMITED_LABEL')


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_crypto_details_preserve_amount_precision_and_markup(language):
    rendered = (
        get_texts(language)
        .t('SUBSCRIPTION_UI_CRYPTOBOT_DETAILS')
        .format(
            rubles=1234.4,
            dollars=12.345,
            asset='USDT',
            rate=99.876,
            invoice_id='masked-1',
            support='<a href="https://support.example">Support</a>',
        )
    )
    assert '1234 ₽' in rendered
    assert '12.35 USD' in rendered
    assert '99.88 ₽' in rendered
    assert 'masked-1...' in rendered
    assert '<a href="https://support.example">Support</a>' in rendered
    assert rendered.count('<b>') == rendered.count('</b>') == 2


@pytest.mark.parametrize('language', [None, 'ru', 'en', 'ua', 'zh', 'fa'])
async def test_countries_management_single_server_preserves_language(monkeypatch, language):
    from app.handlers.subscription import countries

    lookup = AsyncMock(return_value=[{'is_available': True}])
    monkeypatch.setattr(countries, '_get_available_countries', lookup)
    user = SimpleNamespace(language=language, promo_group_id=7) if language else None
    assert await countries._should_show_countries_management(user) is False
    lookup.assert_awaited_once_with(7 if user else None, language=language)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_country_charge_message_formats_amount_and_days(language):
    texts = get_texts(language)
    amount = texts.format_price(12345)
    rendered = texts.t('COUNTRY_CHANGES_CHARGED').format(amount=amount, days=17)
    assert amount in rendered
    assert '17' in rendered
    assert '{' not in rendered


@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
def test_tariff_purchase_units_use_selected_language(language):
    from types import SimpleNamespace

    from app.handlers.subscription.tariff_purchase import _period_button_text, format_tariff_info_for_user
    from app.utils.formatting import format_period, format_traffic

    tariff = SimpleNamespace(
        name='Operator label',
        description=None,
        traffic_limit_gb=25,
        device_limit=2,
        is_daily=False,
        highlight_period_days=None,
    )
    texts = get_texts(language)
    assert format_traffic(25, language=language) in format_tariff_info_for_user(tariff, language)
    assert _period_button_text(tariff, 10, '100 ₽', texts) == f'{format_period(10, language=language)} — 100 ₽'


def test_reset_devices_keyboard_builds_locale_once(monkeypatch):
    from unittest.mock import Mock

    factory = Mock(wraps=common.get_texts)
    monkeypatch.setattr(common, 'get_texts', factory)
    keyboard = common.get_reset_devices_confirm_keyboard('zh')
    factory.assert_called_once_with('zh')
    assert keyboard.inline_keyboard[0][0].text == get_texts('zh').t('SUBSCRIPTION_UI_RESET_ALL_DEVICES_CONFIRM')


async def test_simple_status_early_error_reuses_initial_locale(monkeypatch):
    from unittest.mock import Mock

    factory = Mock(wraps=simple_subscription.get_texts)
    monkeypatch.setattr(simple_subscription, 'get_texts', factory)
    callback = SimpleNamespace(data='check_simple_wata_invalid', answer=AsyncMock())
    await simple_subscription.check_simple_wata_payment_status(
        callback, AsyncMock(), db_user=SimpleNamespace(language='fa')
    )
    factory.assert_called_once_with('fa')
    callback.answer.assert_awaited_once_with(get_texts('fa').t('SUBSCRIPTION_UI_INVALID_PAYMENT_ID'), show_alert=True)
