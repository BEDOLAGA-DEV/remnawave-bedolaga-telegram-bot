from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.localization.texts import get_texts


CASES = [
    (
        'simple_subscription',
        'check_simple_pal24_payment_status',
        'pal24',
        'get_pal24_payment_by_id',
        'get_pal24_payment_status',
    ),
    (
        'simple_subscription',
        'check_simple_mulenpay_payment_status',
        'mulenpay',
        'get_mulenpay_payment_by_local_id',
        'get_mulenpay_payment_status',
    ),
    ('simple_subscription', 'check_simple_cryptobot_payment_status', 'cryptobot', 'get_cryptobot_payment_by_id', None),
    ('simple_subscription', 'check_simple_heleket_payment_status', 'heleket', 'get_heleket_payment_by_id', None),
    (
        'simple_subscription',
        'check_simple_wata_payment_status',
        'wata',
        'get_wata_payment_by_id',
        'get_wata_payment_status',
    ),
    ('balance.cryptobot', 'check_cryptobot_payment_status', 'cryptobot', 'get_cryptobot_payment_by_id', None),
    (
        'balance.heleket',
        'check_heleket_payment_status',
        'heleket',
        'get_heleket_payment_by_id',
        'sync_heleket_payment_status',
    ),
    ('balance.yookassa', 'check_yookassa_payment_status', 'yookassa', 'get_yookassa_payment_by_local_id', None),
    ('balance.main', 'handle_sbp_payment', 'yookassa', 'get_yookassa_payment_by_local_id', None),
    ('balance.pal24', 'check_pal24_payment_status', 'pal24', 'get_pal24_payment_by_id', 'get_pal24_payment_status'),
    (
        'balance.mulenpay',
        'check_mulenpay_payment_status',
        'mulenpay',
        'get_mulenpay_payment_by_local_id',
        'get_mulenpay_payment_status',
    ),
    ('balance.wata', 'check_wata_payment_status', 'wata', 'get_wata_payment_by_id', 'get_wata_payment_status'),
    (
        'balance.platega',
        'check_platega_payment_status',
        'platega',
        'get_platega_payment_by_id',
        'get_platega_payment_status',
    ),
    (
        'balance.cashera',
        'check_cashera_payment_status',
        'cashera',
        'get_cashera_payment_by_id',
        'get_cashera_payment_status',
    ),
]


def setup_case(monkeypatch, case, owner=1, exists=True):
    module_name, handler_name, provider, lookup_name, service_method = case
    module = import_module(f'app.handlers.{module_name}')
    payment = SimpleNamespace(
        id=99,
        user_id=owner,
        status={
            'pal24': 'NEW',
            'mulenpay': 'created',
            'wata': 'Opened',
            'cryptobot': 'active',
            'heleket': 'check',
            'yookassa': 'pending',
            'platega': 'PENDING',
            'cashera': 'pending',
        }[provider],
        is_paid=False,
        is_pending=True,
        is_expired=False,
        is_succeeded=False,
        is_failed=False,
        amount=10,
        amount_kopeks=10000,
        asset='USDT',
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        invoice_id='masked-invoice',
        uuid='masked-uuid',
        bill_id='masked-bill',
        yookassa_payment_id='masked-yoo',
        mulen_payment_id='masked-mulen',
        payment_link_id='masked-wata',
        metadata_json='{"confirmation_token": "masked-token"}',
        link_url='https://payment.example',
        link_page_url=None,
        payment_url='https://payment.example',
        payer_amount=None,
        payer_currency=None,
    )
    lookup = AsyncMock(return_value=payment if exists else None)
    monkeypatch.setattr(import_module(f'app.database.crud.{provider}'), lookup_name, lookup)
    service = SimpleNamespace()
    if service_method:
        result = (
            payment
            if service_method.startswith('sync_')
            else {'payment': payment, 'status': payment.status, 'is_paid': False}
        )
        setattr(service, service_method, AsyncMock(return_value=result))
    factory = Mock(return_value=service)
    if hasattr(module, 'PaymentService'):
        monkeypatch.setattr(module, 'PaymentService', factory)
    monkeypatch.setattr(
        'app.services.payment_service.get_user_by_id', AsyncMock(return_value=SimpleNamespace(language='en'))
    )
    callback = SimpleNamespace(
        data=f'check_{provider}_99',
        bot=SimpleNamespace(),
        from_user=SimpleNamespace(id=7001, language_code='en'),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    return getattr(module, handler_name), callback, lookup, factory, service


@pytest.mark.parametrize('case', CASES, ids=[c[1] for c in CASES])
@pytest.mark.parametrize('denied', ['foreign', 'missing', 'no_caller'])
async def test_denied_payment_status_never_reaches_provider_or_renders_details(monkeypatch, case, denied):
    handler, callback, lookup, factory, _service = setup_case(
        monkeypatch, case, owner=2 if denied == 'foreign' else 1, exists=denied != 'missing'
    )
    caller = None if denied == 'no_caller' else SimpleNamespace(id=1, language='en')
    await handler(callback, AsyncMock(), db_user=caller)
    # Missing caller falls back to default language in simple checkout and to Telegram language in balance.
    language = 'ru' if denied == 'no_caller' and case[0] == 'simple_subscription' else 'en'
    callback.answer.assert_awaited_once_with(get_texts(language).t('PAYMENT_UI_NOT_FOUND'), show_alert=True)
    callback.message.edit_text.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    factory.assert_not_called()
    if denied == 'no_caller':
        lookup.assert_not_awaited()


@pytest.mark.parametrize('case', CASES, ids=[c[1] for c in CASES])
async def test_owned_payment_preserves_provider_status_flow(monkeypatch, case):
    handler, callback, lookup, factory, service = setup_case(monkeypatch, case)
    db = AsyncMock()
    await handler(callback, db, db_user=SimpleNamespace(id=1, language='en'))
    lookup.assert_awaited_once_with(db, 99)
    service_method = case[4]
    if service_method:
        factory.assert_called_once_with(callback.bot)
        method = getattr(service, service_method)
        if service_method.startswith('sync_'):
            method.assert_awaited_once_with(db, local_payment_id=99)
        else:
            method.assert_awaited_once_with(db, 99)
    else:
        factory.assert_not_called()
    rendered = (
        callback.answer.await_args_list
        + callback.message.edit_text.await_args_list
        + callback.message.answer.await_args_list
    )
    assert rendered
    assert not any(call.args and call.args[0] == get_texts('en').t('PAYMENT_UI_NOT_FOUND') for call in rendered)
    assert not any(call.args and call.args[0] == get_texts('en').t('PAYMENT_UI_STATUS_ERROR') for call in rendered)
