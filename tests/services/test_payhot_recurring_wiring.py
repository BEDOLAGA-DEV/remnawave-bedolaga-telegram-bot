from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.payment.payhot import PayHotPaymentMixin


@pytest.mark.parametrize(
    'event,data',
    [
        ('recurring_charge.cancelled', {'object': 'recurring_charge'}),
        ('recurring_charge.skipped', {'object': 'recurring_charge'}),
        ('payment.succeeded', {'object': 'payment', 'origin': 'recurring'}),
        ('payment.refunded', {'object': 'payment', 'recurring_charge_id': 'synthetic'}),
    ],
)
async def test_recurring_callback_never_dispatches_balance(monkeypatch, event, data):
    from app.services import payhot_recurrent_service as runtime

    handle = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, 'process_payhot_recurring_callback', handle)
    db = SimpleNamespace(scalar=AsyncMock())
    payload = {'event_type': event, 'data': data}
    assert await PayHotPaymentMixin().process_payhot_callback(db, payload) is True
    handle.assert_awaited_once_with(db, payload, bot=None)
    db.scalar.assert_not_awaited()


async def test_first_payment_uses_enrollment_not_balance(monkeypatch):
    from app.services import payhot_recurrent_service as runtime

    accept = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, 'accept_enrollment_payment', accept)
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(metadata_json={'purpose': 'payhot_recurring'})), scalar=AsyncMock()
    )
    data = {'status': 'succeeded'}
    await PayHotPaymentMixin()._accept_payhot_state(db, 12, data)
    accept.assert_awaited_once_with(db, 12, data, bot=None)
    db.scalar.assert_not_awaited()


async def test_creation_recovery_uses_saved_enrollment(monkeypatch):
    from app.services import payhot_recurrent_service as runtime

    create = AsyncMock()
    monkeypatch.setattr(runtime, 'create_payhot_agreement', create)
    payment = SimpleNamespace(id=12, metadata_json={'purpose': 'payhot_recurring', 'agreement_id': 8})
    db = SimpleNamespace(scalar=AsyncMock())
    await PayHotPaymentMixin()._recover_payhot_creation(db, payment, config=object())
    create.assert_awaited_once_with(db, 8, bot=None)
    db.scalar.assert_not_awaited()


async def test_paid_enrollment_delivery_never_runs_topup_fulfillment(monkeypatch):
    from app.services import payhot_recurrent_service as runtime
    from app.services.payment import payhot_fulfillment

    deliver, topup = AsyncMock(), AsyncMock()
    monkeypatch.setattr(runtime, 'deliver_payhot_renewal', deliver)
    monkeypatch.setattr(payhot_fulfillment, 'fulfill_payhot_payment', topup)
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(metadata_json={'purpose': 'payhot_recurring', 'agreement_id': 8}))
    )
    await PayHotPaymentMixin()._fulfill_payhot(db, 12)
    deliver.assert_awaited_once_with(db, 8, payment_id=12)
    topup.assert_not_awaited()


@pytest.mark.parametrize('fails', [False, True])
async def test_monitoring_runs_recovery_when_feature_off(monkeypatch, fails):
    from app.config import settings
    from app.services import payhot_recurrent_service as runtime
    from app.services.monitoring_service import MonitoringService

    monkeypatch.setattr(settings, 'PAYHOT_RECURRENT_ENABLED', False)
    reconcile = AsyncMock(side_effect=RuntimeError('private') if fails else None)
    monkeypatch.setattr(runtime, 'reconcile_payhot_agreements', reconcile)
    service = SimpleNamespace(bot=None)
    db = SimpleNamespace(rollback=AsyncMock())
    await MonitoringService._reconcile_payhot_subscriptions(service, db)
    reconcile.assert_awaited_once_with(db, bot=None)
    assert db.rollback.await_count == int(fails)


async def test_first_callback_uses_agreement_scoped_canonical_refresh(monkeypatch):
    from app.services import payhot_recurrent_service as runtime
    from app.services.payment import payhot

    refresh = AsyncMock()
    monkeypatch.setattr(runtime, 'refresh_payhot_enrollment', refresh)
    monkeypatch.setattr(payhot, '_validate_remote', lambda payment, data: 'validated')
    remote = AsyncMock()
    monkeypatch.setattr(payhot.payhot_service, 'get_payment', remote)
    payment = SimpleNamespace(metadata_json={'purpose': 'payhot_recurring', 'agreement_id': 8})
    db = SimpleNamespace(scalar=AsyncMock(return_value=payment))
    assert await PayHotPaymentMixin().process_payhot_callback(
        db, {'event_type': 'payment.succeeded', 'data': {'merchant_order_reference': 'test'}}
    )
    refresh.assert_awaited_once_with(db, 8, bot=None)
    remote.assert_not_awaited()


async def test_enrollment_poll_does_not_use_ordinary_creation_or_get(monkeypatch):
    from app.services import payhot_recurrent_service as runtime
    from app.services.payment import payhot

    refresh = AsyncMock()
    monkeypatch.setattr(runtime, 'refresh_payhot_enrollment', refresh)
    monkeypatch.setattr(payhot.payhot_service, 'snapshot', object)
    remote = AsyncMock()
    monkeypatch.setattr(payhot.payhot_service, 'get_payment', remote)
    payment = SimpleNamespace(
        id=12,
        metadata_json={'purpose': 'payhot_recurring', 'agreement_id': 8},
        is_paid=False,
        next_check_at=None,
        payhot_payment_id=None,
        status='creating',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=payment), get=AsyncMock(return_value=payment), commit=AsyncMock()
    )
    service = PayHotPaymentMixin()
    service._recover_payhot_creation = AsyncMock()
    result = await service.check_payhot_payment_status(db, 12)
    assert result['is_paid'] is False
    refresh.assert_awaited_once_with(db, 8, bot=None)
    service._recover_payhot_creation.assert_not_awaited()
    remote.assert_not_awaited()
