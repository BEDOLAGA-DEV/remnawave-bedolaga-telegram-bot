"""Trial warning timing follows runtime settings, preserving existing deduplication."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import monitoring_service as module


@pytest.mark.parametrize('hours', [1, 2, 12, 24, 48])
async def test_trial_query_uses_configured_warning_window(monkeypatch, hours):
    now = datetime(2026, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.settings, 'TRIAL_WARNING_HOURS', hours)
    monkeypatch.setattr(module.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    service = module.MonitoringService.__new__(module.MonitoringService)
    await service._check_trial_expiring_soon(db)
    values = list(db.execute.await_args.args[0].compile().params.values())
    assert now + timedelta(hours=hours) in values
    assert now in values


@pytest.mark.parametrize('sent,delivery_ok', [(True, True), (False, True), (False, False)])
async def test_trial_warning_keeps_legacy_deduplication_and_retries_failed_delivery(monkeypatch, sent, delivery_ok):
    monkeypatch.setattr(module.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(module.settings, 'TRIAL_WARNING_HOURS', 12)
    user = SimpleNamespace(id=1, telegram_id=100001, notification_settings={})
    subscription = SimpleNamespace(id=7, user=user)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [subscription]
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    seen, record = AsyncMock(return_value=sent), AsyncMock()
    monkeypatch.setattr(module, 'notification_sent', seen)
    monkeypatch.setattr(module, 'record_notification', record)
    service = module.MonitoringService.__new__(module.MonitoringService)
    service.bot = object()
    service._send_trial_ending_notification = AsyncMock(return_value=delivery_ok)
    service._log_monitoring_event = AsyncMock()
    await service._check_trial_expiring_soon(db)
    seen.assert_awaited_once_with(db, 1, 7, 'trial_2h')
    assert service._send_trial_ending_notification.await_count == (0 if sent else 1)
    assert record.await_count == (1 if not sent and delivery_ok else 0)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_trial_notice_does_not_claim_two_hours_when_twelve_remain(monkeypatch, language):
    monkeypatch.setattr(module.settings, 'TRIAL_WARNING_HOURS', 12)
    service = module.MonitoringService.__new__(module.MonitoringService)
    service._send_message_with_logo = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=100001, language=language)
    subscription = SimpleNamespace(id=7, end_date=datetime.now(UTC) + timedelta(hours=11, minutes=59))
    assert await service._send_trial_ending_notification(user, subscription)
    assert '12' in service._send_message_with_logo.await_args.kwargs['text']


@pytest.mark.parametrize('trigger_days', [5, 30, 45, 60])
async def test_followup_query_includes_entire_configured_third_wave_day(monkeypatch, trigger_days):
    now = datetime(2026, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS', trigger_days)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_WAVE3_ENABLED', True)
    monkeypatch.setattr(module.settings, 'ENABLE_NOTIFICATIONS', True)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    service = module.MonitoringService.__new__(module.MonitoringService)
    service.bot = object()
    await service._check_expired_subscription_followups(db)
    values = list(db.execute.await_args.args[0].compile().params.values())
    cutoffs = [value for value in values if isinstance(value, datetime) and value < now]
    assert len(cutoffs) == 1
    assert cutoffs[0] <= now - timedelta(days=trigger_days + 1)


@pytest.mark.parametrize('env_override', [False, True])
async def test_trial_warning_uses_runtime_override_unless_env_pins_it(monkeypatch, env_override):
    from app.services.system_settings_service import bot_configuration_service

    key = 'TRIAL_WARNING_HOURS'
    monkeypatch.setattr(module.settings, key, 12)
    monkeypatch.setattr(bot_configuration_service, '_env_override_keys', {key} if env_override else set())
    bot_configuration_service._apply_to_settings(key, 24)
    expected = 12 if env_override else 24
    assert module.settings.get_trial_warning_hours() == expected
    monkeypatch.setattr(module.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    service = module.MonitoringService.__new__(module.MonitoringService)
    await service._check_trial_expiring_soon(db)
    dates = sorted(
        value for value in db.execute.await_args.args[0].compile().params.values() if isinstance(value, datetime)
    )
    assert dates[1] - dates[0] == timedelta(hours=expected)
