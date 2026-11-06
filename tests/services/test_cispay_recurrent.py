from unittest.mock import AsyncMock

import pytest

from app.services.cispay_service import CisPayAPIError, CisPayConfig, CisPayService


REMOTE_ID = '00000000-0000-4000-8000-000000000001'


@pytest.mark.parametrize('days', [7, 10, 30, 359, 360])
def test_exact_period(days):
    from app.services.cispay_recurrent import resolve_cispay_period

    assert resolve_cispay_period(days, False) == days


@pytest.mark.parametrize('days,daily', [(1, True), (30, True), (6, False), (365, False), (True, False), (30.0, False)])
def test_unsupported_period(days, daily):
    from app.services.cispay_recurrent import resolve_cispay_period

    with pytest.raises(ValueError):
        resolve_cispay_period(days, daily)


async def test_create_uses_exact_contract_and_config(monkeypatch):
    service = CisPayService()
    request = AsyncMock(return_value={'id': REMOTE_ID, 'status': 'PENDING'})
    monkeypatch.setattr(service, '_request', request)
    config = CisPayConfig(shop_id='shop', api_key='test-key')
    await service.create_subscription(
        amount_kopeks=19901,
        interval_days=10,
        customer_id='user-7',
        purpose='Тариф · заказ A1',
        success_url=None,
        fail_url=None,
        config=config,
    )
    request.assert_awaited_once_with(
        'POST',
        '/subscriptions',
        json_payload={
            'amount': 19901,
            'currency': 'RUB',
            'payment_method': 'SBP',
            'interval_days': 10,
            'customer_id': 'user-7',
            'subscription_purpose': 'Тариф · заказ A1',
        },
        config=config,
    )


@pytest.mark.parametrize(
    'overrides',
    [
        {'amount_kopeks': True},
        {'amount_kopeks': 0},
        {'interval_days': 365},
        {'customer_id': 'x' * 41},
        {'customer_id': ' '},
        {'purpose': 'x' * 256},
    ],
)
async def test_invalid_request_never_reaches_provider(monkeypatch, overrides):
    service = CisPayService()
    request = AsyncMock()
    monkeypatch.setattr(service, '_request', request)
    args = dict(
        amount_kopeks=10000, interval_days=30, customer_id='user-7', purpose='Тариф', success_url=None, fail_url=None
    )
    with pytest.raises(ValueError):
        await service.create_subscription(**(args | overrides))
    request.assert_not_awaited()


async def test_creation_error_is_not_retried(monkeypatch):
    service = CisPayService()
    request = AsyncMock(side_effect=CisPayAPIError())
    monkeypatch.setattr(service, '_request', request)
    with pytest.raises(CisPayAPIError):
        await service.create_subscription(
            amount_kopeks=10000,
            interval_days=30,
            customer_id='user-7',
            purpose='Тариф',
            success_url=None,
            fail_url=None,
        )
    request.assert_awaited_once()


async def test_read_cancel_patch_and_lists(monkeypatch):
    service = CisPayService()
    request = AsyncMock(return_value={'id': REMOTE_ID})
    monkeypatch.setattr(service, '_request', request)
    await service.get_subscription(REMOTE_ID)
    request.assert_awaited_with('GET', f'/subscriptions/{REMOTE_ID}', config=None)
    await service.cancel_subscription(REMOTE_ID)
    request.assert_awaited_with('POST', f'/subscriptions/{REMOTE_ID}/cancel', config=None)
    await service.update_subscription(REMOTE_ID, interval_days=10, purpose='New')
    request.assert_awaited_with(
        'PATCH',
        f'/subscriptions/{REMOTE_ID}',
        json_payload={'interval_days': 10, 'subscription_purpose': 'New'},
        config=None,
    )
    await service.list_subscriptions(limit=100, offset=20, status='ACTIVE')
    request.assert_awaited_with(
        'GET', '/subscriptions', params={'limit': 100, 'offset': 20, 'status': 'ACTIVE'}, config=None
    )
    await service.list_transactions(limit=100, offset=20)
    request.assert_awaited_with('GET', '/transactions', params={'limit': 100, 'offset': 20}, config=None)


async def test_provider_paths_cannot_be_injected(monkeypatch):
    service = CisPayService()
    request = AsyncMock()
    monkeypatch.setattr(service, '_request', request)
    with pytest.raises(ValueError):
        await service.get_subscription('../payments')
    with pytest.raises(ValueError):
        await service.list_transactions(limit=101)
    request.assert_not_awaited()


@pytest.mark.parametrize('channel', ['telegram', 'cabinet'])
async def test_policy_uses_channel_and_db_configuration(monkeypatch, channel):
    from types import SimpleNamespace

    from app.config import settings
    from app.services import payment_method_config_service as policies
    from app.services.cispay_recurrent import get_cispay_recurrent_policy

    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', True)
    existing = AsyncMock(return_value=None)
    monkeypatch.setattr(policies, 'get_cispay_checkout_policy', existing)
    user = SimpleNamespace(status='active', restriction_topup=False)
    assert not (await get_cispay_recurrent_policy(None, user=user, channel=channel))['enabled']
    existing.assert_awaited_once_with(None, user, 'SBP', telegram_checkout=channel == 'telegram')
    existing.return_value = {'min_amount_kopeks': 100, 'max_amount_kopeks': 100000}
    assert (await get_cispay_recurrent_policy(None, user=user, channel=channel))['enabled']
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    existing.reset_mock()
    assert not (await get_cispay_recurrent_policy(None, user=user, channel=channel))['enabled']
    existing.assert_not_awaited()


@pytest.mark.parametrize(
    'url',
    ['javascript:alert(1)', 'https://user:secret@pay.example/x', 'http://pay.example/x', 'https://pay.example/\nfoo'],
)
def test_recurrent_checkout_rejects_unsafe_url(url):
    from app.services.cispay_recurrent_service import safe_payment_url

    with pytest.raises(ValueError):
        safe_payment_url(url)
