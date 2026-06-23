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
