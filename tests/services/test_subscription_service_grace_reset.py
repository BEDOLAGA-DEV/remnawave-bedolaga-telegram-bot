from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.grace_access_runtime as grace_runtime
import app.services.subscription_service as subscription_service_module
from app.config import Settings
from app.services.subscription_service import SubscriptionService


def _user(*, remnawave_id: int = 101) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=100,
        username='user',
        full_name='User',
        email=None,
        remnawave_id=remnawave_id,
        status='active',
    )


def _subscription(*, remnawave_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=11,
        user_id=1,
        status='limited',
        actual_status='limited',
        end_date=datetime.now(UTC) + timedelta(days=10),
        traffic_limit_gb=5,
        traffic_used_gb=0.0,
        connected_squads=['regular-squad'],
        tariff=None,
        tariff_id=2,
        remnawave_id=remnawave_id,
        is_trial=False,
        device_limit=2,
        subscription_url=None,
        subscription_crypto_link=None,
    )


def _db() -> AsyncMock:
    db = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _api() -> AsyncMock:
    api = AsyncMock()
    api.update_user.return_value = SimpleNamespace(
        subscription_url='https://example.test/sub',
        happ_crypto_link='happ://example',
    )
    return api


def _install(
    monkeypatch: pytest.MonkeyPatch,
    service: SubscriptionService,
    api: AsyncMock,
    user: SimpleNamespace,
    *,
    multi: bool,
) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi)
    monkeypatch.setattr(
        subscription_service_module,
        'get_user_by_id',
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(
        subscription_service_module,
        'resolve_hwid_device_limit_for_payload',
        lambda subscription: subscription.device_limit,
    )
    lock = AsyncMock(return_value={11})
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(grace_runtime, 'lock_grace_sensitive_panel_updates', lock)
    monkeypatch.setattr(grace_runtime, 'apply_grace_tariff_switch_reset_locked', reset)

    @asynccontextmanager
    async def client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', client)
    return lock, reset


@pytest.mark.asyncio
async def test_open_grace_reset_intent_delegates_to_locked_grace_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SubscriptionService()
    api = _api()
    db = _db()
    subscription = _subscription()
    _, reset = _install(monkeypatch, service, api, _user(), multi=False)
    direct_reset = AsyncMock()
    monkeypatch.setattr(service, '_reset_user_traffic', direct_reset)

    result = await service.update_remnawave_user(
        db,
        subscription,
        reset_traffic=True,
        reset_reason='tariff switch',
        tariff_switch_reset=True,
    )

    assert result is api.update_user.return_value
    reset.assert_awaited_once_with(
        db,
        subscription.id,
        source='subscription_service.update_remnawave_user',
    )
    direct_reset.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_grace_without_reset_intent_keeps_metadata_only_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SubscriptionService()
    api = _api()
    db = _db()
    subscription = _subscription()
    _, reset = _install(monkeypatch, service, api, _user(), multi=False)

    await service.update_remnawave_user(db, subscription, reset_traffic=False)

    reset.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()
    assert api.update_user.await_args.kwargs['user_id'] == 101


@pytest.mark.asyncio
async def test_open_grace_generic_reset_does_not_enter_tariff_switch_state_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SubscriptionService()
    api = _api()
    db = _db()
    subscription = _subscription()
    _, reset = _install(monkeypatch, service, api, _user(), multi=False)

    result = await service.update_remnawave_user(
        db,
        subscription,
        reset_traffic=True,
        reset_reason='daily traffic package expiration',
    )

    assert result is api.update_user.return_value
    reset.assert_not_awaited()
    api.reset_user_traffic.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_grace_multi_tariff_uses_subscription_panel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SubscriptionService()
    api = _api()
    db = _db()
    subscription = _subscription(remnawave_id=202)
    _, reset = _install(
        monkeypatch,
        service,
        api,
        _user(remnawave_id=303),
        multi=True,
    )

    await service.update_remnawave_user(
        db,
        subscription,
        reset_traffic=True,
        tariff_switch_reset=True,
    )

    reset.assert_awaited_once()
    assert api.update_user.await_args.kwargs['user_id'] == 202
