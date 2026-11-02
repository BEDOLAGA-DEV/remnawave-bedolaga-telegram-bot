"""Контракт изолированных API-вызовов рекуррентных планов CisPay.

Ни один тест не инициирует настоящий платёж и не обращается к внешней сети.
"""

from unittest.mock import AsyncMock

import pytest

from app.services.cispay_service import CisPayAPIError, CisPayService


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['CARD', 'SBP'])
async def test_create_subscription_sends_one_request_with_minor_units(monkeypatch, method):
    client = CisPayService()
    remote = {'id': 'sub-example', 'status': 'PENDING', 'payment_url': 'https://cispay.app/pay/example'}
    request = AsyncMock(return_value=remote)
    monkeypatch.setattr(client, '_request', request)

    result = await client.create_subscription(
        amount_kopeks=10001,
        interval_days=30,
        payment_method=method,
        customer_id='customer-example',
        subscription_purpose='Продление VPN',
        redirect_success_url='https://example.org/success',
    )

    assert result is remote
    request.assert_awaited_once()
    assert request.call_args.args == ('POST', '/subscriptions')
    body = request.call_args.kwargs['json_payload']
    assert body == {
        'amount': 10001,
        'currency': 'RUB',
        'interval_days': 30,
        'payment_method': method,
        'customer_id': 'customer-example',
        'subscription_purpose': 'Продление VPN',
        'redirect_success_url': 'https://example.org/success',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('amount_kopeks', True),
        ('amount_kopeks', 0),
        ('amount_kopeks', -1),
        ('amount_kopeks', 5000.5),
        ('interval_days', True),
        ('interval_days', 6),
        ('interval_days', 361),
        ('payment_method', 'CRYPTO'),
        ('payment_method', None),
        ('customer_id', ''),
        ('customer_id', ' '),
        ('subscription_purpose', ''),
        ('subscription_purpose', ' '),
        ('redirect_success_url', 'http://example.org'),
        ('redirect_fail_url', 'javascript:alert(1)'),
    ],
)
async def test_invalid_subscription_never_calls_provider(monkeypatch, field, value):
    client = CisPayService()
    request = AsyncMock(side_effect=AssertionError('Недопустимый запрос ушёл провайдеру'))
    monkeypatch.setattr(client, '_request', request)
    args = {
        'amount_kopeks': 10000,
        'interval_days': 30,
        'payment_method': 'CARD',
        'customer_id': 'customer-example',
        'subscription_purpose': 'VPN',
    }
    args[field] = value
    with pytest.raises(ValueError):
        await client.create_subscription(**args)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_subscription_minimum_never_calls_provider(monkeypatch):
    client = CisPayService()
    request = AsyncMock()
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(ValueError):
        await client.create_subscription(
            amount_kopeks=4999,
            interval_days=30,
            payment_method='CARD',
            customer_id='customer-example',
            subscription_purpose='VPN',
        )
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'reply',
    [
        {'id': 'other', 'status': 'PENDING', 'payment_url': 'https://cispay.app/pay/a'},
        {'id': 'sub-example', 'status': 'ACTIVE', 'payment_url': 'https://cispay.app/pay/a'},
        {'id': 'sub-example', 'status': 'PENDING', 'payment_url': 'http://cispay.app/pay/a'},
        {'id': 'sub-example', 'status': 'PENDING'},
    ],
)
async def test_create_subscription_fails_closed_on_unknown_remote_response(monkeypatch, reply):
    client = CisPayService()
    request = AsyncMock(return_value=reply)
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(CisPayAPIError):
        await client.create_subscription(
            amount_kopeks=10000,
            interval_days=30,
            payment_method='SBP',
            customer_id='customer-example',
            subscription_purpose='VPN',
        )
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_subscription_requires_matching_identity(monkeypatch):
    client = CisPayService()
    request = AsyncMock(return_value={'id': 'sub-example', 'status': 'ACTIVE'})
    monkeypatch.setattr(client, '_request', request)

    result = await client.get_subscription('sub-example')
    assert result['status'] == 'ACTIVE'
    request.assert_awaited_once_with('GET', '/subscriptions/sub-example', config=None)

    request.reset_mock()
    request.return_value = {'id': 'other', 'status': 'ACTIVE'}
    with pytest.raises(CisPayAPIError):
        await client.get_subscription('sub-example')


@pytest.mark.asyncio
async def test_cancel_subscription_requires_provider_confirmation(monkeypatch):
    client = CisPayService()
    request = AsyncMock(return_value={'id': 'sub-example', 'status': 'ACTIVE'})
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(CisPayAPIError):
        await client.cancel_subscription('sub-example')
    request.assert_awaited_once_with('POST', '/subscriptions/sub-example/cancel', config=None)

    request.reset_mock()
    request.return_value = {'id': 'another', 'status': 'CANCELLED'}
    with pytest.raises(CisPayAPIError):
        await client.cancel_subscription('sub-example')

    request.reset_mock()
    request.return_value = {'id': 'sub-example', 'status': 'CANCELLED'}
    assert await client.cancel_subscription('sub-example')
    request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_id', ['', '../payment', 'id/another', 'a?x=1', ' '])
async def test_subscription_id_rejects_path_injection_without_network(monkeypatch, invalid_id):
    client = CisPayService()
    request = AsyncMock()
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(ValueError):
        await client.get_subscription(invalid_id)
    with pytest.raises(ValueError):
        await client.cancel_subscription(invalid_id)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_subscription_does_not_retry_timeout(monkeypatch):
    client = CisPayService()
    request = AsyncMock(side_effect=CisPayAPIError())
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(CisPayAPIError):
        await client.create_subscription(
            amount_kopeks=10000,
            interval_days=30,
            payment_method='CARD',
            customer_id='customer-example',
            subscription_purpose='VPN',
        )
    request.assert_awaited_once()
