"""Истечение в боте сверяется с панелью: панель — истина по сроку.

Владелец (2026-09-11): «панель считает, когда кончится подписка, бот — биллинг для продажи».
Мониторинг гасил подписку по своей дате. Если срок продлили руками в панели, а бот об этом
ещё не читал (вебхуков нет, расписание раз в сутки), он гасил живую подписку и слал «истекла».
Теперь перед гашением бот спрашивает панель: аккаунт там активен и дата в будущем — забирает
дату и не гасит. Панель молчит или аккаунта нет — гасит по своей дате, как раньше.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import SubscriptionStatus
from app.services import monitoring_service as monitoring_module
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService


NOW = datetime.now(UTC)


class _FakeApi:
    def __init__(self, panel_user) -> None:
        self.panel_user = panel_user
        self.get_user_by_id = AsyncMock(return_value=panel_user)
        self.get_user_by_short_uuid = AsyncMock(return_value=None)
        self.find_users_by_telegram_id = AsyncMock(return_value=[])
        self.find_users_by_email = AsyncMock(return_value=[])


def _panel_user(status: str, expire_at: datetime):
    return SimpleNamespace(
        id=9001,
        status=status,
        expire_at=expire_at,
        used_traffic_bytes=0,
        traffic_limit_bytes=100 * 1024**3,
        hwid_device_limit=3,
        active_internal_squads=['squad-1'],
        short_uuid='abc',
        subscription_url='https://sub',
        happ_crypto_link=None,
    )


def _service(api) -> MonitoringService:
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())

    @asynccontextmanager
    async def client():
        yield api

    service.subscription_service = SimpleNamespace(is_configured=True, get_api_client=client)
    service._send_subscription_expired_notification = AsyncMock()
    service._log_monitoring_event = AsyncMock()
    return service


def _subscription(user):
    return SimpleNamespace(
        id=7,
        user_id=user.id,
        user=user,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW - timedelta(minutes=5),
        traffic_used_gb=0.0,
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['squad-1'],
        remnawave_id=9001,
        remnawave_short_uuid='abc',
        subscription_url='https://sub',
        subscription_crypto_link=None,
        grace_candidate_reason=None,
        grace_candidate_at=None,
        grace_tail_expire_at=None,
        grace_session_open=False,
        updated_at=None,
        last_webhook_update_at=None,
        tariff=None,
    )


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(
        NotificationSettingsService, 'are_notifications_globally_enabled', classmethod(lambda cls: True)
    )
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda subscription: False)
    monkeypatch.setattr('app.utils.notification_prefs.is_subscription_expiry_enabled', lambda user: True)


@pytest.mark.asyncio
async def test_alive_in_the_panel_is_not_expired_and_takes_the_panel_date(monkeypatch) -> None:
    user = SimpleNamespace(
        id=42, telegram_id=1001, email=None, remnawave_id=9001, status='active', notification_settings={}
    )
    subscription = _subscription(user)
    api = _FakeApi(_panel_user('ACTIVE', NOW + timedelta(days=20)))
    service = _service(api)
    expire = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription_if_still_due', expire)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire.assert_not_awaited()
    service._send_subscription_expired_notification.assert_not_awaited()
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date == NOW + timedelta(days=20), 'дата панели записана в бота'
    db.commit.assert_awaited()
    api.get_user_by_id.assert_awaited_once_with(9001)


@pytest.mark.asyncio
async def test_expired_in_the_panel_is_expired_in_the_bot(monkeypatch) -> None:
    user = SimpleNamespace(
        id=42, telegram_id=1001, email=None, remnawave_id=9001, status='active', notification_settings={}
    )
    subscription = _subscription(user)
    api = _FakeApi(_panel_user('EXPIRED', NOW - timedelta(minutes=5)))
    service = _service(api)
    expire = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription_if_still_due', expire)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire.assert_awaited_once_with(db, subscription)
    service._send_subscription_expired_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_silent_panel_falls_back_to_the_bot_date(monkeypatch) -> None:
    """Панель не отвечает — последняя известная истина у бота, гасим по ней."""
    user = SimpleNamespace(
        id=42, telegram_id=1001, email=None, remnawave_id=9001, status='active', notification_settings={}
    )
    subscription = _subscription(user)
    api = _FakeApi(None)
    api.get_user_by_id = AsyncMock(side_effect=RuntimeError('panel down'))
    service = _service(api)
    expire = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription_if_still_due', expire)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire.assert_awaited_once_with(db, subscription)


# ==================== грейс: ACTIVE панели — это оверлей, а не продление ====================
#
# Баг 2026-09-15 (5+ аккаунтов на сторонних установках): грейс выдан в 06:16 —
# в панели ACTIVE до конца грейса, сквад грейса, лимит «расход + 1 ГБ». В 06:42
# мониторинг, гася подписку по своей дате, спросил панель, принял оверлей за
# продление и перенёс его в бота. Воркер грейса увидел более позднюю дату и
# закрыл грейс «человек продлил» — аккаунт остался в скваде грейса.


def _grace_overlay_panel_user(expire_at: datetime):
    panel_user = _panel_user('ACTIVE', expire_at)
    panel_user.traffic_limit_bytes = 103 * 1024**3
    panel_user.active_internal_squads = ['grace-squad']
    return panel_user


@pytest.mark.asyncio
async def test_open_grace_overlay_is_not_taken_for_a_panel_renewal(monkeypatch) -> None:
    user = SimpleNamespace(
        id=42, telegram_id=1001, email=None, remnawave_id=9001, status='active', notification_settings={}
    )
    subscription = _subscription(user)
    subscription.grace_session_open = True
    subscription.traffic_limit_gb = 0
    api = _FakeApi(_grace_overlay_panel_user(NOW + timedelta(hours=72)))
    service = _service(api)
    expire = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription_if_still_due', expire)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire.assert_awaited_once_with(db, subscription)
    assert subscription.end_date == NOW - timedelta(minutes=5), 'дата грейса не стала датой подписки'
    assert subscription.connected_squads == ['squad-1'], 'сквад грейса не стал сквадом подписки'
    assert subscription.traffic_limit_gb == 0, 'лимит грейса не стал лимитом подписки'


@pytest.mark.asyncio
async def test_grace_tail_left_in_the_panel_is_not_a_renewal_either(monkeypatch) -> None:
    """После конца грейса панель ещё несколько минут ACTIVE с погашенной датой."""
    user = SimpleNamespace(
        id=42, telegram_id=1001, email=None, remnawave_id=9001, status='active', notification_settings={}
    )
    subscription = _subscription(user)
    tail = NOW + timedelta(minutes=5)
    subscription.grace_tail_expire_at = tail
    api = _FakeApi(_panel_user('ACTIVE', tail))
    service = _service(api)
    expire = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription_if_still_due', expire)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    await service._check_expired_subscriptions(db)

    expire.assert_awaited_once_with(db, subscription)
    assert subscription.end_date == NOW - timedelta(minutes=5)
