from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _make_sub(*, sub_id=1, user_id=10, autopay=False, hours_left=10, tg_id=555):
    user = SimpleNamespace(id=user_id, telegram_id=tg_id, language='ru')
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        autopay_enabled=autopay,
        status='active',
        end_date=datetime.now(UTC) + timedelta(hours=hours_left),
        tariff=None,
    )


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    svc._log_monitoring_event = AsyncMock()
    svc._send_prerenew_save_notification = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(ms.NotificationSettingsService, 'are_notifications_globally_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(ms.NotificationSettingsService, 'is_prerenew_save_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_trigger_hours', classmethod(lambda cls: 36))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_discount_percent', classmethod(lambda cls: 15))
    monkeypatch.setattr(ms.NotificationSettingsService, 'get_prerenew_save_valid_hours', classmethod(lambda cls: 24))
    monkeypatch.setattr(ms.settings.__class__, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr('app.utils.notification_prefs.is_subscription_expiry_enabled', lambda user: True)
    yield


@pytest.fixture
def offer():
    return SimpleNamespace(id=99, expires_at=datetime.now(UTC) + timedelta(hours=24))


@pytest.mark.asyncio
async def test_at_risk_in_window_creates_offer_and_records(service, offer, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock(return_value=offer)
    record = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', record)

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs['notification_type'] == 'prerenew_save'
    assert upsert.await_args.kwargs['discount_percent'] == 15
    service._send_prerenew_save_notification.assert_awaited_once()
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_autopay_enabled_is_skipped(service, monkeypatch):
    sub = _make_sub(autopay=True, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()
    service._send_prerenew_save_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_pref_disabled_is_skipped(service, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    monkeypatch.setattr('app.utils.notification_prefs.is_subscription_expiry_enabled', lambda user: False)

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()
    service._send_prerenew_save_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_outside_window_is_skipped(service, monkeypatch):
    # trigger_hours=36, sub expires in 50h -> outside window
    sub = _make_sub(autopay=False, hours_left=50)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_sent_is_skipped(service, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=True))
    upsert = AsyncMock()
    monkeypatch.setattr(ms, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())

    await service._check_prerenew_save_offers(MagicMock())

    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_flag_early_returns(service, monkeypatch):
    monkeypatch.setattr(ms.NotificationSettingsService, 'is_prerenew_save_enabled', classmethod(lambda cls: False))
    get_subs = AsyncMock(return_value=[])
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', get_subs)

    await service._check_prerenew_save_offers(MagicMock())

    get_subs.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_does_not_record(service, offer, monkeypatch):
    sub = _make_sub(autopay=False, hours_left=10)
    monkeypatch.setattr(service, '_get_expiring_paid_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'upsert_discount_offer', AsyncMock(return_value=offer))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._send_prerenew_save_notification = AsyncMock(return_value=False)

    await service._check_prerenew_save_offers(MagicMock())

    record.assert_not_awaited()
