from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod, TransactionType


@pytest.mark.parametrize('applied', [False, True])
async def test_receipt_uses_existing_unified_delivery_and_exact_principal(monkeypatch, applied):
    from app.cabinet.routes.websocket import cabinet_ws_manager
    from app.database.crud import transaction
    from app.services.notification_delivery_service import notification_delivery_service
    from app.services.payhot_recurrent_notifications import emit_payhot_receipt

    event, ws, renewal, balance = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(transaction, 'emit_transaction_side_effects', event)
    monkeypatch.setattr(cabinet_ws_manager, 'send_to_user', ws)
    monkeypatch.setattr(notification_delivery_service, 'notify_autopay_success', renewal)
    monkeypatch.setattr(notification_delivery_service, 'notify_balance_topup', balance)
    db = AsyncMock()
    bot = object()
    tx = SimpleNamespace(
        id=4,
        user_id=1,
        amount_kopeks=-12300 if applied else 12300,
        external_id='receipt-example',
        description='HOT Pay',
    )
    user = SimpleNamespace(id=1, balance_kopeks=15000)
    target = SimpleNamespace(id=2, end_date=datetime(2030, 1, 1, tzinfo=UTC))
    agreement = SimpleNamespace(id=3, owner_id=1, subscription_id=2, status='ACTIVE')
    await emit_payhot_receipt(db, tx, user, target, agreement, applied=applied, bot=bot)
    assert event.await_args.kwargs['type'] == (
        TransactionType.SUBSCRIPTION_PAYMENT if applied else TransactionType.DEPOSIT
    )
    assert event.await_args.kwargs['payment_method'] == PaymentMethod.PAYHOT
    assert event.await_args.kwargs['amount_kopeks'] == 12300
    ws.assert_awaited_once()
    if applied:
        renewal.assert_awaited_once_with(user, 12300, target.end_date, bot=bot)
        balance.assert_not_called()
    else:
        balance.assert_awaited_once_with(user, 12300, user.balance_kopeks, bot=bot)
        renewal.assert_not_called()
    db.commit.assert_not_called()


async def test_side_effect_failures_do_not_hide_other_delivery(monkeypatch):
    from app.cabinet.routes.websocket import cabinet_ws_manager
    from app.database.crud import transaction
    from app.services.notification_delivery_service import notification_delivery_service
    from app.services.payhot_recurrent_notifications import emit_payhot_receipt

    monkeypatch.setattr(transaction, 'emit_transaction_side_effects', AsyncMock(side_effect=RuntimeError('masked')))
    monkeypatch.setattr(cabinet_ws_manager, 'send_to_user', AsyncMock(side_effect=RuntimeError('masked')))
    notify = AsyncMock()
    monkeypatch.setattr(notification_delivery_service, 'notify_balance_topup', notify)
    tx = SimpleNamespace(id=4, user_id=1, amount_kopeks=123, external_id='receipt-example', description='HOT Pay')
    user = SimpleNamespace(id=1, balance_kopeks=123)
    agreement = SimpleNamespace(id=3, owner_id=1, subscription_id=2, status='CANCEL_PENDING')
    await emit_payhot_receipt(AsyncMock(), tx, user, None, agreement, applied=False)
    notify.assert_awaited_once()


async def test_missing_receipt_or_wrong_owner_emits_nothing(monkeypatch):
    from app.database.crud import transaction
    from app.services.payhot_recurrent_notifications import emit_payhot_receipt

    event = AsyncMock()
    monkeypatch.setattr(transaction, 'emit_transaction_side_effects', event)
    agreement = SimpleNamespace(owner_id=1)
    await emit_payhot_receipt(AsyncMock(), None, None, None, agreement, applied=False)
    await emit_payhot_receipt(
        AsyncMock(), SimpleNamespace(user_id=2), SimpleNamespace(id=2), None, agreement, applied=False
    )
    event.assert_not_called()
