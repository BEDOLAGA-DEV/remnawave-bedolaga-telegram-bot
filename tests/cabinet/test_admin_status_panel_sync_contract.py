"""Executable public-route outcome contracts for status transitions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateUserStatusRequest
from app.config import settings
from app.services.admin_panel_sync import PanelSyncFailed, PanelSyncReason, PanelSyncSkipped
from tests.cabinet.admin_panel_sync_case_manifest import (
    STATUS_FAILED_CASES,
    STATUS_SKIPPED_CASES,
    STATUS_SUCCESS_CASES,
)


@pytest.fixture
def user():
    subscription = SimpleNamespace(
        id=23,
        status='disabled',
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_uuid='sub-exact-uuid',
    )
    return SimpleNamespace(
        id=17,
        status='expired',
        subscriptions=[subscription],
        updated_at=None,
        remnawave_uuid='wrong-user-uuid',
    )


@pytest.fixture
def db():
    return AsyncMock()


async def _call_status(route, user, db):
    return await admin_users.update_user_status(
        user.id,
        UpdateUserStatusRequest(status=route),
        SimpleNamespace(id=1),
        db,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), STATUS_SUCCESS_CASES)
async def test_status_transition_success_cases_use_the_public_route(monkeypatch, user, db, case_key, route):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    if route == 'active':
        monkeypatch.setattr('app.services.user_service.UserService.unblock_user', AsyncMock(return_value=True))
    elif route == 'blocked':
        monkeypatch.setattr('app.services.user_service.UserService.block_user', AsyncMock(return_value=True))
    else:
        monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', AsyncMock())

    result = await _call_status(route, user, db)

    assert result.success is True
    assert result.new_status == route
    if route == 'deleted':
        db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), STATUS_SKIPPED_CASES)
async def test_status_transition_skipped_cases_fail_closed_on_public_route(monkeypatch, user, db, case_key, route):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    panel_method = AsyncMock()
    if route == 'active':
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(is_configured=False, update_remnawave_user=panel_method),
        )
    elif route == 'blocked':
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(is_configured=False, disable_remnawave_user=panel_method),
        )
    else:
        monkeypatch.setattr(
            admin_users,
            '_require_panel_disable_for_subscriptions',
            AsyncMock(side_effect=PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)),
        )

    with capture_logs() as logs:
        result = await _call_status(route, user, db)

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.commit.assert_not_awaited()
    panel_method.assert_not_awaited()
    expected_action = {'active': 'unblock', 'blocked': 'block', 'deleted': 'status_deleted'}[route]
    assert any(
        event.get('user_id') == user.id
        and event.get('subscription_id') == user.subscriptions[0].id
        and event.get('action') == expected_action
        for event in logs
    )
    assert any(event.get('reason_code') == PanelSyncReason.NOT_CONFIGURED.value for event in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), STATUS_FAILED_CASES)
async def test_status_transition_failed_cases_fail_closed_on_public_route(monkeypatch, user, db, case_key, route):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    if route == 'active':
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(is_configured=True, update_remnawave_user=AsyncMock(return_value=None)),
        )
    elif route == 'blocked':
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(is_configured=True, disable_remnawave_user=AsyncMock(return_value=False)),
        )
    else:
        monkeypatch.setattr(
            admin_users,
            '_require_panel_disable_for_subscriptions',
            AsyncMock(side_effect=PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)),
        )

    with capture_logs() as logs:
        result = await _call_status(route, user, db)

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.commit.assert_not_awaited()
    assert any(
        event.get('user_id') == user.id
        and event.get('subscription_id') == user.subscriptions[0].id
        and event.get('reason_code') == PanelSyncReason.PANEL_API_FAILED.value
        for event in logs
    )
    assert 'secret-value' not in repr(logs)
