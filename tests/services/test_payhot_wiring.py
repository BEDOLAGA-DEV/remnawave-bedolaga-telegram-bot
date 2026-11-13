from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.payment_method_config_service import _get_method_defaults
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available


def test_payhot_public_options_follow_live_settings(monkeypatch):
    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'shop-key-example')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'example-secret')
    monkeypatch.setattr(settings, 'PAYHOT_PAYMENT_METHODS', 'sbp,crypto')
    definition = _get_method_defaults()['payhot']
    assert definition['is_configured']
    assert {option['id'] for option in definition['available_sub_options']} == {'sbp', 'crypto'}
    methods = {method['id'] for method in get_available_payment_methods()}
    assert {'payhot_sbp', 'payhot_crypto'} <= methods
    assert 'payhot_card' not in methods
    assert is_payment_method_available('payhot_sbp')
    assert not is_payment_method_available('payhot_card')


def test_payhot_disabled_hides_all_topup_methods(monkeypatch):
    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', False)
    assert not any(method['id'].startswith('payhot') for method in get_available_payment_methods())
    assert not is_payment_method_available('payhot')
    assert not is_payment_method_available('payhot_sbp')


async def test_guest_invoice_receives_metadata_before_provider_request(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.payment_service import PaymentService

    monkeypatch.setattr(type(settings), 'is_payhot_enabled', lambda self: True)
    service = PaymentService()
    service.create_payhot_payment = AsyncMock(
        return_value={
            'payment_url': 'https://pay.example/invoice',
            'order_id': 'example-order',
            'local_payment_id': 1,
        }
    )
    db = AsyncMock()
    result = await service.create_guest_payment(
        db,
        amount_kopeks=10000,
        payment_method='payhot_crypto',
        description='Example',
        purchase_token='example-token',
        return_url='https://cabinet.example/result',
    )
    assert result['provider'] == 'payhot'
    kwargs = service.create_payhot_payment.await_args.kwargs
    assert kwargs['metadata'] == {'purpose': 'guest_purchase', 'purchase_token': 'example-token', 'source': 'landing'}
    assert kwargs['payment_method_type'] == 'crypto'
    assert kwargs['user_id'] is None
    db.commit.assert_not_awaited()


async def test_telegram_payhot_amount_route_uses_its_database_session(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.handlers.balance import main, payhot

    handler = AsyncMock()
    monkeypatch.setattr(payhot, 'process_payhot_payment_amount', handler)
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    from app.database import database

    monkeypatch.setattr(database, 'AsyncSessionLocal', factory)
    message, user, state = object(), SimpleNamespace(id=1), object()
    assert await main.route_payment_by_method(message, user, 10000, state, 'payhot_sbp') is True
    handler.assert_awaited_once_with(message, user, session, 10000, state)


async def test_cabinet_payhot_rejects_hidden_option_before_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest
    from fastapi import HTTPException

    from app.cabinet.routes import balance
    from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpRequest

    monkeypatch.setattr(type(settings), 'is_payhot_enabled', lambda self: True)
    monkeypatch.setattr(
        balance,
        'get_payment_methods',
        AsyncMock(
            return_value=[
                PaymentMethodResponse(
                    id='payhot',
                    name='PayHot',
                    min_amount_kopeks=10000,
                    max_amount_kopeks=100000,
                    is_available=True,
                    options=[{'id': 'sbp'}],
                )
            ]
        ),
    )
    service = SimpleNamespace(create_payhot_payment=AsyncMock())
    monkeypatch.setattr(balance, 'PaymentService', lambda: service)
    with pytest.raises(HTTPException) as exc:
        await balance.create_topup(
            TopUpRequest(amount_kopeks=10000, payment_method='payhot', payment_option='card'),
            SimpleNamespace(id=1, telegram_id=None, restriction_topup=False),
            AsyncMock(),
        )
    assert exc.value.status_code == 400
    service.create_payhot_payment.assert_not_awaited()


async def test_miniapp_status_checks_ownership_before_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.database.crud import payhot
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    monkeypatch.setattr(payhot, 'get_payhot_payment_by_id', AsyncMock(return_value=SimpleNamespace(user_id=2)))
    service = SimpleNamespace(check_payhot_payment_status=AsyncMock())
    result = await miniapp._resolve_payhot_payment_status(
        service,
        AsyncMock(),
        SimpleNamespace(id=1),
        MiniAppPaymentStatusQuery(method='payhot', localPaymentId=7),
    )
    assert not result.is_paid
    service.check_payhot_payment_status.assert_not_awaited()


def test_payhot_webhook_registered_when_configured_but_disabled(monkeypatch):
    from types import SimpleNamespace

    from app.webserver.payments import create_payment_router

    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', False)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'shop-key-example')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'example-secret')
    router = create_payment_router(None, SimpleNamespace())
    assert any(route.path == settings.PAYHOT_WEBHOOK_PATH and 'POST' in route.methods for route in router.routes)


async def test_database_suboptions_all_disabled_hides_payhot(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import payment_method_config_service as config_service

    monkeypatch.setattr(type(settings), 'is_payhot_enabled', lambda self: True)
    config = SimpleNamespace(
        method_id='payhot',
        is_enabled=True,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        display_name=None,
        min_amount_kopeks=None,
        max_amount_kopeks=None,
        sub_options=dict.fromkeys(('sbp', 'card', 'sberpay', 'crypto', 'applepay', 'googlepay'), False),
    )
    monkeypatch.setattr(config_service, 'get_all_configs', AsyncMock(return_value=[config]))
    assert await config_service.get_enabled_methods_for_user(AsyncMock(), None) == []


async def test_telegram_uses_effective_minimum_for_amount(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers.balance import payhot

    monkeypatch.setattr(settings, 'PAYHOT_MIN_AMOUNT_KOPEKS', 10000)
    monkeypatch.setattr(
        payhot,
        'get_payhot_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 5000,
                'max_amount_kopeks': 20000,
                'options': [{'id': 'sbp'}],
            }
        ),
    )
    create = AsyncMock()
    monkeypatch.setattr(payhot, '_create_payhot_payment_and_respond', create)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'payhot_sbp'}
    user = SimpleNamespace(id=1, language='ru', restriction_topup=False)
    await payhot.process_payhot_payment_amount(AsyncMock(), user, AsyncMock(), 5000, state)
    assert create.await_args.kwargs['amount_kopeks'] == 5000


async def test_miniapp_lists_and_creates_effective_payhot_options(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentCreateRequest, MiniAppPaymentMethodsRequest

    user = SimpleNamespace(id=1, telegram_id=None, language='ru', restriction_topup=False)
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', AsyncMock(return_value=(user, None)))
    monkeypatch.setattr(
        miniapp,
        'get_payhot_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 5000,
                'max_amount_kopeks': 20000,
                'options': [{'id': 'sbp', 'name': 'СБП'}],
            }
        ),
    )
    service = SimpleNamespace(
        create_payhot_payment=AsyncMock(
            return_value={
                'payment_url': 'https://invalid.example/pay',
                'local_payment_id': 1,
                'payment_id': 'masked-1',
            }
        )
    )
    monkeypatch.setattr(miniapp, 'PaymentService', lambda: service)
    methods = await miniapp.get_payment_methods(MiniAppPaymentMethodsRequest(initData='example'), AsyncMock())
    payhot = next(method for method in methods.methods if method.id == 'payhot')
    assert payhot.min_amount_kopeks == 5000
    assert [option.id for option in payhot.options] == ['sbp']
    result = await miniapp.create_payment_link(
        MiniAppPaymentCreateRequest(
            initData='example',
            method='payhot',
            amountKopeks=5000,
            option='sbp',
        ),
        AsyncMock(),
    )
    assert result.payment_url == 'https://invalid.example/pay'
    assert service.create_payhot_payment.await_args.kwargs['payment_method_type'] == 'sbp'


async def test_miniapp_paid_invoice_displays_paid(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.database.crud import payhot
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    payment = SimpleNamespace(
        id=7,
        user_id=1,
        order_id='masked-order',
        payhot_payment_id='masked-1',
        status='paid',
        amount_kopeks=5000,
        currency='RUB',
        paid_at=None,
        transaction_id=None,
    )
    monkeypatch.setattr(payhot, 'get_payhot_payment_by_id', AsyncMock(return_value=payment))
    service = SimpleNamespace(check_payhot_payment_status=AsyncMock(return_value={'status': 'paid', 'is_paid': True}))
    result = await miniapp._resolve_payhot_payment_status(
        service,
        AsyncMock(),
        SimpleNamespace(id=1),
        MiniAppPaymentStatusQuery(method='payhot', localPaymentId=7),
    )
    assert result.is_paid
    assert result.message is None


async def test_cabinet_latest_payhot_queries_owner_scoped_model():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import pytest
    from fastapi import HTTPException
    from sqlalchemy.dialects import postgresql

    from app.cabinet.routes.balance import get_latest_payment_by_method

    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db = AsyncMock()
    db.execute.return_value = result
    with pytest.raises(HTTPException) as exc:
        await get_latest_payment_by_method('payhot', SimpleNamespace(id=42), db)
    assert exc.value.status_code == 404
    sql = str(
        db.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    )
    assert 'payhot_payments' in sql
    assert 'payhot_payments.user_id = 42' in sql


def test_cabinet_payhot_confirmed_payment_displays_credit():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.cabinet.routes.balance import _get_status_info, _record_to_response
    from app.database.models import PaymentMethod
    from app.services.payment_verification_service import PendingPayment

    record = PendingPayment(
        local_id=1,
        method=PaymentMethod.PAYHOT,
        identifier='masked-1',
        amount_kopeks=5000,
        status='paid',
        is_paid=True,
        created_at=datetime.now(UTC),
        expires_at=None,
        user=None,
        payment=SimpleNamespace(payment_url=None),
    )
    result = _record_to_response(record)
    assert result.is_paid
    assert result.status == 'paid'
    assert _get_status_info(record)[1]


@pytest.mark.parametrize(
    'field,value',
    [('user_type_filter', 'telegram'), ('first_topup_filter', 'first_only'), ('promo_group_filter_mode', 'selected')],
)
async def test_payhot_guest_policy_does_not_bypass_account_filters(monkeypatch, field, value):
    from types import SimpleNamespace

    from app.services import payment_method_config_service as module

    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'phk_v2_example')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'whsec_v2_example')
    config = SimpleNamespace(user_type_filter='all', first_topup_filter='any', promo_group_filter_mode='all')
    setattr(config, field, value)
    monkeypatch.setattr(module, 'get_config_by_method_id', AsyncMock(return_value=config))
    monkeypatch.setattr(module, 'get_enabled_methods_for_user', AsyncMock(return_value=[{'id': 'payhot'}]))
    assert await module.get_payhot_checkout_policy(AsyncMock(), None) is None


@pytest.mark.parametrize('flow', ['cabinet', 'miniapp', 'gift', 'landing'])
@pytest.mark.parametrize('phone', [None, '123', '+79000000000'])
def test_sberpay_requires_real_phone_across_entrypoints(flow, phone):
    from pydantic import ValidationError

    from app.cabinet.routes.landing import PurchaseRequest
    from app.cabinet.schemas.balance import TopUpRequest
    from app.cabinet.schemas.gift import GiftPurchaseRequest
    from app.webapi.schemas.miniapp import MiniAppPaymentCreateRequest

    schemas = {
        'cabinet': (TopUpRequest, {'payment_method': 'payhot', 'payment_option': 'sberpay', 'amount_kopeks': 10000}),
        'miniapp': (
            MiniAppPaymentCreateRequest,
            {'method': 'payhot', 'option': 'sberpay', 'amountKopeks': 10000, 'initData': 'test'},
        ),
        'gift': (
            GiftPurchaseRequest,
            {'payment_method': 'payhot_sberpay', 'payment_mode': 'gateway', 'tariff_id': 1, 'period_days': 30},
        ),
        'landing': (
            PurchaseRequest,
            {
                'payment_method': 'payhot_sberpay',
                'tariff_id': 1,
                'period_days': 30,
                'contact_type': 'email',
                'contact_value': 'buyer@example.com',
            },
        ),
    }
    schema, values = schemas[flow]
    if phone != '+79000000000':
        with pytest.raises(ValidationError):
            schema(**values, payment_phone=phone)
    else:
        assert schema(**values, payment_phone=phone).payment_phone == phone
