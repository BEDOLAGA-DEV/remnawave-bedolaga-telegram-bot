from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from tests.services.test_tariff_purchase_quote import make_tariff


@pytest.mark.parametrize(
    ('provider', 'expected', 'interval'),
    [('platega', 50001, 3), ('cashera', 50100, 'monthly'), ('lava', 50001, 'one_month')],
)
async def test_provider_terms_use_selected_devices_and_actual_cadence(monkeypatch, provider, expected, interval):
    from app.services import recurrent_purchase_selection as service

    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 90)
    tariff = make_tariff(period_prices={'30': 30001, '90': 90000}, lava_product_id='product-example')
    monkeypatch.setattr(
        service,
        'resolve_lava_purchase_product',
        AsyncMock(return_value={'price': '500.01', 'periodDays': 30, 'period': 'one_month'}),
        raising=False,
    )
    terms = await service.calculate_recurrent_purchase_terms(tariff, provider, 3)
    assert terms['amount_kopeks'] == expected
    assert terms['charge_days'] == 30
    assert terms['interval'] == interval


async def test_lava_product_cannot_undercharge_selected_devices(monkeypatch):
    from app.services import recurrent_purchase_selection as service

    monkeypatch.setattr(
        service,
        'resolve_lava_purchase_product',
        AsyncMock(return_value={'price': '300', 'periodDays': 30}),
        raising=False,
    )
    with pytest.raises(ValueError, match='Lava'):
        await service.calculate_recurrent_purchase_terms(make_tariff(lava_product_id='product-example'), 'lava', 3)


def test_provider_setup_rejects_changed_consent():
    from app.services import recurrent_purchase_selection as service

    sub = SimpleNamespace(
        _recurrent_purchase_terms={'provider': 'platega', 'amount_kopeks': 50000, 'charge_days': 30, 'interval': 3}
    )
    with pytest.raises(ValueError, match='quote_changed'):
        service.validate_recurrent_setup_terms(sub, 'platega', 50001, 30, 3)


@pytest.mark.parametrize(('provider', 'suffix'), [('platega', 'sbp'), ('lava', 'lava'), ('cashera', 'cashera')])
async def test_cabinet_forwards_device_consent(monkeypatch, provider, suffix):
    from importlib import import_module
    from uuid import uuid4

    import httpx
    from fastapi import FastAPI

    route = import_module(f'app.cabinet.routes.subscription_modules.{provider}_recurrent')
    module = import_module(f'app.services.payment.{provider}')
    from app.database.crud import tariff as tariff_crud

    monkeypatch.setattr(type(settings), f'is_{provider}_recurrent_enabled', lambda self: True)
    tariff = make_tariff(is_active=True)
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    called = AsyncMock(
        return_value={'status': 'pending', 'redirect_url': 'https://payment.example/pay', 'subscription_id': 3}
    )
    monkeypatch.setattr(module, f'purchase_tariff_with_{suffix}_recurring', called)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.get_current_cabinet_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[route.get_cabinet_db] = lambda: None
    quote, checkout = uuid4(), uuid4()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://cabinet.example') as client:
        response = await client.post(
            f'/{provider}-recurrent/purchase',
            params={'tariff_id': 7, 'selected_device_limit': 3, 'quote_id': str(quote), 'checkout_id': str(checkout)},
        )
    assert response.status_code == 200
    assert called.await_args.kwargs.get('selected_device_limit') == 3
    assert str(called.await_args.kwargs.get('quote_id')) == str(quote)
    assert str(called.await_args.kwargs.get('checkout_id')) == str(checkout)


async def test_lava_rejects_invoice_amount_changed_after_consent(monkeypatch):
    from tests.fixtures.sqlite_memory import memory_session
    from tests.services.test_lava_recurrent import TABLES, _agent, _seed

    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, subscription = await _seed(db)
        subscription._recurrent_purchase_terms = {
            'provider': 'lava',
            'amount_kopeks': 10000,
            'charge_days': 30,
            'interval': '30_days',
        }
        agent, remote = _agent(
            monkeypatch,
            subscribe_response={'data': {'subscriptionId': 'example', 'url': 'https://pay.example', 'amount': 110}},
        )
        with pytest.raises(ValueError, match='quote_changed'):
            await agent.create_lava_recurrent_subscription(
                db, user_id=user.id, subscription=subscription, tariff=tariff
            )
        remote.unsubscribe_recurrent.assert_awaited_once_with(subscription_id='example', order_id=None)
