"""Самолечение при удалённом из панели юзере: PATCH /api/users отвечает
«User not found» (404 / A018 / A063), когда подписка в боте живая, — сервисы
должны пересоздать панель-юзера, а не падать в ошибку (кейс: админ удалил
пользователя из RemnaWave вручную, бот об этом не знает)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.services.monitoring_service as monitoring_service_mod
import app.services.subscription_service as subscription_service_mod
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import RemnaWaveAPIError, is_user_not_found_error
from app.services.monitoring_service import MonitoringService
from app.services.subscription_service import SubscriptionService


# ---- is_user_not_found_error: маркеры «панель-юзера больше нет» ----


def test_not_found_by_status_404():
    assert is_user_not_found_error(RemnaWaveAPIError('User not found', 404, {}))


def test_not_found_by_error_code_without_404():
    # Разные версии RemnaWave отвечают A018/A063 и не всегда со статусом 404
    assert is_user_not_found_error(RemnaWaveAPIError('x', 400, {'errorCode': 'A018'}))
    assert is_user_not_found_error(RemnaWaveAPIError('x', 500, {'errorCode': 'A063'}))


def test_other_errors_are_not_not_found():
    assert not is_user_not_found_error(RemnaWaveAPIError('fk violation', 400, {'errorCode': 'A039'}))
    assert not is_user_not_found_error(RemnaWaveAPIError('internal', 500, {}))
    assert not is_user_not_found_error(RemnaWaveAPIError('не настроен'))  # без статуса и response_data


# ---- общие фикстуры-строители ----


def _make_user():
    return SimpleNamespace(
        id=1,
        telegram_id=100,
        username='u',
        full_name='User',
        email=None,
        remnawave_uuid='dead-uuid',
    )


def _make_subscription(*, status=SubscriptionStatus.ACTIVE.value, days_left=10):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=11,
        user_id=1,
        status=status,
        end_date=now + timedelta(days=days_left),
        traffic_limit_gb=100,
        connected_squads=[],
        tariff=None,
        remnawave_uuid=None,
        is_trial=False,
        last_webhook_update_at=None,
    )


def _patch_api_client(monkeypatch, service, api):
    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', fake_client)


# ---- recreate_deleted_panel_user: гейт «только живые подписки» ----


async def test_recreate_active_delegates_to_create_flow():
    service = SubscriptionService()
    recreated = object()
    service.create_remnawave_user = AsyncMock(return_value=recreated)

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(), reset_traffic=True, reset_reason='renewal'
    )

    assert result is recreated
    service.create_remnawave_user.assert_awaited_once()
    assert service.create_remnawave_user.await_args.kwargs == {'reset_traffic': True, 'reset_reason': 'renewal'}


async def test_recreate_trial_is_also_alive():
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock(return_value=object())

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.TRIAL.value)
    )

    assert result is not None
    service.create_remnawave_user.assert_awaited_once()


async def test_recreate_skips_expired_status():
    """Истёкшую подписку не пересоздаём: админ удалил панель-юзера намеренно."""
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock()

    result = await service.recreate_deleted_panel_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.EXPIRED.value, days_left=-3)
    )

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_recreate_skips_active_status_with_past_end_date():
    """Статус ACTIVE, но end_date уже прошёл (scheduled job ещё не отработал) — не пересоздаём."""
    service = SubscriptionService()
    service.create_remnawave_user = AsyncMock()

    result = await service.recreate_deleted_panel_user(AsyncMock(), _make_subscription(days_left=-1))

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


# ---- SubscriptionService.update_remnawave_user: рекавери вместо ошибки ----


def _setup_subscription_service(monkeypatch, api):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)
    _patch_api_client(monkeypatch, service, api)
    return service


async def test_update_recreates_deleted_panel_user(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A018'})
    service = _setup_subscription_service(monkeypatch, api)

    recreated = object()
    service.create_remnawave_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    api.update_user.assert_awaited_once()
    service.create_remnawave_user.assert_awaited_once()


async def test_update_does_not_recreate_on_other_api_errors(monkeypatch):
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('internal error', 500, {})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


async def test_update_does_not_recreate_for_expired_subscription(monkeypatch):
    """Пуш DISABLED в удалённого панель-юзера: молча пропускаем, панель не засоряем."""
    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {})
    service = _setup_subscription_service(monkeypatch, api)
    service.create_remnawave_user = AsyncMock()

    result = await service.update_remnawave_user(
        AsyncMock(), _make_subscription(status=SubscriptionStatus.EXPIRED.value, days_left=-3)
    )

    assert result is None
    service.create_remnawave_user.assert_not_awaited()


# ---- MonitoringService.update_remnawave_user: тот же рекавери в рутинном синке ----


async def test_monitoring_update_happy_path_reaches_panel(monkeypatch):
    """Регрессия: self._gb_to_bytes не существовал у MonitoringService — метод падал
    AttributeError-ом до запроса в панель и молча возвращал None из общего except."""
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = MonitoringService()
    service.subscription_service._config_error = None  # is_configured → True

    monkeypatch.setattr(monitoring_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(monitoring_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)

    updated_user = SimpleNamespace(subscription_url='https://sub.example/u', happ_crypto_link=None)
    api = AsyncMock()
    api.update_user.return_value = updated_user
    _patch_api_client(monkeypatch, service.subscription_service, api)

    db = AsyncMock()
    result = await service.update_remnawave_user(db, _make_subscription())

    assert result is updated_user
    assert api.update_user.await_args.kwargs['traffic_limit_bytes'] == 100 * 1024**3
    db.commit.assert_awaited()


async def test_monitoring_update_recreates_deleted_panel_user(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = MonitoringService()
    service.subscription_service._config_error = None  # is_configured → True

    monkeypatch.setattr(monitoring_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(monitoring_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)

    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A063'})
    _patch_api_client(monkeypatch, service.subscription_service, api)

    recreated = object()
    service.subscription_service.recreate_deleted_panel_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is recreated
    api.update_user.assert_awaited_once()
    service.subscription_service.recreate_deleted_panel_user.assert_awaited_once()


async def test_monitoring_update_does_not_recreate_on_other_api_errors(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = MonitoringService()
    service.subscription_service._config_error = None

    monkeypatch.setattr(monitoring_service_mod, 'get_user_by_id', AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(monitoring_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)

    api = AsyncMock()
    api.update_user.side_effect = RemnaWaveAPIError('internal error', 500, {})
    _patch_api_client(monkeypatch, service.subscription_service, api)

    service.subscription_service.recreate_deleted_panel_user = AsyncMock()

    result = await service.update_remnawave_user(AsyncMock(), _make_subscription())

    assert result is None
    service.subscription_service.recreate_deleted_panel_user.assert_not_awaited()
