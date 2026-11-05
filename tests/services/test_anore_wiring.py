from app.config import settings
from app.services.payment_method_config_service import _get_method_defaults
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available


def test_anore_public_options_follow_live_settings(monkeypatch):
    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'shop-key-example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example-secret')
    monkeypatch.setattr(settings, 'ANORE_PAYMENT_METHODS', 'sbp,crypto')
    definition = _get_method_defaults()['anore']
    assert definition['is_configured']
    assert {option['id'] for option in definition['available_sub_options']} == {'sbp', 'crypto'}
    methods = {method['id'] for method in get_available_payment_methods()}
    assert {'anore_sbp', 'anore_crypto'} <= methods
    assert 'anore_card' not in methods
    assert is_payment_method_available('anore_sbp')
    assert not is_payment_method_available('anore_card')


def test_anore_disabled_hides_all_topup_methods(monkeypatch):
    monkeypatch.setattr(settings, 'ANORE_ENABLED', False)
    assert not any(method['id'].startswith('anore') for method in get_available_payment_methods())
    assert not is_payment_method_available('anore')
    assert not is_payment_method_available('anore_sbp')


async def test_guest_invoice_receives_metadata_before_provider_request(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.payment_service import PaymentService

    monkeypatch.setattr(type(settings), 'is_anore_enabled', lambda self: True)
    service = PaymentService()
    service.create_anore_payment = AsyncMock(
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
        payment_method='anore_crypto',
        description='Example',
        purchase_token='example-token',
        return_url='https://cabinet.example/result',
    )
    assert result['provider'] == 'anore'
    kwargs = service.create_anore_payment.await_args.kwargs
    assert kwargs['metadata'] == {'purpose': 'guest_purchase', 'purchase_token': 'example-token', 'source': 'landing'}
    assert kwargs['payment_method_type'] == 'crypto'
    assert kwargs['user_id'] is None
    db.commit.assert_not_awaited()


async def test_telegram_anore_amount_route_uses_its_database_session(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.handlers.balance import anore, main

    handler = AsyncMock()
    monkeypatch.setattr(anore, 'process_anore_payment_amount', handler)
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    from app.database import database

    monkeypatch.setattr(database, 'AsyncSessionLocal', factory)
    message, user, state = object(), SimpleNamespace(id=1), object()
    assert await main.route_payment_by_method(message, user, 10000, state, 'anore_sbp') is True
    handler.assert_awaited_once_with(message, user, session, 10000, state)


async def test_cabinet_anore_rejects_hidden_option_before_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest
    from fastapi import HTTPException

    from app.cabinet.routes import balance
    from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpRequest

    monkeypatch.setattr(type(settings), 'is_anore_enabled', lambda self: True)
    monkeypatch.setattr(
        balance,
        'get_payment_methods',
        AsyncMock(
            return_value=[
                PaymentMethodResponse(
                    id='anore',
                    name='Anore',
                    min_amount_kopeks=10000,
                    max_amount_kopeks=100000,
                    is_available=True,
                    options=[{'id': 'sbp'}],
                )
            ]
        ),
    )
    service = SimpleNamespace(create_anore_payment=AsyncMock())
    monkeypatch.setattr(balance, 'PaymentService', lambda: service)
    with pytest.raises(HTTPException) as exc:
        await balance.create_topup(
            TopUpRequest(amount_kopeks=10000, payment_method='anore', payment_option='card'),
            SimpleNamespace(id=1, telegram_id=None, restriction_topup=False),
            AsyncMock(),
        )
    assert exc.value.status_code == 400
    service.create_anore_payment.assert_not_awaited()


async def test_miniapp_status_checks_ownership_before_provider(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.database.crud import anore
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    monkeypatch.setattr(anore, 'get_anore_payment_by_id', AsyncMock(return_value=SimpleNamespace(user_id=2)))
    service = SimpleNamespace(check_anore_payment_status=AsyncMock())
    result = await miniapp._resolve_anore_payment_status(
        service,
        AsyncMock(),
        SimpleNamespace(id=1),
        MiniAppPaymentStatusQuery(method='anore', localPaymentId=7),
    )
    assert not result.is_paid
    service.check_anore_payment_status.assert_not_awaited()


def test_anore_webhook_registered_when_configured_but_disabled(monkeypatch):
    from types import SimpleNamespace

    from app.webserver.payments import create_payment_router

    monkeypatch.setattr(settings, 'ANORE_ENABLED', False)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'shop-key-example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'example-secret')
    router = create_payment_router(None, SimpleNamespace())
    assert any(route.path == settings.ANORE_WEBHOOK_PATH and 'POST' in route.methods for route in router.routes)


async def test_database_suboptions_all_disabled_hides_anore(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import payment_method_config_service as config_service

    monkeypatch.setattr(type(settings), 'is_anore_enabled', lambda self: True)
    config = SimpleNamespace(
        method_id='anore',
        is_enabled=True,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        display_name=None,
        min_amount_kopeks=None,
        max_amount_kopeks=None,
        sub_options=dict.fromkeys(('sbp', 'card', 'yoomoney', 'crypto'), False),
    )
    monkeypatch.setattr(config_service, 'get_all_configs', AsyncMock(return_value=[config]))
    assert await config_service.get_enabled_methods_for_user(AsyncMock(), None) == []


async def test_telegram_uses_effective_minimum_for_amount(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers.balance import anore

    monkeypatch.setattr(settings, 'ANORE_MIN_AMOUNT_KOPEKS', 10000)
    monkeypatch.setattr(
        anore,
        'get_anore_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 5000,
                'max_amount_kopeks': 20000,
                'options': [{'id': 'sbp'}],
            }
        ),
    )
    create = AsyncMock()
    monkeypatch.setattr(anore, '_create_anore_payment_and_respond', create)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'anore_sbp'}
    user = SimpleNamespace(id=1, language='ru', restriction_topup=False)
    await anore.process_anore_payment_amount(AsyncMock(), user, AsyncMock(), 5000, state)
    assert create.await_args.kwargs['amount_kopeks'] == 5000


async def test_miniapp_lists_and_creates_effective_anore_options(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentCreateRequest, MiniAppPaymentMethodsRequest

    user = SimpleNamespace(id=1, telegram_id=None, language='ru', restriction_topup=False)
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', AsyncMock(return_value=(user, None)))
    monkeypatch.setattr(
        miniapp,
        'get_anore_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 5000,
                'max_amount_kopeks': 20000,
                'options': [{'id': 'sbp', 'name': 'СБП'}],
            }
        ),
    )
    service = SimpleNamespace(
        create_anore_payment=AsyncMock(
            return_value={
                'payment_url': 'https://invalid.example/pay',
                'local_payment_id': 1,
                'payment_id': 'masked-1',
            }
        )
    )
    monkeypatch.setattr(miniapp, 'PaymentService', lambda: service)
    methods = await miniapp.get_payment_methods(MiniAppPaymentMethodsRequest(initData='example'), AsyncMock())
    anore = next(method for method in methods.methods if method.id == 'anore')
    assert anore.min_amount_kopeks == 5000
    assert [option.id for option in anore.options] == ['sbp']
    result = await miniapp.create_payment_link(
        MiniAppPaymentCreateRequest(
            initData='example',
            method='anore',
            amountKopeks=5000,
            option='sbp',
        ),
        AsyncMock(),
    )
    assert result.payment_url == 'https://invalid.example/pay'
    assert service.create_anore_payment.await_args.kwargs['payment_method_type'] == 'sbp'


async def test_miniapp_sandbox_paid_never_displays_paid(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.database.crud import anore
    from app.webapi.routes import miniapp
    from app.webapi.schemas.miniapp import MiniAppPaymentStatusQuery

    payment = SimpleNamespace(
        id=7,
        user_id=1,
        order_id='masked-order',
        anore_payment_id='masked-1',
        is_test=True,
        status='paid',
        amount_kopeks=5000,
        currency='RUB',
        paid_at=None,
        transaction_id=None,
    )
    monkeypatch.setattr(anore, 'get_anore_payment_by_id', AsyncMock(return_value=payment))
    service = SimpleNamespace(check_anore_payment_status=AsyncMock(return_value={'status': 'paid', 'is_paid': True}))
    result = await miniapp._resolve_anore_payment_status(
        service,
        AsyncMock(),
        SimpleNamespace(id=1),
        MiniAppPaymentStatusQuery(method='anore', localPaymentId=7),
    )
    assert result.status != 'paid'
    assert not result.is_paid
    assert 'Sandbox' in result.message


async def test_cabinet_latest_anore_queries_owner_scoped_model():
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
        await get_latest_payment_by_method('anore', SimpleNamespace(id=42), db)
    assert exc.value.status_code == 404
    sql = str(
        db.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    )
    assert 'anore_payments' in sql
    assert 'anore_payments.user_id = 42' in sql


def test_cabinet_sandbox_status_never_claims_credit():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.cabinet.routes.balance import _get_status_info, _record_to_response
    from app.database.models import PaymentMethod
    from app.services.payment_verification_service import PendingPayment

    record = PendingPayment(
        local_id=1,
        method=PaymentMethod.ANORE,
        identifier='masked-1',
        amount_kopeks=5000,
        status='paid',
        is_paid=True,
        created_at=datetime.now(UTC),
        expires_at=None,
        user=None,
        payment=SimpleNamespace(is_test=True, payment_url=None),
    )
    result = _record_to_response(record)
    assert not result.is_paid
    assert result.status == 'test_paid'
    assert 'баланс не пополнен' in result.status_text
    assert 'баланс не пополнен' in _get_status_info(record)[1]
