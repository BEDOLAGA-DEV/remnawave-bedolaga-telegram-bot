from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.balance import _get_status_info
from app.database.models import PaymentMethod


@pytest.mark.parametrize(
    ('status', 'label'),
    [
        ('Disputed', 'Платёж оспаривается'),
        ('CHARGEBACKED', 'Возврат по спору'),
        ('PARTIALLY_REFUNDED', 'Частичный возврат'),
        ('CONFIRMED', 'Оплачено'),
    ],
)
def test_platega_settlement_state_takes_priority_over_durable_credit(status: str, label: str) -> None:
    record = SimpleNamespace(method=PaymentMethod.PLATEGA, status=status, is_paid=True)
    assert _get_status_info(record)[1] == label


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['DISPUTED', 'CHARGEBACKED', 'PARTIALLY_REFUNDED'])
async def test_bot_status_check_reports_settlement_instead_of_new_success(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from app.handlers.balance import platega

    service = SimpleNamespace(
        get_platega_payment_status=AsyncMock(
            return_value={'status': status, 'is_paid': True, 'payment': SimpleNamespace(user=None)}
        )
    )
    monkeypatch.setattr(platega, 'PaymentService', lambda bot: service)
    monkeypatch.setattr(
        'app.database.crud.platega.get_platega_payment_by_id',
        AsyncMock(return_value=SimpleNamespace(user_id=1)),
    )
    callback = SimpleNamespace(data='check_platega_7', bot=None, answer=AsyncMock())
    await platega.check_platega_payment_status(callback, SimpleNamespace(), SimpleNamespace(id=1, language='ru'))
    assert status in callback.answer.await_args.args[0]
