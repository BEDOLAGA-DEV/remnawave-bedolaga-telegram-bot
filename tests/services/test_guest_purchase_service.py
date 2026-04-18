from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.database.models import GuestPurchaseStatus
from app.services import guest_purchase_service


class _DummyScalars:
    def __init__(self, item):
        self._item = item

    def first(self):
        return self._item


class _DummyResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        return _DummyScalars(self._item)


async def test_fulfill_purchase_keeps_gift_pending_activation_for_existing_subscription_in_multi_mode(monkeypatch):
    monkeypatch.setattr(type(guest_purchase_service.settings), 'is_multi_tariff_enabled', lambda self: True, raising=False)

    purchase = SimpleNamespace(
        id=1,
        token='gift-token',
        status=GuestPurchaseStatus.PAID.value,
        tariff_id=7,
        period_days=30,
        is_gift=True,
        gift_recipient_type='email',
        gift_recipient_value='user@example.com',
        cabinet_password=None,
        buyer=None,
        landing=None,
        user=None,
    )
    user = SimpleNamespace(id=10, language='ru', auth_type='telegram')
    tariff = SimpleNamespace(id=7, name='Gift Pro', get_effective_price=lambda period_days: 10_000)
    active_subscription = SimpleNamespace(id=55, is_active=True)

    db = MagicMock()
    db.execute = AsyncMock(return_value=_DummyResult(purchase))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr(guest_purchase_service, '_find_or_create_user', AsyncMock(return_value=(user, False)))
    monkeypatch.setattr(guest_purchase_service, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        'app.database.crud.subscription.get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[active_subscription]),
    )

    notify_mock = AsyncMock()
    admin_notify_mock = AsyncMock()
    nalogo_mock = AsyncMock()
    create_paid_mock = AsyncMock()

    monkeypatch.setattr(guest_purchase_service, 'send_guest_notification', notify_mock)
    monkeypatch.setattr(guest_purchase_service, '_send_admin_notification', admin_notify_mock)
    monkeypatch.setattr(guest_purchase_service, '_create_nalogo_receipt_for_purchase', nalogo_mock)
    monkeypatch.setattr(guest_purchase_service, 'create_paid_subscription', create_paid_mock)

    result = await guest_purchase_service.fulfill_purchase(db, purchase.token)

    assert result is purchase
    assert purchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
    assert purchase.user_id == user.id
    create_paid_mock.assert_not_awaited()
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs['is_pending_activation'] is True
    admin_notify_mock.assert_awaited_once()
    nalogo_mock.assert_awaited_once()
