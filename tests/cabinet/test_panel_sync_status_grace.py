"""Карточка сверки «бот ↔ панель» и открытый временный доступ.

Пока грейс открыт, в панели намеренно стоит его оверлей: дата, статус, лимит и
сквад — грейса, а не подписки. Бот их не перенимает
(app/services/panel_sync/projection.py), но карточка сверки об этом не знала и
кричала «Есть отличия» на четырёх строках подряд у каждого, кому выдали
временный доступ. Владелец видел красное там, где всё правильно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import admin_users
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import (
    RemnaWaveUser,
    TrafficLimitStrategy,
    UserStatus as PanelStatus,
    UserTraffic,
)


NOW = datetime.now(UTC)
GRACE_UNTIL = NOW + timedelta(days=1)


def _subscription(**overrides):
    base = dict(
        id=7,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=NOW - timedelta(days=1),
        traffic_limit_gb=300,
        traffic_used_gb=71.0,
        device_limit=3,
        connected_squads=['own-squad'],
        remnawave_id=9001,
        is_active=False,
        tariff=None,
        grace_session_open=False,
        grace_overlay_expire_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _panel_user():
    """Аккаунт в панели с оверлеем грейса: живой, своя дата, урезанный лимит, свой сквад."""
    return RemnaWaveUser(
        id=9001,
        short_uuid='s',
        username='u',
        status=PanelStatus.ACTIVE,
        traffic_limit_bytes=int(74 * 1024**3),
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        expire_at=GRACE_UNTIL,
        telegram_id=1,
        email=None,
        hwid_device_limit=3,
        description=None,
        tag=None,
        subscription_url='',
        active_internal_squads=[{'uuid': 'grace-squad'}],
        created_at=NOW,
        updated_at=NOW,
        user_traffic=UserTraffic(used_traffic_bytes=int(71 * 1024**3), lifetime_used_traffic_bytes=int(71 * 1024**3)),
    )


async def _status(monkeypatch: pytest.MonkeyPatch, subscription) -> object:
    user = SimpleNamespace(
        id=1,
        telegram_id=1,
        email=None,
        remnawave_id=9001,
        last_remnawave_sync=None,
        subscriptions=[subscription],
    )
    api = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=_panel_user()),
        find_users_by_telegram_id=AsyncMock(return_value=[]),
        find_users_by_email=AsyncMock(return_value=[]),
    )

    class _Client:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *exc):
            return False

    service = SimpleNamespace(is_configured=True, get_api_client=_Client)
    db = SimpleNamespace(refresh=AsyncMock())

    with (
        patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=user)),
        patch('app.services.remnawave_service.RemnaWaveService', lambda *a, **k: service),
    ):
        return await admin_users.get_user_sync_status(user_id=1, subscription_id=None, admin=None, db=db)


async def test_open_grace_is_not_a_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дата, статус, лимит и сквад грейса — так и задумано, а не расхождение."""
    response = await _status(
        monkeypatch,
        _subscription(grace_session_open=True, grace_overlay_expire_at=GRACE_UNTIL),
    )

    assert response.grace_open is True
    assert response.grace_until == GRACE_UNTIL
    assert response.differences == []
    assert response.has_differences is False


async def test_without_grace_the_same_panel_data_is_a_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Те же данные панели без грейса обязаны остаться расхождением."""
    response = await _status(monkeypatch, _subscription())

    assert response.grace_open is False
    assert response.grace_until is None
    assert response.has_differences is True
    assert len(response.differences) >= 3


async def test_traffic_used_is_still_compared_during_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Расход трафика панель ведёт и в грейсе — его сверять надо по-прежнему."""
    response = await _status(
        monkeypatch,
        _subscription(grace_session_open=True, grace_overlay_expire_at=GRACE_UNTIL, traffic_used_gb=5.0),
    )

    assert response.grace_open is True
    assert any('Traffic used' in item for item in response.differences)
