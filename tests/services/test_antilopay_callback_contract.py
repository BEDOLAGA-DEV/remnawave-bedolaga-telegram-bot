"""No credit for unverifiable Antilopay settlement fields."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.payment.antilopay import AntilopayPaymentMixin


@pytest.mark.parametrize(
    'changes',
    [
        {'original_amount': None},
        {'original_amount': 100.00},
        {'original_amount': True},
        {'original_amount': 'NaN'},
        {'currency': 'USD'},
        {'payment_id': 'other'},
        {'payment_id': None},
        {'status': 'UNKNOWN'},
    ],
)
async def test_invalid_settlement_never_credits(monkeypatch, changes):
    payment = SimpleNamespace(
        id=1,
        user_id=None,
        order_id='masked-order',
        amount_kopeks=10001,
        currency='RUB',
        is_paid=False,
        antilopay_payment_id='masked-invoice',
    )
    monkeypatch.setattr(
        'app.database.crud.antilopay.get_antilopay_payment_by_order_id', AsyncMock(return_value=payment)
    )
    monkeypatch.setattr(
        'app.database.crud.antilopay.get_antilopay_payment_by_id_for_update', AsyncMock(return_value=payment)
    )
    service = AntilopayPaymentMixin()
    service._finalize_antilopay_payment = AsyncMock(return_value=True)
    payload = {
        'type': 'payment',
        'order_id': 'masked-order',
        'payment_id': 'masked-invoice',
        'original_amount': 100.01,
        'currency': 'RUB',
        'status': 'SUCCESS',
        **changes,
    }
    assert not await service.process_antilopay_callback(AsyncMock(), payload)
    service._finalize_antilopay_payment.assert_not_awaited()
