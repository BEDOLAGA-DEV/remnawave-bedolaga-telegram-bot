from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import monitoring_service
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService


def _service():
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())
    return service


async def test_global_switch_stops_monitoring_notification_queries(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: False),
    )
    service = _service()
    db = SimpleNamespace(execute=AsyncMock())

    await service._check_expiring_subscriptions(db)
    await service._check_trial_expiring_soon(db)
    await service._check_traffic_warnings(db)
    await service._check_low_balance_alerts(db)

    db.execute.assert_not_awaited()
    service.bot.send_message.assert_not_awaited()


async def test_expiration_state_updates_even_when_notifications_are_disabled(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: False),
    )
    subscription = SimpleNamespace(id=7, user_id=42, tariff=None)
    user = SimpleNamespace(id=42, notification_settings={})
    expire_subscription = AsyncMock()

    monkeypatch.setattr(monitoring_service, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_service, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.database.crud.subscription.is_recently_updated_by_webhook',
        lambda subscription: False,
    )
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_subscription)

    service = _service()
    service._send_subscription_expired_notification = AsyncMock()
    service._log_monitoring_event = AsyncMock()
    db = SimpleNamespace(execute=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire_subscription.assert_awaited_once_with(db, subscription)
    service._send_subscription_expired_notification.assert_not_awaited()
