"""Promo and coupon UI translations keep custom operator copy and payloads intact."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.handlers.admin import coupons, promo_offers


def _batch():
    return SimpleNamespace(
        id=7,
        name='Partner <name>',
        tariff=SimpleNamespace(name='Plan <name>'),
        period_days=30,
        coupons_total=5,
        wholesale_price_kopeks=12300,
        max_per_user=1,
        valid_until=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_revoked=False,
    )


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_coupon_card_preserves_operator_names_and_buttons(language):
    batch = _batch()
    card = coupons._format_batch_card(batch, {}, language=language)
    assert 'Partner &lt;name&gt;' in card and 'Plan &lt;name&gt;' in card
    keyboard = coupons._batch_card_keyboard(batch, {}, language=language)
    assert [b.callback_data for row in keyboard.inline_keyboard for b in row] == [
        'admin_coupon_delete_7',
        'admin_coupons',
    ]


def test_custom_promo_text_is_not_translated():
    template = SimpleNamespace(
        id=1,
        offer_type='extend_discount',
        message_text='Custom {discount_percent}% {server_name}',
        discount_percent=20,
        valid_hours=24,
        test_duration_hours=None,
        active_discount_hours=24,
    )
    assert promo_offers._render_template_text(template, 'fa', server_name='Operator') == 'Custom 20% Operator'


def _stock_template():
    source = next(t for t in promo_offers.DEFAULT_TEMPLATES if t['offer_type'] == 'extend_discount')
    return SimpleNamespace(
        **{
            **source,
            'id': 9,
            'discount_percent': 20,
            'valid_hours': 24,
            'active_discount_hours': 12,
            'test_duration_hours': None,
            'test_squad_uuids': [],
        }
    )


@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
def test_stock_templates_and_segments_are_localized(language):
    template = _stock_template()
    assert promo_offers._render_template_text(template, language) != promo_offers._render_template_text(template, 'ru')
    assert promo_offers._template_display_field(template, 'button_text', language) != template.button_text
    keyboard = promo_offers._build_send_keyboard(template, language)
    assert keyboard.inline_keyboard[0][0].text != '🟢 Активные платные'
    assert keyboard.inline_keyboard[0][0].callback_data == 'promo_offer_send_9_paid_active'
    template.button_text = 'Operator button'
    assert promo_offers._template_display_field(template, 'button_text', language) == 'Operator button'
    template.message_text = 'Custom {server_name}'
    assert promo_offers._render_template_text(template, language, server_name='<server>') == 'Custom &lt;server&gt;'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa', None])
@pytest.mark.parametrize('email_only', [False, True])
async def test_recipient_language_and_offer_parameters(monkeypatch, language, email_only):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import InlineKeyboardButton

    from app.database import database
    from app.services import promo_offer_email
    from app.utils import notification_prefs

    template = _stock_template()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(database, 'AsyncSessionLocal', lambda: session)
    upsert = AsyncMock(return_value=SimpleNamespace(id=81))
    monkeypatch.setattr(promo_offers, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(
        promo_offers,
        'settings',
        SimpleNamespace(
            is_notifications_enabled=lambda: True, is_multi_tariff_enabled=lambda: False, DEFAULT_LANGUAGE='en'
        ),
    )
    monkeypatch.setattr(notification_prefs, 'is_promo_offers_enabled', lambda user: True)
    monkeypatch.setattr(
        promo_offers, 'build_miniapp_or_callback_button', lambda **kwargs: InlineKeyboardButton(**kwargs)
    )
    email_send = AsyncMock(return_value=True)
    monkeypatch.setattr(promo_offer_email, 'send_promo_offer_email', email_send)
    user = SimpleNamespace(
        id=12,
        telegram_id=None if email_only else 123,
        language=language,
        subscription=SimpleNamespace(id=32),
        email='recipient@example.com',
        email_verified=True,
        first_name='Recipient',
        username='recipient',
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await promo_offers._send_offer_to_users(
        bot, template, SimpleNamespace(language='ru'), None, [user], squad_name=None, effect_type='percent'
    ) == (1, 0)
    effective_language = language or 'en'
    expected_text = promo_offers._render_template_text(template, effective_language)
    if email_only:
        assert email_send.await_args.kwargs['language'] == effective_language
        assert email_send.await_args.kwargs['message_text'] == expected_text
        bot.send_message.assert_not_awaited()
    else:
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs['text'] == expected_text
        button = kwargs['reply_markup'].inline_keyboard[0][0]
        assert button.text == promo_offers._template_display_field(template, 'button_text', effective_language)
        assert button.callback_data == 'claim_discount_81'
    assert upsert.await_args.kwargs == dict(
        user_id=12,
        subscription_id=32,
        notification_type='promo_template_9',
        discount_percent=20,
        bonus_amount_kopeks=0,
        valid_hours=24,
        effect_type='percent',
        extra_data=dict(
            template_id=9,
            offer_type='extend_discount',
            test_duration_hours=None,
            test_squad_uuids=[],
            active_discount_hours=12,
        ),
    )
