"""Draft: own invoice prose and closed-map recurring validation localization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.localization.texts import get_texts
from app.services.payment.stars import TelegramStarsMixin


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_stars_invoice_preserves_exact_price_payload_and_operator_description(language):
    service = TelegramStarsMixin()
    service.bot = SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://example.com/invoice'))
    service.stars_service = object()
    result = await service.create_stars_invoice(
        12345, 'Operator <description>&', payload='opaque_12345', stars_amount=17, language=language
    )
    assert result == 'https://example.com/invoice'
    args = service.bot.create_invoice_link.await_args.kwargs
    texts = get_texts(language)
    assert args['title'] == texts.t('STARS_INVOICE_TITLE')
    assert args['prices'][0].label == texts.t('STARS_INVOICE_LABEL')
    assert args['prices'][0].amount == 17
    assert args['description'] == 'Operator <description>& (≈17 ⭐)'
    assert args['payload'] == 'opaque_12345'
    assert args['provider_token'] == '' and args['currency'] == 'XTR'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_known_recurring_errors_and_unknown_passthrough(language):
    from app.handlers.subscription.tariff_purchase import _localize_recurrent_purchase_error

    texts = get_texts(language)
    for source, key in [
        ('Оформление через Lava недоступно для триальной подписки — оплатите с баланса', 'RECURRENT_UI_LAVA_TRIAL'),
        ('СБП-оформление недоступно для этой подписки — оплатите с баланса', 'RECURRENT_UI_SBP_UNAVAILABLE'),
        ('quote_changed', 'CISREC_ERROR_QUOTECHANGED'),
        ('quote_expired', 'RECURRENT_UI_QUOTE_EXPIRED'),
        ('device_consent_required', 'RECURRENT_UI_DEVICE_CONSENT'),
    ]:
        assert _localize_recurrent_purchase_error(ValueError(source), language) == texts.t(key)
    source = (
        'Продукт Lava стоит 123.45 ₽, а продление подписки — 234.56 ₽ за 30 дн. Заведите продукт с актуальной ценой.'
    )
    assert _localize_recurrent_purchase_error(ValueError(source), language) == texts.t(
        'RECURRENT_UI_LAVA_PRICE_MISMATCH'
    ).format(price='123.45', renewal='234.56', days='30')
    assert (
        _localize_recurrent_purchase_error(ValueError('provider <opaque>& details'), language)
        == 'provider <opaque>& details'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('kind', ['balance', 'wheel'])
async def test_cabinet_direct_invoices_keep_amount_and_payload(monkeypatch, language, kind):
    from contextlib import asynccontextmanager

    from app.cabinet.routes import balance, wheel
    from app.cabinet.schemas.balance import StarsInvoiceRequest
    from app.config import Settings, settings

    bot = SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://example.com/invoice'))

    @asynccontextmanager
    async def create_bot():
        yield bot

    monkeypatch.setattr(balance, 'create_bot', create_bot)
    monkeypatch.setattr('app.bot_factory.create_bot', create_bot)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(Settings, 'get_stars_rate', lambda self: 1)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(balance.time, 'time', lambda: 123456)
    user = SimpleNamespace(id=17, language=language)
    if kind == 'balance':
        await balance.create_stars_invoice(StarsInvoiceRequest(amount_kopeks=10000), user, None)
        expected_payload = 'balance_topup_17_10000_123456'
        expected_stars = 100
        title, label = 'STARS_INVOICE_TITLE', 'STARS_BALANCE_LABEL'
        description = get_texts(language).t('STARS_BALANCE_DESCRIPTION').format(amount=100, stars=100)
    else:
        monkeypatch.setattr(
            wheel,
            'get_or_create_wheel_config',
            AsyncMock(
                return_value=SimpleNamespace(
                    id=1, is_enabled=True, spin_cost_stars_enabled=True, spin_cost_stars=17, daily_spin_limit=3
                )
            ),
        )
        monkeypatch.setattr(wheel, 'get_user_spins_today', AsyncMock(return_value=0))
        monkeypatch.setattr(wheel, 'get_wheel_prizes', AsyncMock(return_value=[object()]))
        monkeypatch.setattr(
            'app.database.crud.subscription.get_subscription_by_user_id',
            AsyncMock(return_value=SimpleNamespace(is_active=True)),
        )
        await wheel.create_stars_invoice(user, None)
        expected_payload = 'wheel_spin_17_123456'
        expected_stars = 17
        title, label = 'STARS_WHEEL_TITLE', 'STARS_WHEEL_LABEL'
        description = get_texts(language).t('STARS_WHEEL_DESCRIPTION').format(stars=17)
    args = bot.create_invoice_link.await_args.kwargs
    assert args['payload'] == expected_payload
    assert args['currency'] == 'XTR' and args['provider_token'] == ''
    assert args['prices'][0].amount == expected_stars
    assert args['prices'][0].label == get_texts(language).t(label)
    assert args['title'] == get_texts(language).t(title)
    assert args['description'] == description


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('source', ['Источник: YooKassa', '<operator>&'])
async def test_receipt_source_uses_admin_locale_and_custom_labels_are_preserved(monkeypatch, language, source):
    from app.config import settings
    from app.services import nalogo_service

    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', language)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', 77)
    monkeypatch.setattr(nalogo_service, '_download_receipt_file', AsyncMock(return_value=None))
    monkeypatch.setattr(nalogo_service, '_send_receipt_email', AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock(), send_photo=AsyncMock())
    await nalogo_service.send_nalogo_receipt_notifications(
        bot,
        SimpleNamespace(get_receipt_print_url=lambda _: 'https://receipt.example/print'),
        'masked-1',
        12345,
        None,
        context_label=source,
    )
    args = bot.send_message.await_args.kwargs
    assert args['chat_id'] == '-100500' or args['chat_id'] == -100500
    assert args['message_thread_id'] == 77
    expected = (
        get_texts(language).t('NALOGO_SOURCE_YOOKASSA') if source.startswith('Источник') else '&lt;operator&gt;&amp;'
    )
    assert expected in args['text']
    assert args['reply_markup'].inline_keyboard[0][0].url == 'https://receipt.example/print'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('detail', ['NaloGO сервис не настроен', 'provider custom <error>'])
async def test_monitoring_known_error_localized_unknown_preserved(monkeypatch, language, detail):
    import inspect

    from app.handlers.admin import monitoring

    monkeypatch.setattr(monitoring.nalogo_queue_service, 'force_process', AsyncMock(return_value={'error': detail}))
    callback = SimpleNamespace(answer=AsyncMock())
    await inspect.unwrap(monitoring.nalogo_force_process_callback)(callback, SimpleNamespace(language=language))
    expected = get_texts(language).t('NALOGO_SERVICE_NOT_CONFIGURED') if detail.startswith('NaloGO') else detail
    assert callback.answer.await_args.args == (f'❌ {expected}',)
    assert callback.answer.await_args.kwargs == {'show_alert': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('provider', ['lava', 'sbp'])
async def test_recurring_handlers_show_localized_own_errors(monkeypatch, language, provider):
    import inspect

    from app.handlers.subscription import tariff_purchase

    user = SimpleNamespace(language=language)
    tariff = SimpleNamespace(id=7, is_active=True)
    callback = SimpleNamespace(data='purchase:7', answer=AsyncMock())
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    prefix = 'LAVA' if provider == 'lava' else 'SBP'
    source = get_texts('ru').t(f'RECURRENT_UI_{prefix}_TRIAL')
    purchase = AsyncMock(side_effect=ValueError(source))
    target = (
        'app.services.payment.lava.purchase_tariff_with_lava_recurring'
        if provider == 'lava'
        else 'app.services.payment.platega.purchase_tariff_with_sbp_recurring'
    )
    monkeypatch.setattr(target, purchase)
    handler = getattr(tariff_purchase, f'purchase_tariff_with_{provider}')
    await inspect.unwrap(handler)(callback, user, None, None)
    purchase.assert_awaited_once_with(None, user=user, tariff=tariff)
    callback.answer.assert_awaited_once_with(get_texts(language).t(f'RECURRENT_UI_{prefix}_TRIAL'), show_alert=True)


@pytest.mark.asyncio
async def test_stars_optional_language_uses_configured_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'fa')
    service = TelegramStarsMixin()
    service.bot = SimpleNamespace(create_invoice_link=AsyncMock(return_value='https://example.com/invoice'))
    service.stars_service = object()
    await service.create_stars_invoice(10000, 'custom description', stars_amount=17)
    assert service.bot.create_invoice_link.await_args.kwargs['title'] == get_texts('fa').t('STARS_INVOICE_TITLE')
