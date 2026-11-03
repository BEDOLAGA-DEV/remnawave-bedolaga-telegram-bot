"""Payment responses must honor the account language without changing payment flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import stars_payments
from app.handlers.balance import cashera, wata
from app.localization.texts import get_texts


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
async def test_restricted_topup_uses_account_language_and_escapes_reason(monkeypatch, language):
    user = SimpleNamespace(language=language, restriction_topup=True, restriction_reason='<private>')
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    monkeypatch.setattr(
        wata, 'settings', SimpleNamespace(get_support_contact_url=lambda: 'https://example.com/support')
    )
    await wata.start_wata_payment(callback, user, AsyncMock())
    text = callback.message.edit_text.await_args.args[0]
    expected = get_texts(language).t('USER_RESTRICTION_TOPUP_BLOCKED').format(reason='&lt;private&gt;')
    assert text == expected
    assert cashera._restriction_text(user) == expected
    button = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.text == get_texts(language).t('USER_RESTRICTION_APPEAL_BUTTON')


@pytest.mark.asyncio
async def test_trial_invalid_payload_uses_provided_texts():
    class Translations:
        def t(self, key, fallback=None):
            return f'localized:{key}'

    message = SimpleNamespace(answer=AsyncMock())
    result = await stars_payments._handle_trial_payment(
        message, AsyncMock(), SimpleNamespace(language='fa'), 10, 'trial', Translations()
    )
    assert result is False
    assert message.answer.await_args.args[0].startswith('localized:')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize(
    'provider', ['cashera', 'cryptobot', 'heleket', 'mulenpay', 'pal24', 'platega', 'wata', 'yookassa']
)
async def test_status_validation_uses_account_language_before_payment_lookup(language, provider):
    from importlib import import_module

    module = import_module(f'app.handlers.balance.{provider}')
    handler = getattr(module, f'check_{provider}_payment_status')
    callback = SimpleNamespace(
        data='payment_invalid',
        from_user=SimpleNamespace(language_code='ru'),
        answer=AsyncMock(),
    )
    db = AsyncMock()
    await handler(callback, db, SimpleNamespace(language=language))
    text = callback.answer.await_args.args[0]
    assert text
    assert (
        not any('\u0400' <= c <= '\u04ff' for c in text)
        if language != 'ua'
        else 'Неправиль' in text or 'Помилка' in text
    )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_handler_without_injected_user_uses_telegram_language():
    callback = SimpleNamespace(
        data='payment_invalid', from_user=SimpleNamespace(language_code='fa'), answer=AsyncMock()
    )
    await wata.check_wata_payment_status(callback, AsyncMock())
    assert callback.answer.await_args.args[0] == get_texts('fa').t('PAYMENT_UI_NOT_FOUND')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
async def test_guest_stars_invalid_payload_uses_account_language(language):
    message = SimpleNamespace(answer=AsyncMock())
    await stars_payments._handle_guest_purchase_payment(
        message, AsyncMock(), SimpleNamespace(language=language), 10, 'guest_purchase_!', 'masked-charge'
    )
    assert message.answer.await_args.args[0] == get_texts(language).t('STARS_GIFT_INVALID_PAYMENT')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('provider', ['wata', 'mulenpay', 'pal24'])
async def test_paid_status_formats_amount_and_labels_in_callers_language(monkeypatch, language, provider):
    from datetime import UTC, datetime
    from importlib import import_module

    module = import_module(f'app.handlers.balance.{provider}')
    payment = SimpleNamespace(
        id=7,
        user_id=42,
        status={'wata': 'Paid', 'mulenpay': 'success', 'pal24': 'SUCCESS'}[provider],
        is_paid=True,
        amount_kopeks=12345,
        payment_link_id='masked-link',
        mulen_payment_id='masked-payment',
        bill_id='masked-bill',
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        metadata_json={},
        payment_url=None,
        link_url=None,
        link_page_url=None,
    )
    lookup_name = f'get_{provider}_payment_by_local_id' if provider == 'mulenpay' else f'get_{provider}_payment_by_id'
    monkeypatch.setattr(import_module(f'app.database.crud.{provider}'), lookup_name, AsyncMock(return_value=payment))
    service = SimpleNamespace(**{f'get_{provider}_payment_status': AsyncMock(return_value={'payment': payment})})
    monkeypatch.setattr(module, 'PaymentService', lambda *_args: service)
    callback = SimpleNamespace(
        data=f'check_{provider}_7',
        bot=SimpleNamespace(),
        from_user=SimpleNamespace(language_code='ru'),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
    )
    await getattr(module, f'check_{provider}_payment_status')(
        callback, AsyncMock(), SimpleNamespace(id=42, language=language)
    )
    calls = (
        callback.message.answer.await_args_list
        + callback.message.edit_text.await_args_list
        + callback.answer.await_args_list
    )
    text = '\n'.join(c.args[0] for c in calls if c.args)
    assert get_texts(language).t('WATA_STATUS_PAID') in text
    assert '{value' not in text
    assert get_texts(language).t('PAYMENT_UI_AMOUNT').format(value1=module.settings.format_price(12345)) in text


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('method', ['kassa_ai', 'kassa_ai_sbp', 'kassa_ai_card', 'kassa_ai_sberpay'])
async def test_disabled_kassa_ai_submethod_uses_selected_language(monkeypatch, language, method):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers.balance import kassa_ai
    from app.localization.texts import get_texts

    config = dict(kassa_ai._KASSA_AI_METHOD_CONFIG[method], is_enabled=lambda: False)
    monkeypatch.setitem(kassa_ai._KASSA_AI_METHOD_CONFIG, method, config)
    callback = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(set_state=AsyncMock())
    await kassa_ai._start_kassa_ai_sub_topup(callback, SimpleNamespace(language=language), None, state, method)
    callback.answer.assert_awaited_once_with(get_texts(language).t(config['unavailable_key']), show_alert=True)
    state.set_state.assert_not_awaited()
