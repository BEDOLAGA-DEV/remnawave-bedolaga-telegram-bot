"""Localized pricing keeps kopek conversion and reminder recipient selection intact."""

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.admin import pricing, subscriptions
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_price_input_stores_rounded_kopeks(monkeypatch, language):
    save = AsyncMock()
    monkeypatch.setattr(pricing.bot_configuration_service, 'set_value', save)
    monkeypatch.setattr(pricing, '_build_section', lambda *_: ('rendered', None))
    db = SimpleNamespace(commit=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={'pricing_key': 'PRICE_30_DAYS', 'pricing_section': 'periods', 'pricing_mode': 'price'}
        ),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(text='12,345 RUB', answer=AsyncMock(), delete=AsyncMock())
    await pricing.process_pricing_input(message, state, SimpleNamespace(language=language), db)
    save.assert_awaited_once_with(db, 'PRICE_30_DAYS', 1235)
    db.commit.assert_awaited_once()
    state.clear.assert_awaited_once()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_period_render_preserves_prices_and_days(monkeypatch, language):
    from app.config import PERIOD_PRICES

    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda self: [30, 90])
    monkeypatch.setitem(PERIOD_PRICES, 30, 1234)
    monkeypatch.setitem(PERIOD_PRICES, 90, 5678)
    texts = get_texts(language)
    items = pricing._get_period_items(language)
    assert items == [
        ('PRICE_30_DAYS', texts.t('ADMIN_PRICE_UI_000').format(value1=30), 1234),
        ('PRICE_90_DAYS', texts.t('ADMIN_PRICE_UI_000').format(value1=90), 5678),
    ]
    summary = pricing._build_period_summary(items, language, '-')
    assert summary == ', '.join(
        f'{days}{texts.t("ADMIN_PRICE_UI_002")}: {settings.format_price(amount)}'
        for days, amount in [(30, 1234), (90, 5678)]
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_setting_labels_and_choice_values_are_independent(language):
    texts = get_texts(language)
    text, keyboard = pricing._build_settings_section('core', language)
    entry = pricing.SETTING_ENTRY_BY_KEY['TRAFFIC_SELECTION_MODE']
    assert entry.label(language) == texts.t('ADMIN_PRICE_UI_025')
    assert entry.label(language) in text
    for choice in entry.choices:
        matching = [
            button
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
            == f'admin_pricing_choice:core:{pricing.SETTING_KEY_TO_TOKEN[entry.key]}:{choice.value}'
        ]
        assert len(matching) == 1
        assert choice.label(language) in matching[0].text


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('days_left', [0, 1])
@pytest.mark.asyncio
async def test_reminder_uses_recipient_language_and_admin_receipt(monkeypatch, language, days_left):
    from app.utils import notification_prefs

    user = SimpleNamespace(id=1, telegram_id=123, language=language)
    subscription = SimpleNamespace(user=user, user_id=1, end_date=datetime(2030, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(subscriptions, 'get_expiring_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(notification_prefs, 'is_subscription_expiry_enabled', lambda _: True)
    monkeypatch.setattr(subscriptions, 'local_days_until', lambda _: days_left)
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )
    admin_language = 'fa' if language != 'fa' else 'en'
    await inspect.unwrap(subscriptions.send_expiry_reminders)(callback, SimpleNamespace(language=admin_language), None)
    texts = get_texts(language)
    when = (
        texts.t('ADMIN_SUBSCRIPTIONS_UI_038')
        if days_left == 0
        else texts.t('ADMIN_SUBSCRIPTIONS_UI_039').format(value1=days_left)
    )
    callback.bot.send_message.assert_awaited_once_with(
        chat_id=123, text=texts.t('ADMIN_SUBSCRIPTIONS_UI_027').format(value1='', value2=when)
    )
    assert callback.message.edit_text.await_args.args[0] == get_texts(admin_language).t(
        'ADMIN_SUBSCRIPTIONS_UI_019'
    ).format(value1=1, value2=1)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_pricing_overview_reuses_one_locale_context(monkeypatch, language):
    calls = []

    def factory(code):
        calls.append(code)
        return get_texts(code)

    monkeypatch.setattr(pricing, 'get_texts', factory)
    text, keyboard = pricing._build_overview(language)
    assert text
    assert keyboard.inline_keyboard
    assert calls == [language]
