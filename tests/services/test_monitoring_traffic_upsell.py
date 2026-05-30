from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _make_sub(*, sub_id=1, user_id=10, tg_id=555, used=8.5, limit=10):
    user = SimpleNamespace(id=user_id, telegram_id=tg_id, language='ru')
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        status='active',
        traffic_limit_gb=limit,
        traffic_used_gb=used,
    )


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    svc._deliver_web_notification = AsyncMock()
    svc._send_traffic_upsell_notification = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(MonitoringService, '_parse_traffic_warning_thresholds', lambda self: [80, 95])
    monkeypatch.setattr('app.database.crud.user_notification.check_recent_traffic_warning', AsyncMock(return_value=False))
    monkeypatch.setattr('app.utils.notification_prefs.is_traffic_warning_enabled', lambda user: True)
    yield


def _db_returning(subs):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_threshold_crossed_sends_web_and_telegram(service):
    db = _db_returning([_make_sub(used=8.5, limit=10)])  # 85% -> crosses 80
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
    service._send_traffic_upsell_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_email_only_user_gets_web_but_not_telegram(service):
    db = _db_returning([_make_sub(tg_id=None, used=9.6, limit=10)])  # 96%
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_pref_disabled_sends_nothing(service, monkeypatch):
    monkeypatch.setattr('app.utils.notification_prefs.is_traffic_warning_enabled', lambda user: False)
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_hit_sends_nothing(service, monkeypatch):
    monkeypatch.setattr('app.database.crud.user_notification.check_recent_traffic_warning', AsyncMock(return_value=True))
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_below_threshold_sends_nothing(service):
    db = _db_returning([_make_sub(used=5.0, limit=10)])  # 50%
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_not_awaited()
    service._send_traffic_upsell_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_does_not_break_web(service):
    service._send_traffic_upsell_notification = AsyncMock(side_effect=RuntimeError('tg down'))
    db = _db_returning([_make_sub(used=8.5, limit=10)])
    await service._check_traffic_usage_warnings(db)
    service._deliver_web_notification.assert_awaited_once()
