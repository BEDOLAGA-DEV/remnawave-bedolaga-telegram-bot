"""Transaction contracts for mandatory admin bulk panel mutations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.routes import admin_bulk_actions as bulk
from app.cabinet.schemas.bulk_actions import BulkActionParams, BulkActionType, BulkUserResult
from app.services.admin_panel_sync import PanelSyncFailed, PanelSyncReason, PanelSyncSkipped


MANDATORY_BULK_ACTIONS = (
    BulkActionType.EXTEND_SUBSCRIPTION,
    BulkActionType.CANCEL_SUBSCRIPTION,
    BulkActionType.ACTIVATE_SUBSCRIPTION,
    BulkActionType.CHANGE_TARIFF,
    BulkActionType.ADD_TRAFFIC,
    BulkActionType.SET_DEVICES,
    BulkActionType.DELETE_SUBSCRIPTION,
    BulkActionType.GRANT_SUBSCRIPTION,
    BulkActionType.DELETE_USER,
)

SUBSCRIPTION_TARGET_ACTIONS = tuple(
    action
    for action in MANDATORY_BULK_ACTIONS
    if action not in {BulkActionType.GRANT_SUBSCRIPTION, BulkActionType.DELETE_USER}
)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=42, username='target', subscriptions=[])


@pytest.mark.anyio
@pytest.mark.parametrize('action', MANDATORY_BULK_ACTIONS)
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_bulk_user_target_panel_failure_rolls_back_without_false_success(action, failure):
    """The executor, rather than a handler, owns failure rollback and response."""
    db = AsyncMock()
    user = _user()
    user.subscriptions = [SimpleNamespace(id=99, is_active=True)]
    handler = AsyncMock(side_effect=failure)

    with (
        patch.object(bulk, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.object(bulk, '_do_change_tariff', handler),
        patch.object(bulk, '_do_grant_subscription', handler),
        patch.object(bulk, '_do_delete_user', handler),
        patch.dict(bulk._ACTION_HANDLERS, {action: handler}, clear=False),
    ):
        result = await bulk._execute_for_user(db, user.id, action, BulkActionParams(), None, dry_run=False)

    assert result.success is False
    assert result.subscription_id == 99
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('action', MANDATORY_BULK_ACTIONS)
async def test_bulk_user_target_success_commits_once_after_handler(action):
    db = AsyncMock()
    user = _user()
    handler = AsyncMock(
        return_value=BulkUserResult(user_id=user.id, success=True, message='Retained success', username=user.username)
    )

    with (
        patch.object(bulk, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.object(bulk, '_do_change_tariff', handler),
        patch.object(bulk, '_do_grant_subscription', handler),
        patch.object(bulk, '_do_delete_user', handler),
        patch.dict(bulk._ACTION_HANDLERS, {action: handler}, clear=False),
    ):
        result = await bulk._execute_for_user(db, user.id, action, BulkActionParams(), None, dry_run=False)

    assert result.success is True
    assert result.message == 'Retained success'
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.anyio
async def test_bulk_subscription_target_uses_selected_subscription_and_commits_once():
    db = AsyncMock()
    selected = SimpleNamespace(id=202, user=_user(), status='active', end_date=None, tariff=None, tariff_id=None,
                               traffic_used_gb=0, traffic_limit_gb=0, device_limit=1)
    selected.user.subscriptions = [SimpleNamespace(id=101), selected]
    handler = AsyncMock(
        return_value=BulkUserResult(user_id=selected.user.id, success=True, message='Retained success')
    )

    with (
        patch.object(bulk, 'get_subscription_by_id', AsyncMock(return_value=selected)),
        patch.dict(bulk._ACTION_HANDLERS, {BulkActionType.SET_DEVICES: handler}, clear=False),
    ):
        result = await bulk._execute_for_subscription(
            db, selected.id, BulkActionType.SET_DEVICES, BulkActionParams(device_limit=3), None, dry_run=False
        )

    handler.assert_awaited_once_with(db, selected.user, BulkActionParams(device_limit=3), False, sub_override=selected)
    assert result.subscription_id == selected.id
    db.commit.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize('action', SUBSCRIPTION_TARGET_ACTIONS)
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_bulk_subscription_target_panel_failure_rolls_back_with_exact_target(action, failure):
    db = AsyncMock()
    user = _user()
    selected = SimpleNamespace(id=202, user=user)
    handler = AsyncMock(side_effect=failure)

    with (
        patch.object(bulk, 'get_subscription_by_id', AsyncMock(return_value=selected)),
        patch.object(bulk, '_do_change_tariff', handler),
        patch.dict(bulk._ACTION_HANDLERS, {action: handler}, clear=False),
    ):
        result = await bulk._execute_for_subscription(db, selected.id, action, BulkActionParams(), None, dry_run=False)

    assert result.success is False
    assert result.subscription_id == selected.id
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_bulk_dry_run_neither_commits_nor_adds_a_commit():
    db = AsyncMock()
    user = _user()
    handler = AsyncMock()
    with (
        patch.object(bulk, 'get_user_by_id', AsyncMock(return_value=user)),
        patch.dict(bulk._ACTION_HANDLERS, {BulkActionType.SET_DEVICES: handler}, clear=False),
    ):
        # A dry-run handler is responsible for its preview response; the
        # executor must never add a transaction completion of its own.
        handler.return_value = BulkUserResult(user_id=user.id, success=True, message='Would set devices')
        await bulk._execute_for_user(
            db, user.id, BulkActionType.SET_DEVICES, BulkActionParams(device_limit=3), None, True
        )

    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_bulk_subscription_delete_missing_panel_identity_rolls_back_before_local_delete(monkeypatch):
    db = AsyncMock()
    user = _user()
    sub = SimpleNamespace(
        id=202,
        user=user,
        is_active=False,
        is_trial=False,
        tariff=None,
        remnawave_uuid=None,
        status='expired',
        end_date=None,
        tariff_id=None,
        traffic_used_gb=0,
        traffic_limit_gb=0,
        device_limit=1,
    )
    user.remnawave_uuid = None
    user.subscriptions = [sub]
    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False

    async def no_grace(*_args):
        return None

    async def no_cancel(*_args, **_kwargs):
        return None

    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', no_grace)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', no_cancel)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', no_cancel)

    with (
        patch.object(bulk, 'settings', fake_settings),
        patch.object(bulk, 'get_subscription_by_id', AsyncMock(return_value=sub)),
    ):
        result = await bulk._execute_for_subscription(
            db, sub.id, BulkActionType.DELETE_SUBSCRIPTION, BulkActionParams(), None, dry_run=False
        )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()
