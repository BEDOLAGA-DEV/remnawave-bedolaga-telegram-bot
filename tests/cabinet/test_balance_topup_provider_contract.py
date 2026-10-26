"""Cabinet top-up keeps provider limits and exposes Antilopay checkout."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cabinet.routes import balance
from app.cabinet.schemas.balance import PaymentMethodResponse
from app.config import settings


_real_get_payment_methods = balance.get_payment_methods

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def checkout(monkeypatch):
    user = SimpleNamespace(id=42, telegram_id=None, username=None, email='user@example.com', language='ru')
    service = SimpleNamespace(
        create_yookassa_payment=AsyncMock(
            return_value={'confirmation_url': 'https://pay.example/invoice', 'local_payment_id': 1}
        ),
        create_yookassa_sbp_payment=AsyncMock(
            return_value={'confirmation_url': 'https://pay.example/invoice', 'local_payment_id': 2}
        ),
        create_antilopay_payment=AsyncMock(
            return_value={'payment_url': 'https://pay.example/invoice', 'local_payment_id': 3}
        ),
    )
    methods = []

    async def get_methods(**kwargs):
        return methods

    monkeypatch.setattr(balance, 'get_payment_methods', get_methods)
    monkeypatch.setattr(balance, 'PaymentService', lambda: service)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example')
    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda self: True)
    application = FastAPI()
    application.include_router(balance.router)
    application.dependency_overrides[balance.get_current_cabinet_user] = lambda: user
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar=lambda: False)))
    application.dependency_overrides[balance.get_cabinet_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=application), base_url='https://cabinet.example') as client:
        yield client, methods, service


def _method(method_id, minimum=100):
    return PaymentMethodResponse(
        id=method_id,
        name=method_id,
        min_amount_kopeks=minimum,
        max_amount_kopeks=100_000,
        is_available=True,
        options=[{'id': option} for option in ('card', 'sbp', 'sberpay')] if method_id == 'antilopay' else None,
    )


@pytest.mark.parametrize('option', ['card', 'sbp'])
async def test_yookassa_one_ruble_uses_provider_minimum_and_cabinet_return(checkout, option):
    client, methods, service = checkout
    methods.append(_method('yookassa'))
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 100, 'payment_method': 'yookassa', 'payment_option': option}
    )
    assert response.status_code == 200, response.text
    assert response.json()['amount_kopeks'] == 100
    call = service.create_yookassa_sbp_payment if option == 'sbp' else service.create_yookassa_payment
    assert call.await_args.kwargs['amount_kopeks'] == 100
    assert call.await_args.kwargs['return_url'] == 'https://cabinet.example/balance/top-up/result?method=yookassa'


@pytest.mark.parametrize('provider', ['yookassa', 'wata', 'antilopay', 'cryptobot', 'pal24'])
async def test_lower_schema_bound_does_not_bypass_provider_minimum(checkout, provider):
    client, methods, service = checkout
    methods.append(_method(provider, minimum=2000))
    response = await client.post('/balance/topup', json={'amount_kopeks': 100, 'payment_method': provider})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Minimum amount is 20.00 RUB'
    assert not service.create_yookassa_payment.called
    assert not service.create_antilopay_payment.called


@pytest.mark.parametrize('amount', [0, -1])
async def test_nonpositive_topup_remains_invalid(checkout, amount):
    client, _, _ = checkout
    response = await client.post('/balance/topup', json={'amount_kopeks': amount, 'payment_method': 'yookassa'})
    assert response.status_code == 422


async def test_unavailable_method_rejected_before_payment_creation(checkout):
    client, _, service = checkout
    response = await client.post('/balance/topup', json={'amount_kopeks': 100, 'payment_method': 'yookassa'})
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid or unavailable payment method'
    assert not service.create_yookassa_payment.called


@pytest.mark.parametrize('option', ['card', 'sbp', 'sberpay'])
async def test_antilopay_cabinet_creates_checkout_with_user_and_return_url(checkout, option):
    client, methods, service = checkout
    methods.append(_method('antilopay', minimum=1000))
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 1000, 'payment_method': 'antilopay', 'payment_option': option}
    )
    assert response.status_code == 200, response.text
    assert response.json()['payment_url'] == 'https://pay.example/invoice'
    assert response.json()['payment_id'] == '3'
    kwargs = service.create_antilopay_payment.await_args.kwargs
    assert kwargs['email'] == 'user@example.com'
    assert kwargs['user_id'] == 42
    assert kwargs['payment_method_type'] == option
    assert kwargs['return_url'] == 'https://cabinet.example/balance/top-up/result?method=antilopay'


async def test_antilopay_unrecognized_submethod_is_rejected(checkout):
    client, methods, service = checkout
    methods.append(_method('antilopay', minimum=1000))
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 1000, 'payment_method': 'antilopay', 'payment_option': 'unknown'}
    )
    assert response.status_code == 422
    assert not service.create_antilopay_payment.called


async def test_antilopay_latest_and_details_respect_ownership(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.database.models import AntilopayPayment, User
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, (User.__table__, AntilopayPayment.__table__)) as db:
        user = User(id=42, email='user@example.com', status='active', language='ru', balance_kopeks=0)
        other = User(id=43, email='other@example.com', status='active', language='ru', balance_kopeks=0)
        now = datetime.now(UTC)
        own = AntilopayPayment(
            user_id=42,
            order_id='masked-own',
            amount_kopeks=1000,
            status='pending',
            payment_url='https://pay.example/own',
            created_at=now - timedelta(minutes=1),
        )
        foreign = AntilopayPayment(
            user_id=43,
            order_id='masked-foreign',
            amount_kopeks=2000,
            status='pending',
            payment_url='https://pay.example/foreign',
            created_at=now,
        )
        db.add_all([user, other, own, foreign])
        await db.commit()
        application = FastAPI()
        application.include_router(balance.router)
        application.dependency_overrides[balance.get_current_cabinet_user] = lambda: user
        application.dependency_overrides[balance.get_cabinet_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=application), base_url='https://cabinet.example') as client:
            response = await client.get('/balance/pending-payments/antilopay/latest')
            assert response.status_code == 200, response.text
            assert response.json()['id'] == own.id
            assert response.json()['payment_url'] == 'https://pay.example/own'
            assert response.json()['status_text'] == 'Ожидает оплаты'
            response = await client.get(f'/balance/pending-payments/antilopay/{own.id}')
            assert response.status_code == 200, response.text
            assert response.json()['identifier'] == 'masked-own'
            response = await client.get(f'/balance/pending-payments/antilopay/{foreign.id}')
            assert response.status_code == 403
            response = await client.post(f'/balance/pending-payments/antilopay/{foreign.id}/check')
            assert response.status_code == 403


async def test_antilopay_disabled_after_listing_remains_unavailable(checkout, monkeypatch):
    client, methods, service = checkout
    methods.append(_method('antilopay', minimum=1000))
    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda self: False)
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 1000, 'payment_method': 'antilopay', 'payment_option': 'card'}
    )
    assert response.status_code == 400
    assert not service.create_antilopay_payment.called


async def test_antilopay_provider_error_does_not_return_checkout(checkout):
    client, methods, service = checkout
    methods.append(_method('antilopay', minimum=1000))
    service.create_antilopay_payment.return_value = None
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 1000, 'payment_method': 'antilopay', 'payment_option': 'card'}
    )
    assert response.status_code == 500
    assert 'payment_url' not in response.json()


@pytest.mark.parametrize(
    ('sub_options', 'requested', 'expected_status', 'expected_option'),
    [
        ({'card': True, 'sbp': False, 'sberpay': False}, 'sbp', 422, None),
        ({'card': True, 'sbp': False, 'sberpay': False}, None, 200, 'card'),
        ({'card': False, 'sbp': False, 'sberpay': False}, None, 400, None),
        ({'card': False, 'sbp': False, 'sberpay': False}, 'card', 400, None),
        (None, 'card', 200, 'card'),
        (None, None, 422, None),
        ({}, 'sbp', 200, 'sbp'),
        ({}, None, 422, None),
        ({'sbp': False}, 'sbp', 422, None),
        ({'sbp': False}, 'card', 200, 'card'),
    ],
)
async def test_antilopay_respects_effective_database_options(
    checkout, monkeypatch, sub_options, requested, expected_status, expected_option
):
    from app.services import payment_method_config_service

    client, _, service = checkout
    config = SimpleNamespace(
        method_id='antilopay',
        is_enabled=True,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        display_name=None,
        min_amount_kopeks=1000,
        max_amount_kopeks=100000,
        sub_options=sub_options,
        description=None,
        quick_amounts=None,
        sort_order=0,
        open_url_direct=False,
    )
    monkeypatch.setattr(payment_method_config_service, 'get_all_configs', AsyncMock(return_value=[config]))
    monkeypatch.setattr(balance, 'get_payment_methods', _real_get_payment_methods)
    response = await client.post(
        '/balance/topup', json={'amount_kopeks': 1000, 'payment_method': 'antilopay', 'payment_option': requested}
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert service.create_antilopay_payment.await_args.kwargs['payment_method_type'] == expected_option
    else:
        service.create_antilopay_payment.assert_not_awaited()
