"""Чек НПД по гостевой покупке формируется только для оплат через YooKassa.

В боте чеки NaloGO создаёт исключительно адаптер YooKassa. Хелпер гостевых
покупок способ оплаты не проверял, поэтому покупка с лендинга через CisPay
(и любой другой провайдер) тоже уходила в «Мой налог».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import guest_purchase_service as gps


def _purchase(payment_method: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        payment_id='pay-1',
        payment_method=payment_method,
        amount_kopeks=10000,
        receipt_uuid=None,
        receipt_created_at=None,
        is_gift=False,
        buyer=None,
        contact_type='email',
        contact_value='buyer@example.com',
        gift_recipient_type=None,
        gift_recipient_value=None,
    )


@pytest.fixture
def nalogo(monkeypatch) -> MagicMock:
    service = MagicMock()
    service.configured = True
    service.create_receipt = AsyncMock(return_value='receipt-uuid')

    monkeypatch.setattr(type(gps.settings), 'is_nalogo_enabled', lambda self: True)
    monkeypatch.setattr('app.services.nalogo_service.NaloGoService', lambda: service)
    monkeypatch.setattr('app.services.nalogo_service.send_nalogo_receipt_notifications', AsyncMock())
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['cispay', 'cispay_sbp', 'platega_2', 'cryptobot', 'telegram_stars', None])
async def test_non_yookassa_purchase_gets_no_receipt(nalogo, method):
    await gps._create_nalogo_receipt_for_purchase(
        AsyncMock(), _purchase(method), SimpleNamespace(telegram_id=1, email=None)
    )

    nalogo.create_receipt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['yookassa', 'yookassa_sbp'])
async def test_yookassa_purchase_gets_receipt(nalogo, method):
    purchase = _purchase(method)

    await gps._create_nalogo_receipt_for_purchase(AsyncMock(), purchase, SimpleNamespace(telegram_id=1, email=None))

    nalogo.create_receipt.assert_awaited_once()
    assert purchase.receipt_uuid == 'receipt-uuid'
