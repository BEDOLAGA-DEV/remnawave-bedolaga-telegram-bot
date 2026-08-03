"""Executable public-route outcome contracts for status transitions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateUserStatusRequest
from app.services.admin_panel_sync import PanelSyncFailed, PanelSyncReason, PanelSyncSkipped
from tests.cabinet.admin_panel_sync_case_manifest import (
    STATUS_FAILED_CASES,
    STATUS_SKIPPED_CASES,
    STATUS_SUCCESS_CASES,
)


@pytest.fixture
def user():
    return SimpleNamespace(id=17, status='expired', subscriptions=[], updated_at=None)


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
    if route == 'active':
        monkeypatch.setattr('app.services.user_service.UserService.unblock_user', AsyncMock(return_value=False))
    elif route == 'blocked':
        monkeypatch.setattr('app.services.user_service.UserService.block_user', AsyncMock(return_value=False))
    else:
        monkeypatch.setattr(
            admin_users,
            '_require_panel_disable_for_subscriptions',
            AsyncMock(side_effect=PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)),
        )

    result = await _call_status(route, user, db)

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'route'), STATUS_FAILED_CASES)
async def test_status_transition_failed_cases_fail_closed_on_public_route(monkeypatch, user, db, case_key, route):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    if route == 'active':
        monkeypatch.setattr('app.services.user_service.UserService.unblock_user', AsyncMock(return_value=False))
    elif route == 'blocked':
        monkeypatch.setattr('app.services.user_service.UserService.block_user', AsyncMock(return_value=False))
    else:
        monkeypatch.setattr(
            admin_users,
            '_require_panel_disable_for_subscriptions',
            AsyncMock(side_effect=PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)),
        )

    result = await _call_status(route, user, db)

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.commit.assert_not_awaited()
