from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def api(monkeypatch):
    from app.cabinet.routes.subscription_modules import payhot_recurrent as route
    from app.services import (
        payhot_recurrent_checkout as checkout,
        payhot_recurrent_policy as policy,
        payhot_recurrent_service as service,
    )

    app = FastAPI()
    app.include_router(route.router)
    db = SimpleNamespace(rollback=AsyncMock())
    app.dependency_overrides[route.get_cabinet_db] = lambda: db
    app.dependency_overrides[route.get_current_cabinet_user] = lambda: SimpleNamespace(id=11)
    quote = AsyncMock(
        return_value={
            'quote_id': 'safe',
            'subscription_id': 7,
            'api_key_fingerprint': 'private',
            'source_terms': {'internal': True},
        }
    )
    state = AsyncMock(return_value={'status': 'ACTIVE'})
    cancel = AsyncMock(return_value={'status': 'CANCEL_PENDING'})
    monkeypatch.setattr(checkout, 'quote_payhot_purchase', quote)
    monkeypatch.setattr(checkout, 'get_payhot_agreement_state', state)
    monkeypatch.setattr(service, 'cancel_payhot_agreement', cancel)
    monkeypatch.setattr(
        policy,
        'get_payhot_recurrent_policy',
        AsyncMock(
            return_value={
                'enabled': False,
                'methods': [],
                'checkout_policy': {'secret': 'must-not-expose'},
            }
        ),
    )
    return app, db, quote, state, cancel


@pytest.mark.parametrize('days', [1, 6, 10000])
async def test_quote_custom_period_and_unlimited_devices(api, days):
    app, db, quote, _, _ = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/payhot-recurrent/quote',
            json={
                'tariff_id': None,
                'period_days': days,
                'device_limit': 0,
                'subscription_id': 7,
                'payment_method': 'sbp',
            },
        )
    assert response.status_code == 200
    assert response.json()['source_subscription_id'] == 7
    assert 'api_key_fingerprint' not in response.json()
    assert 'source_terms' not in response.json()
    assert quote.await_args.kwargs['user'].id == 11
    assert quote.await_args.kwargs['period_days'] == days
    assert quote.await_args.kwargs['device_limit'] == 0


@pytest.mark.parametrize(
    'body',
    [
        {'period_days': 0, 'payment_method': 'card'},
        {'period_days': True, 'payment_method': 'card'},
        {'period_days': 30, 'payment_method': 'crypto'},
        {'period_days': 30, 'payment_method': 'card', 'device_limit': -1},
    ],
)
async def test_invalid_requests_do_not_dispatch(api, body):
    app, _, quote, _, _ = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/payhot-recurrent/quote', json=body)
    assert response.status_code == 422
    quote.assert_not_awaited()


async def test_existing_status_and_cancel_available_when_disabled(api):
    app, db, _, state, cancel = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/payhot-recurrent', params={'subscription_id': 7})
        cancelled = await client.post('/payhot-recurrent/cancel', params={'subscription_id': 7})
    assert response.json() == {'status': 'ACTIVE', 'enabled': False, 'methods': []}
    assert cancelled.status_code == 200
    state.assert_awaited_once_with(db, user_id=11, subscription_id=7)
    cancel.assert_awaited_once_with(db, user_id=11, subscription_id=7, reason='customer')


@pytest.mark.parametrize(
    ('error', 'status', 'code'),
    [
        ('subscription_not_found', 404, 'subscription_not_found'),
        ('secret remote error', 409, 'payment_unavailable'),
    ],
)
async def test_safe_errors(api, error, status, code):
    app, _, _, state, _ = api
    state.side_effect = ValueError(error)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/payhot-recurrent', params={'subscription_id': 7})
    assert response.status_code == status
    assert response.json()['detail'] == {'code': code}


@pytest.mark.parametrize(
    ('method', 'suffix', 'permission'),
    [
        ('GET', '', 'users:read'),
        ('POST', '/cancel', 'users:subscription'),
    ],
)
@pytest.mark.parametrize('allowed', [True, False])
async def test_admin_permissions_and_owner_dispatch(monkeypatch, method, suffix, permission, allowed):
    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes import admin_users
    from app.services import payhot_recurrent_checkout as checkout, payhot_recurrent_service as service
    from app.services.permission_service import PermissionService

    app = FastAPI()
    app.include_router(admin_users.router)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    app.dependency_overrides[get_cabinet_db] = lambda: db
    app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=99)
    check = AsyncMock(return_value=(allowed, 'test'))
    audit = AsyncMock()
    monkeypatch.setattr(PermissionService, 'check_permission', check)
    monkeypatch.setattr(PermissionService, 'log_action', audit)
    state, cancel = AsyncMock(return_value={'status': 'ACTIVE'}), AsyncMock(return_value={'status': 'CANCEL_PENDING'})
    monkeypatch.setattr(checkout, 'get_payhot_agreement_state', state)
    monkeypatch.setattr(service, 'cancel_payhot_agreement', cancel)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.request(
            method, f'{admin_users.router.prefix}/11/subscriptions/7/payhot-recurring{suffix}'
        )
    assert response.status_code == (200 if allowed else 403)
    assert check.await_args.args[2] == permission
    assert audit.await_count == 1
    if allowed:
        selected = state if method == 'GET' else cancel
        assert selected.await_args.kwargs['user_id'] == 11
        assert selected.await_args.kwargs['subscription_id'] == 7
        if method == 'POST':
            assert selected.await_args.kwargs['reason'] == 'admin'
    else:
        state.assert_not_awaited()
        cancel.assert_not_awaited()


async def test_purchase_preserves_idempotency_ids_and_authenticated_owner(api, monkeypatch):
    from app.services import payhot_recurrent_checkout as checkout

    app, db, *_ = api
    purchase = AsyncMock(return_value={'status': 'CREATING', 'subscription_id': 7})
    monkeypatch.setattr(checkout, 'purchase_payhot_quote', purchase)
    body = {'quote_id': '00000000-0000-4000-8000-000000000001', 'checkout_id': '00000000-0000-4000-8000-000000000002'}
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/payhot-recurrent/purchase', json=body)
    assert response.status_code == 200
    assert purchase.await_args.kwargs['user'].id == 11
    assert purchase.await_args.kwargs['checkout_id'] == body['checkout_id']
    assert purchase.await_args.kwargs['channel'] == 'cabinet'


@pytest.mark.parametrize('tariffs', [False, True])
async def test_purchase_options_publish_policy_in_both_modes(monkeypatch, tariffs):
    from app.cabinet.routes.subscription_modules import payhot_recurrent, purchase

    monkeypatch.setattr(
        payhot_recurrent, 'payhot_availability', AsyncMock(return_value={'enabled': True, 'methods': ['sbp']})
    )
    monkeypatch.setattr(type(purchase.settings), 'is_tariffs_mode', lambda _: tariffs)
    monkeypatch.setattr(type(purchase.settings), 'is_multi_tariff_enabled', lambda _: False)
    monkeypatch.setattr(purchase, 'get_tariffs_for_user', AsyncMock(return_value=[]))
    monkeypatch.setattr(purchase, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(purchase, 'get_cispay_recurrent_policy', AsyncMock(return_value={'enabled': False}))
    monkeypatch.setattr(purchase.purchase_service, 'build_options', AsyncMock(return_value=SimpleNamespace(payload={})))
    result = await purchase.get_purchase_options(
        user=SimpleNamespace(id=11, balance_kopeks=0), db=None, subscription_id=None
    )
    assert result['payhot_recurrent_enabled'] is True
    assert result['payhot_recurrent_methods'] == ['sbp']
