"""Tribute «цифровой товар» — разбор вебхуков `new_digital_product` / `digital_product_refunded`.

Схема payload — OpenAPI Tribute (https://tribute.tg/api/v1/openapi/en): `purchase_id` — идемпотентность,
`amount` — наименьшие единицы валюты, `telegram_user_id` нет у покупателя по email (`trb_user_id` = `W-…`).
"""

import pytest

from app.external.tribute import DIGITAL_PURCHASE, DIGITAL_REFUND, TributeService, digital_product_event_kind


def _digital_event(name='new_digital_product', **overrides):
    payload = {
        'product_id': 456,
        'product_name': 'VPN 1 month',
        'amount': 30000,
        'currency': 'rub',
        'trb_user_id': 'T-31326',
        'telegram_user_id': 12321321,
        'telegram_username': 'durov',
        'purchase_id': 78901,
        'transaction_id': 234567,
        'purchase_created_at': '2026-11-09T10:00:00.1Z',
    }
    payload.update(overrides)
    return {'name': name, 'created_at': '2026-11-09T10:00:00.1Z', 'sent_at': '2026-11-09T10:00:01Z', 'payload': payload}


@pytest.mark.parametrize(
    ('name', 'kind'),
    [
        ('new_digital_product', DIGITAL_PURCHASE),
        ('newDigitalProduct', DIGITAL_PURCHASE),
        ('digital_product_refunded', DIGITAL_REFUND),
        ('digitalProductRefund', DIGITAL_REFUND),
        ('new_donation', None),
        ('cancelled_subscription', None),
        (None, None),
    ],
)
def test_event_kind(name, kind):
    assert digital_product_event_kind(name) == kind


@pytest.mark.asyncio
async def test_purchase_is_paid_with_purchase_id_key():
    result = await TributeService().process_webhook(_digital_event())

    assert result['event_type'] == 'payment'
    assert result['status'] == 'paid'
    assert result['payment_id'] == 'digital_78901'
    assert result['user_id'] == 12321321
    assert result['amount_kopeks'] == 30000
    assert result['currency'] == 'rub'
    assert result['external_id'] == 'donation_digital_78901'


@pytest.mark.asyncio
async def test_purchase_camel_case_name_is_accepted():
    result = await TributeService().process_webhook(_digital_event(name='newDigitalProduct'))

    assert result['status'] == 'paid'
    assert result['payment_id'] == 'digital_78901'


@pytest.mark.asyncio
async def test_same_purchase_has_same_key_on_retry_and_other_purchase_differs():
    service = TributeService()
    first = await service.process_webhook(_digital_event())
    retry = await service.process_webhook(_digital_event())
    other = await service.process_webhook(_digital_event(purchase_id=78902))

    assert first['payment_id'] == retry['payment_id']
    assert other['payment_id'] != first['payment_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['digital_product_refunded', 'digitalProductRefund'])
async def test_refund_has_same_payment_id_as_purchase(name):
    purchase = await TributeService().process_webhook(_digital_event())
    refund = await TributeService().process_webhook(_digital_event(name=name, refund_reason='telegram_refund'))

    assert refund['event_type'] == 'refund'
    assert refund['status'] == 'refunded'
    assert refund['payment_id'] == purchase['payment_id']
    assert refund['amount_kopeks'] == 30000


@pytest.mark.asyncio
@pytest.mark.parametrize('currency', ['usd', 'eur', 'USD', '', None])
async def test_non_rub_is_not_paid(currency):
    result = await TributeService().process_webhook(_digital_event(currency=currency))

    assert result['status'] == 'unsupported_currency'


@pytest.mark.asyncio
async def test_rub_in_upper_case_is_accepted():
    result = await TributeService().process_webhook(_digital_event(currency='RUB'))

    assert result['status'] == 'paid'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'overrides',
    [
        {'purchase_id': None},
        {'telegram_user_id': None, 'trb_user_id': 'W-15408'},  # покупатель по email
        {'telegram_user_id': 'abc'},
        {'amount': 0},
        {'amount': -5},
        {'amount': 'много'},
    ],
)
async def test_unusable_digital_events_return_none(overrides):
    assert await TributeService().process_webhook(_digital_event(**overrides)) is None


@pytest.mark.asyncio
async def test_donation_parsing_is_unchanged():
    event = {
        'name': 'new_donation',
        'created_at': '2026-11-09T10:00:00Z',
        'payload': {'donation_request_id': 123, 'amount': 10000, 'currency': 'rub', 'telegram_user_id': 42},
    }

    result = await TributeService().process_webhook(event)

    assert result['status'] == 'paid'
    assert result['payment_id'] == '123_42_10000_2026-11-09T10:00:00Z'
    assert result['user_id'] == 42


@pytest.mark.asyncio
async def test_unknown_event_is_still_unknown():
    event = {'name': 'physical_order_created', 'payload': {'amount': 1, 'telegram_user_id': 42}}

    result = await TributeService().process_webhook(event)

    assert result['status'] == 'unknown'
