from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.routes.subscription_modules import purchase
from app.config import settings
from tests.services.test_tariff_purchase_quote import make_tariff


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    tariff = make_tariff(is_active=True)
    user = SimpleNamespace(
        id=1, balance_kopeks=40000, restriction_subscription=False, get_primary_promo_group=lambda: None
    )
    db = AsyncMock()
    monkeypatch.setattr(purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(purchase, 'get_subscription_by_id_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.resolve_trial_conversion_candidate', AsyncMock(return_value=None)
    )
    cache = AsyncMock()
    cache.set.return_value = True
    monkeypatch.setattr('app.services.tariff_purchase_quote.cache', cache)
    app = FastAPI()
    app.include_router(purchase.router)
    app.dependency_overrides[purchase.get_current_cabinet_user] = lambda: user
    app.dependency_overrides[purchase.get_cabinet_db] = lambda: db
    return app, user, db


async def post(context, **changes):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context[0]), base_url='http://test') as client:
        return await client.post(
            '/purchase-tariff/preview', json={'tariff_id': 7, 'period_days': 30, 'selected_device_limit': 3} | changes
        )


async def test_preview_prices_without_mutating_wallet(context):
    response = await post(context)
    assert response.status_code == 200
    data = response.json()
    assert data['total_kopeks'] == 50000
    assert data['missing_kopeks'] == 10000
    assert data['device_selection']['effective_limit'] == 3
    assert data['purchase_kind'] == 'new'
    assert data['quote_id']
    assert context[1].balance_kopeks == 40000
    context[2].commit.assert_not_awaited()
    context[2].add.assert_not_called()


async def test_preview_rejects_foreign_subscription(context):
    response = await post(context, subscription_id=99)
    assert response.status_code == 404
    purchase.get_subscription_by_id_for_user.assert_awaited_once_with(context[2], 99, 1)


async def test_preview_rejects_restricted_user(context):
    context[1].restriction_subscription = True
    assert (await post(context)).status_code == 403


async def test_preview_accepts_configured_ten_thousand_days(context):
    assert (await post(context, period_days=10000)).status_code == 200


@pytest.mark.parametrize('devices', [True, 1.5, '3', 0, 2147483648])
async def test_preview_does_not_coerce_device_input(context, devices):
    assert (await post(context, selected_device_limit=devices)).status_code == 422


async def test_preview_rejects_inactive_tariff(context):
    purchase.get_tariff_by_id.return_value.is_active = False
    assert (await post(context)).status_code == 404


async def test_preview_rejects_unavailable_tariff(context):
    purchase.get_tariff_by_id.return_value.is_available_for_promo_group = lambda _: False
    assert (await post(context)).status_code == 403


async def test_preview_rejects_renewal_device_override(context, monkeypatch):
    subscription = SimpleNamespace(id=8, tariff_id=7, device_limit=2, is_trial=False, end_date=None)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=subscription)
    )
    assert (await post(context)).status_code == 409
    response = await post(context, selected_device_limit=None)
    assert response.status_code == 200
    assert response.json()['purchase_kind'] == 'paid_renewal'
    assert response.json()['device_selection']['effective_limit'] == 2


async def test_single_mode_still_checks_explicit_target_owner(context, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(purchase, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    assert (await post(context, subscription_id=99)).status_code == 404
    purchase.get_subscription_by_id_for_user.assert_awaited_once_with(context[2], 99, 1)


async def test_preview_cache_outage_does_not_allow_checkout(context, monkeypatch):
    cache = AsyncMock()
    cache.set.return_value = False
    monkeypatch.setattr('app.services.tariff_purchase_quote.cache', cache)
    response = await post(context)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'quote_unavailable'


async def test_single_mode_returns_existing_paid_limit(context, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    target = SimpleNamespace(id=8, tariff_id=7, device_limit=4, is_trial=False, end_date=None)
    monkeypatch.setattr(purchase, 'get_subscription_by_user_id', AsyncMock(return_value=target))
    response = await post(context, selected_device_limit=None)
    assert response.status_code == 200
    assert response.json()['device_selection']['effective_limit'] == 4
    assert not response.json()['device_selection']['selectable']
