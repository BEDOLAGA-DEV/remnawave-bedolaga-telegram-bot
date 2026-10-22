import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from types import SimpleNamespace
from app.config import settings

@pytest.fixture(autouse=True)
def configure_recurrent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', True, raising=False)

class MockResult:
    def __init__(self, val):
        self.val = val
    def scalar_one(self):
        return self.val
    def scalar_one_or_none(self):
        return self.val

@pytest.mark.asyncio
async def test_yookassa_guest_purchase_recurrent_registration() -> None:
    from app.services.payment.yookassa import YooKassaPaymentMixin
    from sqlalchemy.ext.asyncio import AsyncSession
    
    db = AsyncMock(spec=AsyncSession)
    payment = SimpleNamespace(
        id=123,
        yookassa_payment_id='yk_payment_123',
        amount_kopeks=5000,
        user_id=None,
        status='succeeded',
        is_paid=True,
        metadata_json='{"purchase_token": "test_purchase_token_123"}',
        test_mode=False,
    )
    
    # Mock execute returning MockResult
    def fake_execute(query, *args, **kwargs):
        q_str = str(query).lower()
        if "guest_purchase" in q_str:
            return MockResult(99)  # Resolved user ID
        return MockResult(payment)

    db.execute = AsyncMock(side_effect=fake_execute)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    
    mixin = YooKassaPaymentMixin()
    
    # Mock import_module for app.services.payment_service
    mock_payment_service = SimpleNamespace(
        get_transaction_by_external_id=AsyncMock(return_value=None),
        link_yookassa_payment_to_transaction=AsyncMock()
    )
    
    event_obj = {
        'id': 'yk_payment_123',
        'status': 'succeeded',
        'paid': True,
        'payment_method': {'type': 'bank_card', 'id': 'pm_id_123'},
    }
    
    with (
        patch('app.services.payment.yookassa.import_module', return_value=mock_payment_service),
        patch('app.services.payment.common.try_fulfill_guest_purchase', AsyncMock(return_value=True)),
        patch.object(mixin, '_save_payment_method_if_available', AsyncMock()) as mock_save_pm,
    ):
        result = await mixin._process_successful_yookassa_payment(db, payment, event_object=event_obj)
        
        assert result is True
        assert payment.user_id == 99
        mock_save_pm.assert_awaited_once_with(db, payment, event_obj)


@pytest.mark.asyncio
async def test_antilopay_guest_purchase_recurrent_registration() -> None:
    from app.services.payment.antilopay import AntilopayPaymentMixin
    from sqlalchemy.ext.asyncio import AsyncSession
    
    db = AsyncMock(spec=AsyncSession)
    payment = SimpleNamespace(
        id=456,
        order_id='anti_order_456',
        antilopay_payment_id='anti_payment_456',
        amount_kopeks=5000,
        user_id=None,
        transaction_id=None,
        status='pending',
        is_paid=False,
        metadata_json={'purchase_token': 'test_anti_token_456'},
    )
    
    db.execute = AsyncMock(return_value=MockResult(100))  # Resolved user ID
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    
    mixin = AntilopayPaymentMixin()
    
    with (
        patch('app.services.payment.common.try_fulfill_guest_purchase', AsyncMock(return_value=True)),
    ):
        result = await mixin._finalize_antilopay_payment(db, payment, trigger='webhook')
        
        assert result is True
        assert payment.user_id == 100
        db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_antilopay_recurrent_suffix_callback_creates_payment() -> None:
    from app.services.payment.antilopay import AntilopayPaymentMixin
    from sqlalchemy.ext.asyncio import AsyncSession

    db = AsyncMock(spec=AsyncSession)

    base_payment = SimpleNamespace(
        id=789,
        order_id='alpguest_6b6ead',
        user_id=91752,
        amount_kopeks=21900,
        currency='RUB',
        payment_method='card',
        metadata_json={},
    )

    created_recurrent_payment = SimpleNamespace(
        id=790,
        order_id='alpguest_6b6ead_R3',
        user_id=91752,
        amount_kopeks=21900,
        currency='RUB',
        payment_method='card',
        status='pending',
        is_paid=False,
        paid_at=None,
        antilopay_payment_id='APAYABD1AC401785024265347',
        metadata_json={'is_recurrent_charge': True, 'parent_order_id': 'alpguest_6b6ead'},
        transaction_id=None,
    )

    mock_crud = SimpleNamespace(
        get_antilopay_payment_by_order_id=AsyncMock(
            side_effect=lambda db_sess, order_id: base_payment if order_id == 'alpguest_6b6ead' else None
        ),
        create_antilopay_payment=AsyncMock(return_value=created_recurrent_payment),
        get_antilopay_payment_by_id_for_update=AsyncMock(return_value=created_recurrent_payment),
        update_antilopay_payment_status=AsyncMock(return_value=created_recurrent_payment),
    )

    mixin = AntilopayPaymentMixin()

    payload = {
        'type': 'payment',
        'order_id': 'alpguest_6b6ead_R3',
        'payment_id': 'APAYABD1AC401785024265347',
        'status': 'SUCCESS',
        'amount': 219.0,
        'original_amount': 219.0,
        'recurrent_id': 'REC_12345',
    }

    with (
        patch('app.services.payment.antilopay.import_module', return_value=mock_crud),
        patch.object(mixin, '_finalize_antilopay_payment', AsyncMock(return_value=True)) as mock_finalize,
        patch.object(mixin, '_register_antilopay_recurrent_from_callback', AsyncMock()) as mock_register,
    ):
        result = await mixin.process_antilopay_callback(db, payload)

        assert result is True
        mock_crud.create_antilopay_payment.assert_awaited_once()
        create_kwargs = mock_crud.create_antilopay_payment.call_args.kwargs
        assert create_kwargs['order_id'] == 'alpguest_6b6ead_R3'
        assert create_kwargs['user_id'] == 91752
        assert create_kwargs['amount_kopeks'] == 21900
        mock_finalize.assert_awaited_once_with(db, created_recurrent_payment, trigger='webhook')
        mock_register.assert_awaited_once()

