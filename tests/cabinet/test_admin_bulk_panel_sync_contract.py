"""Transaction contracts for real mandatory admin bulk panel mutations."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.routes import admin_bulk_actions as bulk
from app.cabinet.schemas.bulk_actions import BulkActionParams, BulkActionType
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
    action for action in MANDATORY_BULK_ACTIONS if action not in {
        BulkActionType.GRANT_SUBSCRIPTION, BulkActionType.DELETE_USER
    }
)


def _subscription(subscription_id: int, *, status: str = 'active') -> SimpleNamespace:
    tariff = SimpleNamespace(
        id=1, name='Starter', traffic_limit_gb=100, device_limit=2,
        max_device_limit=None, allowed_squads=[], is_daily=False,
    )
    return SimpleNamespace(
        id=subscription_id, user_id=42, status=status, is_active=status == 'active', is_trial=True,
        end_date=datetime.now(UTC) + timedelta(days=14), tariff=tariff, tariff_id=tariff.id,
        traffic_used_gb=5, traffic_limit_gb=100, device_limit=2, remnawave_uuid=f'sub-{subscription_id}',
        connected_squads=[], purchased_traffic_gb=0, traffic_reset_at=None,
        grace_suppressed_until=None, is_daily_paused=False,
    )


def _user_and_selected() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    first = _subscription(101)
    selected = _subscription(202)
    user = SimpleNamespace(id=42, username='target', remnawave_uuid='user-42', subscriptions=[first, selected])
    first.user = user
    selected.user = user
    return user, first, selected


def _params(action: BulkActionType) -> BulkActionParams:
    if action in {BulkActionType.EXTEND_SUBSCRIPTION}:
        return BulkActionParams(days=7)
    if action is BulkActionType.CHANGE_TARIFF:
        return BulkActionParams(tariff_id=2)
    if action is BulkActionType.ADD_TRAFFIC:
        return BulkActionParams(traffic_gb=8)
    if action is BulkActionType.SET_DEVICES:
        return BulkActionParams(device_limit=6)
    if action is BulkActionType.GRANT_SUBSCRIPTION:
        return BulkActionParams(tariff_id=2, days=7)
    return BulkActionParams()


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(
        id=2, name='Pro', traffic_limit_gb=200, device_limit=4, max_device_limit=None,
        allowed_squads=['pro'], is_daily=False,
    )


def _snapshot(sub: SimpleNamespace) -> dict[str, object]:
    return dict(vars(sub))


def _restore(sub: SimpleNamespace, state: dict[str, object]) -> None:
    sub.__dict__.clear()
    sub.__dict__.update(state)


def _configure_real_handler_edges(monkeypatch, db, user, selected, action, sync):
    """Keep bulk handlers real; replace only database/provider transport edges."""
    settings = MagicMock()
    settings.is_multi_tariff_enabled.return_value = False
    settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False
    monkeypatch.setattr(bulk, 'settings', settings)
    monkeypatch.setattr(bulk, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(bulk, 'get_subscription_by_id', AsyncMock(return_value=selected))
    monkeypatch.setattr(bulk, '_sync_subscription_to_panel', sync)
    extend = AsyncMock()
    add_traffic = AsyncMock()
    reactivate = AsyncMock()
    create = AsyncMock(return_value=_subscription(303))
    monkeypatch.setattr(bulk, 'extend_subscription', extend)
    monkeypatch.setattr(bulk, 'add_subscription_traffic', add_traffic)
    monkeypatch.setattr(bulk, 'reactivate_subscription', reactivate)
    monkeypatch.setattr(bulk, 'get_tariff_by_id', AsyncMock(return_value=selected.tariff))
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock())

    new_sub = create.return_value
    new_sub.user = user
    monkeypatch.setattr(bulk, 'create_paid_subscription', create)
    if action is BulkActionType.GRANT_SUBSCRIPTION:
        for sub in user.subscriptions:
            sub.status = 'expired'
            sub.is_active = False

    async def no_grace(*_args):
        return None

    async def no_cancel(*_args, **_kwargs):
        return None

    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', no_grace)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', no_cancel)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', no_cancel)
    disable = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService', lambda: SimpleNamespace(disable_remnawave_user=disable)
    )

    delete_account = AsyncMock(return_value=SimpleNamespace(bot_deleted=True, panel_deleted=True))
    monkeypatch.setattr('app.services.user_service.UserService.delete_user_account', delete_account)
    enable = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(disable_remnawave_user=disable, enable_remnawave_user=enable),
    )
    return SimpleNamespace(
        disable=disable, enable=enable, extend=extend, add_traffic=add_traffic,
        reactivate=reactivate, create=create, delete_account=delete_account,
    )


def _expected_action(action: BulkActionType) -> str:
    return action.value


def _assert_handler_staged_locally(action, edges, db, target, tariff):
    """Pin each real handler's local work below its target-boundary commit."""
    if action is BulkActionType.EXTEND_SUBSCRIPTION:
        edges.extend.assert_awaited_once_with(db, target, 7, commit=False)
    elif action is BulkActionType.CANCEL_SUBSCRIPTION:
        assert target.status == 'expired'
    elif action is BulkActionType.ACTIVATE_SUBSCRIPTION:
        assert target.status == 'active'
    elif action is BulkActionType.CHANGE_TARIFF:
        assert target.tariff_id == tariff.id
        assert target.traffic_limit_gb == tariff.traffic_limit_gb
    elif action is BulkActionType.ADD_TRAFFIC:
        edges.add_traffic.assert_awaited_once_with(db, target, 8, commit=False)
        edges.reactivate.assert_awaited_once_with(db, target, commit=False)
    elif action is BulkActionType.SET_DEVICES:
        assert target.device_limit == 6
    elif action is BulkActionType.DELETE_SUBSCRIPTION:
        assert db.execute.await_count == 3
    elif action is BulkActionType.GRANT_SUBSCRIPTION:
        assert edges.create.await_args.kwargs['commit'] is False
    elif action is BulkActionType.DELETE_USER:
        assert edges.delete_account.await_args.kwargs == {'admin_id': 0, 'force_panel_delete': True, 'commit': False}


@pytest.mark.anyio
@pytest.mark.parametrize('action', MANDATORY_BULK_ACTIONS)
@pytest.mark.parametrize('failure_type, reason', [
    (PanelSyncSkipped, PanelSyncReason.NOT_CONFIGURED),
    (PanelSyncFailed, PanelSyncReason.PANEL_API_FAILED),
])
async def test_real_bulk_user_handler_panel_failure_rolls_back_staged_state(monkeypatch, action, failure_type, reason):
    """Removing handler staging, its panel call, or executor rollback breaks this contract."""
    db = AsyncMock()
    user, first, selected = _user_and_selected()
    target = first
    before = _snapshot(target)
    db.rollback.side_effect = lambda: _restore(target, before)
    failure = failure_type(reason)
    sync = AsyncMock(side_effect=failure)
    edges = _configure_real_handler_edges(monkeypatch, db, user, selected, action, sync)

    if action is BulkActionType.DELETE_USER:
        monkeypatch.setattr(
            'app.services.user_service.UserService.delete_user_account',
            AsyncMock(side_effect=failure),
        )
    elif action is BulkActionType.DELETE_SUBSCRIPTION:
        edges.disable.side_effect = failure

    result = await bulk._execute_for_user(db, user.id, action, _params(action), _tariff(), dry_run=False)

    assert result.success is False
    expected_subscription_id = 303 if action is BulkActionType.GRANT_SUBSCRIPTION else target.id
    assert result.subscription_id == expected_subscription_id
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert vars(target) == before
    if action not in {BulkActionType.DELETE_SUBSCRIPTION, BulkActionType.DELETE_USER}:
        expected_target = 303 if action is BulkActionType.GRANT_SUBSCRIPTION else target.id
        assert sync.await_args.args[:2] == (db, user)
        assert sync.await_args.args[2].id == expected_target
        assert sync.await_args.kwargs == {'action': _expected_action(action), **(
            {'reset_traffic': False} if action is BulkActionType.CHANGE_TARIFF else {}
        )}


@pytest.mark.anyio
@pytest.mark.parametrize('action', MANDATORY_BULK_ACTIONS)
async def test_real_bulk_user_handler_stages_panel_work_and_commits_once(monkeypatch, action):
    """An internal handler commit or wrong panel target makes this target contract fail."""
    db = AsyncMock()
    user, first, selected = _user_and_selected()
    sync = AsyncMock(return_value={})
    edges = _configure_real_handler_edges(monkeypatch, db, user, selected, action, sync)

    result = await bulk._execute_for_user(db, user.id, action, _params(action), _tariff(), dry_run=False)

    assert result.success is True
    assert result.message
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    _assert_handler_staged_locally(action, edges, db, first, _tariff())
    if action is BulkActionType.DELETE_SUBSCRIPTION:
        edges.disable.assert_awaited_once_with(user.remnawave_uuid, db=db)
    elif action is not BulkActionType.DELETE_USER:
        expected_sub = 303 if action is BulkActionType.GRANT_SUBSCRIPTION else first.id
        synced_sub = sync.await_args.args[2]
        assert synced_sub.id == expected_sub
        assert sync.await_args.args[:2] == (db, user)
        assert sync.await_args.kwargs['action'] == _expected_action(action)


@pytest.mark.anyio
@pytest.mark.parametrize('action', SUBSCRIPTION_TARGET_ACTIONS)
@pytest.mark.parametrize('failure_type, reason', [
    (PanelSyncSkipped, PanelSyncReason.NOT_CONFIGURED),
    (PanelSyncFailed, PanelSyncReason.PANEL_API_FAILED),
])
async def test_real_subscription_handler_failure_uses_selected_subscription_and_rolls_back(
    monkeypatch, action, failure_type, reason
):
    db = AsyncMock()
    user, first, selected = _user_and_selected()
    before = _snapshot(selected)
    db.rollback.side_effect = lambda: _restore(selected, before)
    failure = failure_type(reason)
    sync = AsyncMock(side_effect=failure)
    edges = _configure_real_handler_edges(monkeypatch, db, user, selected, action, sync)
    if action is BulkActionType.DELETE_SUBSCRIPTION:
        edges.disable.side_effect = failure

    result = await bulk._execute_for_subscription(db, selected.id, action, _params(action), _tariff(), dry_run=False)

    assert result.success is False
    assert result.subscription_id == selected.id
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert vars(selected) == before
    if action not in {BulkActionType.DELETE_SUBSCRIPTION}:
        assert sync.await_args.args[2] is selected
        assert sync.await_args.args[2] is not first


@pytest.mark.anyio
@pytest.mark.parametrize('action', SUBSCRIPTION_TARGET_ACTIONS)
async def test_real_subscription_handler_success_uses_selected_subscription_and_commits_once(monkeypatch, action):
    db = AsyncMock()
    user, first, selected = _user_and_selected()
    sync = AsyncMock(return_value={})
    edges = _configure_real_handler_edges(monkeypatch, db, user, selected, action, sync)

    result = await bulk._execute_for_subscription(db, selected.id, action, _params(action), _tariff(), dry_run=False)

    assert result.success is True
    assert result.subscription_id == selected.id
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    _assert_handler_staged_locally(action, edges, db, selected, _tariff())
    if action is BulkActionType.DELETE_SUBSCRIPTION:
        edges.disable.assert_awaited_once_with(user.remnawave_uuid, db=db)
    else:
        assert sync.await_args.args[2] is selected
        assert sync.await_args.args[2] is not first


@pytest.mark.anyio
async def test_bulk_dry_run_neither_commits_nor_adds_a_commit():
    db = AsyncMock()
    user, _, _ = _user_and_selected()
    with patch.object(bulk, 'get_user_by_id', AsyncMock(return_value=user)):
        result = await bulk._execute_for_user(
            db, user.id, BulkActionType.SET_DEVICES, BulkActionParams(device_limit=3), None, True
        )

    assert result.success is True
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_bulk_subscription_delete_missing_panel_identity_rolls_back_before_local_delete(monkeypatch):
    db = AsyncMock()
    user, _, sub = _user_and_selected()
    user.remnawave_uuid = None
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
