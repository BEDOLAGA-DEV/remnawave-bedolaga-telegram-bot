from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _make_sub(*, sub_id=1, user_id=10, tg_id=555, created_hours_ago=4):
    user = SimpleNamespace(
        id=user_id, telegram_id=tg_id, language='ru', remnawave_uuid='uuid-x', status='active'
    )
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        status='active',
        is_trial=True,
        remnawave_uuid='uuid-x',
        end_date=datetime.now(UTC) + timedelta(days=2),
        created_at=datetime.now(UTC) - timedelta(hours=created_hours_ago),
        tariff=None,
    )


def _db_returning(subs):
    """Mock AsyncSession whose execute(...).scalars().all() yields subs."""
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    svc._log_monitoring_event = AsyncMock()
    svc._send_trial_onboarding_notification = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    NSS = ms.NotificationSettingsService
    monkeypatch.setattr(NSS, 'are_notifications_globally_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(NSS, 'is_trial_onboard_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(NSS, 'get_trial_onboard_first_hours', classmethod(lambda cls: 3))
    monkeypatch.setattr(NSS, 'get_trial_onboard_second_hours', classmethod(lambda cls: 12))
    monkeypatch.setattr('app.utils.notification_prefs.is_promo_offers_enabled', lambda user: True)
    yield


@pytest.mark.asyncio
async def test_not_connected_sends_and_records(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)  # in 3h window
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._trial_user_connected = AsyncMock(return_value=False)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    service._send_trial_onboarding_notification.assert_awaited_once()
    assert service._send_trial_onboarding_notification.await_args.args[2] == 'first'
    record.assert_awaited_once()
    assert record.await_args.args[3] == 'trial_onboard_3h'


@pytest.mark.asyncio
async def test_connected_records_both_keys_no_send(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._trial_user_connected = AsyncMock(return_value=True)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    service._send_trial_onboarding_notification.assert_not_awaited()
    keys = {c.args[3] for c in record.await_args_list}
    assert keys == {'trial_onboard_3h', 'trial_onboard_12h'}


@pytest.mark.asyncio
async def test_second_window_uses_12h_key(service, monkeypatch):
    sub = _make_sub(created_hours_ago=15)  # past 12h
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._trial_user_connected = AsyncMock(return_value=False)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    assert service._send_trial_onboarding_notification.await_args.args[2] == 'second'
    assert record.await_args.args[3] == 'trial_onboard_12h'


@pytest.mark.asyncio
async def test_already_sent_is_skipped(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=True))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    service._trial_user_connected = AsyncMock(return_value=False)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    service._send_trial_onboarding_notification.assert_not_awaited()
    service._trial_user_connected.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_error_skips_without_record(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._trial_user_connected = AsyncMock(return_value=None)  # panel error

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    service._send_trial_onboarding_notification.assert_not_awaited()
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_promo_pref_disabled_is_skipped(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(ms, 'record_notification', AsyncMock())
    monkeypatch.setattr('app.utils.notification_prefs.is_promo_offers_enabled', lambda user: False)
    service._trial_user_connected = AsyncMock(return_value=False)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    service._send_trial_onboarding_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_flag_early_returns(service, monkeypatch):
    monkeypatch.setattr(
        ms.NotificationSettingsService, 'is_trial_onboard_enabled', classmethod(lambda cls: False)
    )
    db = _db_returning([])

    await service._check_trial_onboarding_nudge(db)

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_does_not_record(service, monkeypatch):
    sub = _make_sub(created_hours_ago=4)
    monkeypatch.setattr(ms, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(ms, 'record_notification', record)
    service._trial_user_connected = AsyncMock(return_value=False)
    service._send_trial_onboarding_notification = AsyncMock(return_value=False)

    await service._check_trial_onboarding_nudge(_db_returning([sub]))

    record.assert_not_awaited()
