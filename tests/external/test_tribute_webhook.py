import pytest

from app.external.tribute import TributeService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _donation_payload(
    *,
    created_at: str = '2026-07-01T03:30:40.577825Z',
    sent_at: str = '2026-07-01T03:30:40.860372362Z',
    telegram_user_id: int = 123456789,
    trb_user_id: str = 'T-123456',
    amount: int = 125000,
) -> dict:
    return {
        'created_at': created_at,
        'name': 'new_donation',
        'payload': {
            'donation_request_id': 135873,
            'donation_name': 'Example VPN',
            'period': 'monthly',
            'amount': amount,
            'currency': 'rub',
            'anonymously': False,
            'web_app_link': 'https://t.me/tribute/app?startapp=example',
            'user_id': 123456,
            'trb_user_id': trb_user_id,
            'telegram_user_id': telegram_user_id,
            'telegram_username': 'example_user',
        },
        'sent_at': sent_at,
    }


@pytest.mark.anyio('asyncio')
async def test_tribute_donations_with_same_request_get_distinct_payment_ids() -> None:
    service = TributeService()

    first = await service.process_webhook(
        _donation_payload(
            created_at='2026-07-01T03:30:40.577825Z',
            telegram_user_id=111111111,
            trb_user_id='T-111111',
            amount=50000,
        )
    )
    second = await service.process_webhook(
        _donation_payload(
            created_at='2026-07-01T04:30:40.577825Z',
            telegram_user_id=222222222,
            trb_user_id='T-222222',
            amount=125000,
        )
    )

    assert first is not None
    assert second is not None
    assert first['payment_id'].startswith('trbt_evt_')
    assert second['payment_id'].startswith('trbt_evt_')
    assert first['payment_id'] != second['payment_id']
    assert first['external_id'] == f'donation_{first["payment_id"]}'
    assert second['external_id'] == f'donation_{second["payment_id"]}'


@pytest.mark.anyio('asyncio')
async def test_tribute_retry_keeps_same_payment_id_when_only_sent_at_changes() -> None:
    service = TributeService()

    first = await service.process_webhook(_donation_payload(sent_at='2026-07-01T03:30:40.860372362Z'))
    retry = await service.process_webhook(_donation_payload(sent_at='2026-07-01T03:35:40.860372362Z'))

    assert first is not None
    assert retry is not None
    assert first['payment_id'] == retry['payment_id']
    assert first['external_id'] == retry['external_id']
