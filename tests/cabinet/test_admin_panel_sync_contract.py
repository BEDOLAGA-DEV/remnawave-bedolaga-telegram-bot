from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from structlog.testing import capture_logs

from app.cabinet.routes import admin_bulk_actions, admin_users
from app.cabinet.schemas.bulk_actions import BulkActionParams
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import settings
from app.database.models import Subscription, User
from app.external.remnawave_api import RemnaWaveAPIError
from app.services.admin_panel_sync import (
    MANDATORY_ADMIN_PANEL_MUTATIONS,
    AdminPanelMutation,
    PanelSyncFailed,
    PanelSyncReason,
    PanelSyncSkipped,
    PanelSyncTarget,
    panel_sync_failure_message,
)
from app.services.subscription_service import SubscriptionService
from app.services.user_service import UserService
from tests.cabinet.admin_panel_sync_case_manifest import UNIFIED_CASES as UNIFIED_MUTATION_CASES
from tests.fixtures.sqlite_memory import memory_session


@pytest.fixture
def user():
    return SimpleNamespace(
        id=17,
        full_name='Contract User',
        username='contract-user',
        telegram_id=1700,
        email='contract@example.test',
        remnawave_uuid='user-level-uuid',
        last_remnawave_sync=None,
        status='active',
    )


@pytest.fixture
def subscription():
    return SimpleNamespace(
        id=23,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_uuid='subscription-level-uuid',
        remnawave_short_uuid=None,
        remnawave_short_id='sub23',
        subscription_url=None,
        subscription_crypto_link=None,
        traffic_limit_gb=20,
        connected_squads=[],
        tariff=SimpleNamespace(external_squad_uuid=None),
    )


@pytest.fixture
def db():
    return AsyncMock()


@pytest.fixture
def api():
    api = MagicMock()
    api.get_user_by_uuid = AsyncMock(return_value=SimpleNamespace(uuid='subscription-level-uuid'))
    api.reset_user_traffic = AsyncMock(return_value=None)
    return api


@pytest.fixture
def configured_panel(monkeypatch, api):
    @asynccontextmanager
    async def get_api_client():
        yield api

    service = MagicMock()
    service.is_configured = True
    service.get_api_client = get_api_client
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: service)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'build_remnawave_subscription_username', lambda self, **_: 'contract-user')
    monkeypatch.setattr(type(settings), 'format_remnawave_user_description', lambda self, **_: 'contract description')
    monkeypatch.setattr('app.services.subscription_service.get_traffic_reset_strategy', lambda _: 'no_reset')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _: 1)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.update_panel_user_grace_safe',
        AsyncMock(
            return_value=SimpleNamespace(
                subscription_url='https://panel/subscription',
                happ_crypto_link=None,
                short_uuid='short-subscription',
            )
        ),
    )
    return service


def test_typed_failures_are_bounded_and_safe():
    skipped = PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)
    failed = PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)

    assert skipped.reason_code is PanelSyncReason.NOT_CONFIGURED
    assert failed.reason_code is PanelSyncReason.PANEL_API_FAILED

    message = panel_sync_failure_message()
    assert 'not saved' in message.lower()
    assert 'token' not in message.lower()


@pytest.mark.asyncio
async def test_device_delete_requires_exact_subscription_uuid_in_multi_tariff_mode(monkeypatch, user, db):
    """A user-level UUID must never receive a multi-tariff device mutation."""
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    with pytest.raises(Exception) as raised:
        await admin_users.delete_user_device(
            user_id=user.id,
            hwid='hwid-1',
            admin=SimpleNamespace(id=1),
            db=db,
            subscription_id=None,
        )

    assert getattr(raised.value, 'status_code', None) == 400
    assert 'subscription panel identity' in str(getattr(raised.value, 'detail', '')).lower()


@pytest.mark.asyncio
async def test_block_panel_failure_rolls_back_without_local_success(monkeypatch, user, subscription, db):
    """Swallowing a required disable would falsely report a local block as successful."""
    user.subscriptions = [subscription]
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(disable_remnawave_user=AsyncMock(return_value=False)),
    )

    assert await UserService().block_user(db, user.id, admin_id=1) is False
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert user.status != 'blocked'


@pytest.mark.asyncio
async def test_unblock_panel_failure_rolls_back_without_local_success(monkeypatch, user, subscription, db):
    """Queued retries cannot replace the mandatory panel update at this boundary."""
    user.status = 'blocked'
    subscription.status = 'disabled'
    user.subscriptions = [subscription]
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(update_remnawave_user=AsyncMock(return_value=None)),
    )

    assert await UserService().unblock_user(db, user.id, admin_id=1) is False
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert user.status == 'blocked'


async def _configure_real_unblock_service(monkeypatch, user, subscriptions, api):
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.subscription_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.lock_grace_sensitive_panel_updates', AsyncMock(return_value=set())
    )
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _: 2)
    monkeypatch.setattr(type(settings), 'format_remnawave_user_description', lambda self, **_: 'description')

    @asynccontextmanager
    async def get_api_client():
        yield api

    service = SubscriptionService()
    service._config_error = None
    service.get_api_client = get_api_client
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', lambda: service)
    user.subscriptions = subscriptions
    for target in subscriptions:
        target.user_id = user.id
        target.status = 'disabled'
        target.actual_status = 'disabled'
        target.device_limit = 2
        target.connected_squads = []
        target.subscription_url = None
        target.subscription_crypto_link = None
        target.tariff = SimpleNamespace(traffic_reset_mode=None, external_squad_uuid=None)
    user.status = 'blocked'
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('subscription_count', [1, 2])
async def test_unblock_real_nested_service_uses_exact_uuids_and_one_owner_commit(
    monkeypatch, user, subscription, db, subscription_count
):
    subscriptions = [subscription]
    if subscription_count == 2:
        subscriptions.append(SimpleNamespace(**{**vars(subscription), 'id': 24, 'remnawave_uuid': 'sub-exact-uuid-2'}))
    api = SimpleNamespace(
        update_user=AsyncMock(
            side_effect=[
                SimpleNamespace(subscription_url=f'https://safe/{target.id}', happ_crypto_link=None)
                for target in subscriptions
            ]
        )
    )
    await _configure_real_unblock_service(monkeypatch, user, subscriptions, api)

    assert await UserService().unblock_user(db, user.id, admin_id=1) is True

    assert [call.kwargs['uuid'] for call in api.update_user.await_args_list] == [
        target.remnawave_uuid for target in subscriptions
    ]
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unblock_real_nested_service_late_failure_rolls_back_once_without_partial_local_success(
    monkeypatch, user, subscription, db
):
    second = SimpleNamespace(**{**vars(subscription), 'id': 24, 'remnawave_uuid': 'sub-exact-uuid-2'})
    subscriptions = [subscription, second]
    api = SimpleNamespace(
        update_user=AsyncMock(
            side_effect=[
                SimpleNamespace(subscription_url='https://safe/23', happ_crypto_link=None),
                RuntimeError('secret=https://panel.invalid/?token=secret-value'),
            ]
        )
    )
    await _configure_real_unblock_service(monkeypatch, user, subscriptions, api)

    with capture_logs() as logs:
        assert await UserService().unblock_user(db, user.id, admin_id=1) is False

    assert [call.kwargs['uuid'] for call in api.update_user.await_args_list] == [
        'subscription-level-uuid',
        'sub-exact-uuid-2',
    ]
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    assert user.status == 'blocked'
    assert [target.status for target in subscriptions] == ['disabled', 'disabled']
    assert any(
        event.get('user_id') == user.id
        and event.get('subscription_id') == second.id
        and event.get('action') == 'unblock'
        and event.get('reason_code') == PanelSyncReason.PANEL_API_FAILED.value
        for event in logs
    )
    assert 'secret-value' not in repr(logs)


@pytest.mark.asyncio
async def test_update_real_nested_service_open_grace_branch_respects_commit_false(monkeypatch, user, subscription, db):
    """The grace-preserving fast path must remain inside its caller-owned transaction."""
    user.status = 'active'
    subscription.user_id = user.id
    subscription.actual_status = 'expired'
    subscription.device_limit = 2
    api = SimpleNamespace(
        update_user=AsyncMock(
            return_value=SimpleNamespace(
                subscription_url='https://safe/grace',
                happ_crypto_link=None,
            )
        )
    )
    monkeypatch.setattr('app.services.subscription_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.lock_grace_sensitive_panel_updates',
        AsyncMock(return_value={subscription.id}),
    )
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _: 2)
    monkeypatch.setattr(type(settings), 'format_remnawave_user_description', lambda self, **_: 'description')

    @asynccontextmanager
    async def get_api_client():
        yield api

    service = SubscriptionService()
    service._config_error = None
    service.get_api_client = get_api_client

    result = await service.update_remnawave_user(
        db,
        subscription,
        commit=False,
        diagnostic_action='unblock',
    )

    assert result is not None
    api.update_user.assert_awaited_once()
    assert api.update_user.await_args.kwargs['uuid'] == subscription.remnawave_uuid
    assert 'status' not in api.update_user.await_args.kwargs
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_real_nested_service_recreation_path_propagates_commit_false(monkeypatch, user, subscription, db):
    """A panel-side 404 must not re-enter the legacy create path with nested commit enabled."""
    api = SimpleNamespace(update_user=AsyncMock(side_effect=RemnaWaveAPIError('missing', status_code=404)))
    service = await _configure_real_unblock_service(monkeypatch, user, [subscription], api)
    recreated = SimpleNamespace(subscription_url='https://safe/recreated', happ_crypto_link=None)
    service.recreate_deleted_panel_user = AsyncMock(return_value=recreated)

    result = await service.update_remnawave_user(
        db,
        subscription,
        commit=False,
        diagnostic_action='unblock',
    )

    assert result is recreated
    service.recreate_deleted_panel_user.assert_awaited_once_with(
        db,
        subscription,
        reset_traffic=False,
        reset_reason=None,
        commit=False,
        diagnostic_action='unblock',
    )
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unblock_real_recreation_create_failure_redacts_entire_log_stream(monkeypatch, user, subscription, db):
    """The 404-to-recreate create failure must not serialize panel credentials."""
    api = SimpleNamespace(
        update_user=AsyncMock(side_effect=RemnaWaveAPIError('missing', status_code=404)),
        get_user_by_uuid=AsyncMock(return_value=None),
        create_user=AsyncMock(
            side_effect=RuntimeError('secret=https://panel.invalid/?token=synthetic-secret&payload=x')
        ),
    )
    await _configure_real_unblock_service(monkeypatch, user, [subscription], api)
    subscription.actual_status = 'active'

    with capture_logs() as logs:
        assert await UserService().unblock_user(db, user.id, admin_id=1) is False

    api.create_user.assert_awaited_once()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    nested_diagnostics = [event for event in logs if event.get('event') == 'Required panel synchronization failed']
    assert nested_diagnostics == [
        {
            'event': 'Required panel synchronization failed',
            'log_level': 'error',
            'user_id': user.id,
            'subscription_id': subscription.id,
            'action': 'unblock',
            'reason_code': PanelSyncReason.PANEL_API_FAILED.value,
        }
    ]
    rendered_logs = repr(logs)
    assert 'synthetic-secret' not in rendered_logs
    assert 'https://panel.invalid' not in rendered_logs
    assert 'payload=x' not in rendered_logs


@pytest.mark.asyncio
async def test_unblock_real_recreation_validation_failure_redacts_entire_log_stream(
    monkeypatch, user, subscription, db
):
    """The recreation validation boundary is part of the same credential-redaction contract."""
    api = SimpleNamespace(
        update_user=AsyncMock(side_effect=RemnaWaveAPIError('missing', status_code=404)),
        get_user_by_uuid=AsyncMock(side_effect=RuntimeError('secret=https://panel.invalid/?token=validation-secret')),
        create_user=AsyncMock(),
    )
    await _configure_real_unblock_service(monkeypatch, user, [subscription], api)
    subscription.actual_status = 'active'

    with capture_logs() as logs:
        assert await UserService().unblock_user(db, user.id, admin_id=1) is False

    api.create_user.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    nested_diagnostics = [event for event in logs if event.get('event') == 'Required panel synchronization failed']
    assert nested_diagnostics == [
        {
            'event': 'Required panel synchronization failed',
            'log_level': 'error',
            'user_id': user.id,
            'subscription_id': subscription.id,
            'action': 'unblock',
            'reason_code': PanelSyncReason.PANEL_API_FAILED.value,
        }
    ]
    assert 'validation-secret' not in repr(logs)


@pytest.mark.asyncio
async def test_sync_not_configured_raises_skipped_without_commit(monkeypatch, user, subscription, db):
    """Removing the typed skip would let callers commit a mutation without a panel sync."""
    monkeypatch.setattr(
        'app.services.remnawave_service.RemnaWaveService',
        lambda: SimpleNamespace(is_configured=False),
    )

    with pytest.raises(PanelSyncSkipped) as raised:
        await admin_users._sync_subscription_to_panel(db, user, subscription, action='extend')

    assert raised.value.reason_code is PanelSyncReason.NOT_CONFIGURED
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_traffic_reset_failure_raises_failed_with_safe_diagnostic(
    configured_panel, user, subscription, db, api
):
    """Swallowing the required reset would report a mutation as synchronized when it is not."""
    api.reset_user_traffic.side_effect = TimeoutError('secret=https://panel/?token=token-value')

    with capture_logs() as logs:
        with pytest.raises(PanelSyncFailed) as raised:
            await admin_users._sync_subscription_to_panel(
                db, user, subscription, reset_traffic=True, action='set_traffic'
            )

    assert raised.value.reason_code is PanelSyncReason.PANEL_TIMEOUT_UNKNOWN
    db.commit.assert_not_awaited()
    failure = logs[-1]
    assert failure['event'] == 'Admin panel sync failed'
    assert {key: failure[key] for key in ('user_id', 'subscription_id', 'action', 'reason_code')} == {
        'user_id': 17,
        'subscription_id': 23,
        'action': 'set_traffic',
        'reason_code': PanelSyncReason.PANEL_TIMEOUT_UNKNOWN,
    }
    assert 'token-value' not in repr(logs)
    assert 'https://panel/?token=' not in repr(logs)


@pytest.mark.asyncio
async def test_multi_tariff_missing_subscription_uuid_never_uses_user_uuid(configured_panel, user, subscription, db):
    """Falling back to the user UUID would reset a sibling tariff's panel user."""
    subscription.remnawave_uuid = None
    user.remnawave_uuid = 'wrong-user-level-uuid'

    with pytest.raises(PanelSyncSkipped) as raised:
        await admin_users._sync_subscription_to_panel(db, user, subscription, reset_traffic=True, action='reset')

    assert raised.value.reason_code is PanelSyncReason.MISSING_SUBSCRIPTION_UUID
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_sync_does_not_commit(configured_panel, user, subscription, db):
    """Restoring a helper-level commit would split the caller-owned transaction."""
    changes = await admin_users._sync_subscription_to_panel(db, user, subscription, action='extend')

    assert changes == {'action': 'updated'}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('action,days_delta', [('extend', 7), ('shorten', -7)])
async def test_admin_extend_reaches_required_sync_boundary_with_attributable_action(
    monkeypatch, user, subscription, db, action, days_delta
):
    """Omitting ``action`` makes the existing admin extend path fail before panel sync."""
    subscription.is_active = True
    user.subscriptions = [subscription]
    observed_actions: list[str] = []

    async def required_sync(_db, _user, _subscription, *, action: str, **_kwargs):
        observed_actions.append(action)
        return {}

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock())
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', required_sync)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action=action, days=7),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert observed_actions == [action]
    admin_users.extend_subscription.assert_awaited_once_with(db, subscription, days_delta, commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [
        PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED),
        PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED),
    ],
)
@pytest.mark.parametrize('action,days_delta', [('extend', 7), ('shorten', -7)])
async def test_admin_extend_panel_failure_rolls_back_without_false_success(
    monkeypatch, user, subscription, db, failure, action, days_delta
):
    """The route, not a nested helper, owns the one final transaction commit."""
    subscription.is_active = True
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock())
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(side_effect=failure))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action=action, days=7),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    admin_users.extend_subscription.assert_awaited_once_with(db, subscription, days_delta, commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [
        PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED),
        PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED),
    ],
)
async def test_standalone_reset_panel_failure_prevents_local_delete(monkeypatch, user, subscription, db, failure):
    """Panel failure must occur before the standalone reset stages destructive SQL."""
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', AsyncMock(side_effect=failure))

    result = await admin_users.reset_user_subscription(
        user_id=user.id,
        request=admin_users.ResetSubscriptionRequest(),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.execute.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_reset_success_orders_exact_panel_before_local_delete_and_one_commit(
    monkeypatch, user, subscription, db
):
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    events: list[tuple[str, object]] = []

    async def panel(_user, targets, *, action):
        events.append(('panel', (targets[0].id, targets[0].remnawave_uuid, action)))

    async def execute(statement):
        events.append(('local_sql', statement.__class__.__name__))
        return MagicMock()

    async def commit():
        events.append(('commit', None))

    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', panel)
    db.execute.side_effect = execute
    db.commit.side_effect = commit

    result = await admin_users.reset_user_subscription(
        user_id=user.id,
        request=admin_users.ResetSubscriptionRequest(),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert result.message == 'Subscription reset successfully'
    assert events[0] == ('panel', (23, 'subscription-level-uuid', 'reset_user_subscription'))
    assert [event[0] for event in events[1:-1]] == ['local_sql', 'local_sql']
    assert events[-1] == ('commit', None)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_reset_explicit_false_cannot_bypass_mandatory_panel_disable(
    monkeypatch, user, subscription, db
):
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
    mandatory_disable = AsyncMock(side_effect=PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED))
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', mandatory_disable)

    result = await admin_users.reset_user_subscription(
        user_id=user.id,
        request=admin_users.ResetSubscriptionRequest(deactivate_in_panel=False),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert result.message == panel_sync_failure_message()
    mandatory_disable.assert_awaited_once_with(user, [subscription], action='reset_user_subscription')
    db.execute.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def _unified_request(action: str, subscription: SimpleNamespace) -> UpdateSubscriptionRequest:
    """Small valid request factory for exercising the public route branches."""
    values: dict[str, object] = {'action': action, 'subscription_id': subscription.id}
    if action in {'extend', 'shorten'}:
        values['days'] = 7
    elif action == 'set_end_date':
        values['end_date'] = datetime.now(UTC) + timedelta(days=45)
    elif action == 'change_tariff':
        values['tariff_id'] = 99
    elif action == 'set_traffic':
        values['traffic_limit_gb'] = 42
    elif action == 'add_traffic':
        values['traffic_gb'] = 5
    elif action == 'remove_traffic':
        values['traffic_purchase_id'] = 71
    elif action == 'set_device_limit':
        values['device_limit'] = 3
    return UpdateSubscriptionRequest(**values)


def _unified_success_message(action: str, request: UpdateSubscriptionRequest) -> str:
    """Exact legacy responses that the transaction-boundary tests must preserve."""
    messages = {
        'create': f'Subscription created for {request.days or 30} days',
        'extend': f'Subscription extended by {request.days} days',
        'shorten': f'Subscription shortened by {request.days} days',
        'change_tariff': 'Tariff changed to Target',
        'set_traffic': 'Traffic settings updated',
        'cancel': 'Subscription cancelled',
        'reset': 'Subscription reset',
        'activate': 'Subscription activated',
        'add_traffic': f'Added {request.traffic_gb} GB traffic (30 days)',
        'remove_traffic': 'Removed 5 GB traffic package',
        'set_device_limit': f'Device limit set to {request.device_limit}',
    }
    if action == 'set_end_date':
        return f'Subscription end date set to {request.end_date.isoformat()}'
    return messages[action]


async def _configure_unified_route_action(monkeypatch, db, user, subscription, action: str) -> None:
    """Remove unrelated I/O while retaining each real route branch and its local staging."""
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    subscription.is_active = True
    subscription.tariff_id = 1
    subscription.device_limit = 1
    subscription.purchased_traffic_gb = 5
    subscription.traffic_used_gb = 1.0
    subscription.traffic_reset_at = None
    subscription.is_daily_paused = False
    subscription.is_trial = False
    user.subscriptions = [subscription]
    if action == 'activate':
        subscription.status = 'expired'

    if action in {'change_tariff', 'activate'}:
        monkeypatch.setattr(
            'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=None)
        )

    async def no_commit(*_args, **kwargs):
        assert kwargs.get('commit') is False

    if action in {'extend', 'shorten'}:

        async def stage_extension(_db, target, days, *, commit):
            await no_commit(_db, target, days, commit=commit)
            target.end_date += timedelta(days=days)

        monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock(side_effect=stage_extension))
    elif action == 'create':
        user.subscriptions = []
        new_subscription = SimpleNamespace(**vars(subscription))
        new_subscription.local_record_staged = False

        async def stage_creation(*_args, **kwargs):
            await no_commit(*_args, **kwargs)
            new_subscription.local_record_staged = True
            user.subscriptions.append(new_subscription)
            return new_subscription

        monkeypatch.setattr(
            'app.database.crud.subscription.create_paid_subscription',
            AsyncMock(side_effect=stage_creation),
        )
    elif action == 'change_tariff':
        tariff = SimpleNamespace(
            id=99,
            name='Target',
            traffic_limit_gb=50,
            device_limit=2,
            max_device_limit=8,
            allowed_squads=[],
        )
        monkeypatch.setattr(admin_users, 'get_tariff_by_id', AsyncMock(return_value=tariff))
        monkeypatch.setattr('app.database.crud.subscription.calc_device_limit_on_tariff_switch', lambda **_: 2)
        monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(side_effect=no_commit))
        monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
        monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    elif action == 'cancel':
        monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
        monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    elif action == 'reset':

        async def reset_with_panel(_db, _user, _subscription, *, commit):
            assert commit is False
            _subscription.status = 'disabled'
            return {'panel_disabled': True}

        monkeypatch.setattr('app.services.subscription_service.reset_subscription_with_panel', reset_with_panel)
    elif action == 'add_traffic':

        async def stage_traffic(_db, target, traffic_gb, *, commit):
            await no_commit(_db, target, traffic_gb, commit=commit)
            target.purchased_traffic_gb += traffic_gb

        monkeypatch.setattr(
            'app.database.crud.subscription.add_subscription_traffic', AsyncMock(side_effect=stage_traffic)
        )
        monkeypatch.setattr('app.database.crud.subscription.reactivate_subscription', AsyncMock(side_effect=no_commit))
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService.enable_remnawave_user', AsyncMock(return_value=True)
        )
    elif action == 'remove_traffic':
        purchase = SimpleNamespace(id=71, traffic_gb=5, expires_at=datetime.now(UTC) + timedelta(days=1))
        purchase.deleted = False
        user.related_records = [purchase]

        async def stage_related_delete(record):
            record.deleted = True

        db.delete.side_effect = stage_related_delete
        db.execute.side_effect = [
            MagicMock(scalar_one_or_none=lambda: purchase),
            MagicMock(scalars=lambda: MagicMock(all=list)),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize('enable_outcome', [False, RuntimeError('panel unavailable'), 'missing_uuid'])
async def test_single_add_traffic_late_enable_failure_rolls_back_without_commit(
    monkeypatch, user, subscription, db, enable_outcome
):
    """The route must not commit traffic before its required direct enable succeeds."""
    await _configure_unified_route_action(monkeypatch, db, user, subscription, 'add_traffic')
    before = dict(vars(subscription))

    async def rollback():
        vars(subscription).clear()
        vars(subscription).update(before)

    db.rollback.side_effect = rollback
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(return_value={}))
    enable = AsyncMock(return_value=False)
    if isinstance(enable_outcome, Exception):
        enable.side_effect = enable_outcome
    if enable_outcome == 'missing_uuid':
        subscription.remnawave_uuid = None
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.enable_remnawave_user', enable)

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=_unified_request('add_traffic', subscription),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert result.message == panel_sync_failure_message()
    assert vars(subscription) == before
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    if enable_outcome == 'missing_uuid':
        enable.assert_not_awaited()
    else:
        enable.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('disable_outcome', [False, RuntimeError('panel unavailable')])
async def test_unified_reset_panel_failure_rolls_back_before_local_reset(
    monkeypatch, user, subscription, db, disable_outcome
):
    """The public reset action must translate False/exception into a typed rollback path."""
    from app.services import subscription_service as subscription_service_module

    real_reset_with_panel = subscription_service_module.reset_subscription_with_panel
    await _configure_unified_route_action(monkeypatch, db, user, subscription, 'reset')
    original = dict(vars(subscription))
    disable = AsyncMock(return_value=False)
    if isinstance(disable_outcome, Exception):
        disable.side_effect = disable_outcome
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.disable_remnawave_user', disable)

    monkeypatch.setattr(
        'app.services.subscription_service.reset_subscription_with_panel',
        real_reset_with_panel,
    )

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=_unified_request('reset', subscription),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert result.message == panel_sync_failure_message()
    assert vars(subscription) == original
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'action'), UNIFIED_MUTATION_CASES)
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_every_unified_action_route_fails_closed_after_local_staging(
    monkeypatch, user, subscription, db, case_key, action, failure
):
    """Public route branches, rather than the shared finisher, own fail-closed responses."""
    await _configure_unified_route_action(monkeypatch, db, user, subscription, action)
    original_subscription = dict(vars(subscription))
    original_subscriptions = list(user.subscriptions)
    original_related_records = [dict(vars(record)) for record in getattr(user, 'related_records', [])]
    observed: list[tuple[int, str, str]] = []

    async def fail_sync(_db, _user, target, *, action: str, **_kwargs):
        if action == 'create':
            assert target.local_record_staged is True
            assert target in user.subscriptions
        else:
            assert dict(vars(subscription)) != original_subscription
        observed.append((target.id, target.remnawave_uuid, action))
        raise failure

    async def restore_after_rollback():
        subscription.__dict__.clear()
        subscription.__dict__.update(original_subscription)
        user.subscriptions[:] = original_subscriptions
        for record, original in zip(getattr(user, 'related_records', []), original_related_records, strict=True):
            record.__dict__.clear()
            record.__dict__.update(original)

    db.rollback.side_effect = restore_after_rollback

    if action == 'reset':

        async def fail_reset(_db, _user, target, *, commit):
            assert commit is False
            target.status = 'disabled'
            observed.append((target.id, target.remnawave_uuid, 'reset'))
            raise failure

        monkeypatch.setattr('app.services.subscription_service.reset_subscription_with_panel', fail_reset)
    else:
        monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', fail_sync)

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=_unified_request(action, subscription),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    assert observed == [(23, 'subscription-level-uuid', action)]
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert dict(vars(subscription)) == original_subscription
    assert user.subscriptions == original_subscriptions
    assert [dict(vars(record)) for record in getattr(user, 'related_records', [])] == original_related_records


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ('change_tariff', 'cancel'))
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_active_recurring_tariff_change_and_cancel_do_not_commit_before_failed_panel_sync(
    monkeypatch, user, subscription, db, action, failure
):
    """Active recurrence cleanup remains inside the route-owned transaction."""
    await _configure_unified_route_action(monkeypatch, db, user, subscription, action)
    cancel_platega = AsyncMock()
    cancel_lava = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', cancel_platega)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', cancel_lava)
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(side_effect=failure))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=_unified_request(action, subscription),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    cancel_platega.assert_awaited_once_with(db, subscription.id, commit=False)
    cancel_lava.assert_awaited_once_with(db, subscription.id, commit=False)
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ('change_tariff', 'cancel'))
async def test_active_recurring_tariff_change_and_cancel_commit_once_after_successful_panel_sync(
    monkeypatch, user, subscription, db, action
):
    """Recurrence cleanup and local staging precede panel sync and the sole commit."""
    await _configure_unified_route_action(monkeypatch, db, user, subscription, action)
    events: list[str] = []

    async def cancel_platega(*_args, **kwargs):
        assert kwargs['commit'] is False
        events.append('platega')

    async def cancel_lava(*_args, **kwargs):
        assert kwargs['commit'] is False
        events.append('lava')

    async def sync(*_args, **_kwargs):
        events.append('panel')

    async def commit():
        events.append('commit')

    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', cancel_platega)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', cancel_lava)
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    db.commit.side_effect = commit

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=_unified_request(action, subscription),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert events[-2:] == ['panel', 'commit']
    assert events[:2] == ['platega', 'lava']
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'panel_outcome',
    [
        PanelSyncSkipped(PanelSyncReason.MISSING_SUBSCRIPTION_UUID),
        PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED),
    ],
)
async def test_strict_reset_active_recurrences_never_commit_before_skipped_or_failed_panel(
    monkeypatch, user, subscription, db, panel_outcome
):
    """Passing the default commit=True to either recurrence helper makes this fail."""
    subscription.user_id = user.id
    subscription.is_trial = False
    subscription.autopay_enabled = True
    subscription.traffic_used_gb = 5.0
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    cancellation_flags: list[tuple[str, bool]] = []

    async def active_platega(_db, _subscription_id, *, commit=True):
        cancellation_flags.append(('platega', commit))
        if commit:
            await _db.commit()

    async def active_lava(_db, _subscription_id, *, commit=True):
        cancellation_flags.append(('lava', commit))
        if commit:
            await _db.commit()

    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', active_platega)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', active_lava)
    if isinstance(panel_outcome, PanelSyncSkipped):
        subscription.remnawave_uuid = None
    else:
        monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', AsyncMock(return_value=False))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action='reset', subscription_id=subscription.id),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert result.message == panel_sync_failure_message()
    assert cancellation_flags == [('platega', False), ('lava', False)]
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_strict_reset_active_recurrences_share_the_single_route_commit_on_success(
    monkeypatch, user, subscription, db
):
    """Both active recurrence rows stay staged until panel disable completes."""
    subscription.user_id = user.id
    subscription.is_trial = False
    subscription.autopay_enabled = True
    subscription.traffic_used_gb = 5.0
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', AsyncMock(return_value=True))
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    events: list[str] = []

    async def active_cancel(name, _db, _subscription_id, *, commit=True):
        assert commit is False
        events.append(name)

    async def commit():
        events.append('commit')

    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        lambda db, subscription_id, *, commit=True: active_cancel('platega', db, subscription_id, commit=commit),
    )
    monkeypatch.setattr(
        'app.services.payment.lava.cancel_lava_recurring_for_subscription_safe',
        lambda db, subscription_id, *, commit=True: active_cancel('lava', db, subscription_id, commit=commit),
    )
    db.commit.side_effect = commit

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action='reset', subscription_id=subscription.id),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert events == ['platega', 'lava', 'commit']
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'skipped', 'failed'])
async def test_unified_extend_syncs_every_deactivated_sibling_trial_atomically(
    monkeypatch, user, subscription, db, outcome
):
    """A paid extension's hidden CRUD trial cleanup is part of the public mutation."""
    subscription.user_id = user.id
    subscription.is_trial = False
    subscription.autopay_enabled = False
    subscription.device_limit = 2
    subscription.traffic_used_gb = 0.0
    subscription.tariff_id = 10
    sibling = SimpleNamespace(
        id=24,
        user_id=user.id,
        status='trial',
        is_trial=True,
        autopay_enabled=True,
        end_date=datetime.now(UTC) + timedelta(days=7),
        remnawave_uuid='sibling-trial-uuid',
    )
    user.subscriptions = [subscription, sibling]
    before = dict(vars(sibling))
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    async def extend_with_hidden_trial_cleanup(_db, target, days, *, commit):
        assert target is subscription
        assert days == 3
        assert commit is False
        target.end_date += timedelta(days=days)
        sibling.status = 'disabled'
        sibling.is_trial = False
        sibling.autopay_enabled = False

    monkeypatch.setattr(admin_users, 'extend_subscription', extend_with_hidden_trial_cleanup)
    primary_sync = AsyncMock(return_value={})
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', primary_sync)
    disable = AsyncMock(return_value=outcome == 'success')
    service = SimpleNamespace(is_configured=outcome != 'skipped', disable_remnawave_user=disable)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', lambda: service)
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    async def rollback():
        vars(sibling).clear()
        vars(sibling).update(before)

    db.rollback.side_effect = rollback

    with capture_logs() as logs:
        result = await admin_users.update_user_subscription(
            user_id=user.id,
            request=UpdateSubscriptionRequest(action='extend', subscription_id=subscription.id, days=3),
            admin=SimpleNamespace(id=1),
            db=db,
        )

    primary_sync.assert_awaited_once_with(db, user, subscription, reset_traffic=False, action='extend')
    if outcome == 'success':
        assert result.success is True
        disable.assert_awaited_once_with(
            'sibling-trial-uuid',
            user_id=user.id,
            subscription_id=sibling.id,
            action='extend',
        )
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        assert sibling.status == 'disabled'
    else:
        assert result.success is False
        assert result.message == panel_sync_failure_message()
        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()
        assert vars(sibling) == before
        reason = PanelSyncReason.NOT_CONFIGURED if outcome == 'skipped' else PanelSyncReason.PANEL_API_FAILED
        assert any(
            event.get('subscription_id') == sibling.id
            and event.get('action') == 'extend'
            and event.get('reason_code') in {reason, reason.value}
            for event in logs
        )


@pytest.mark.asyncio
async def test_public_unified_single_tariff_paid_with_sibling_keeps_shared_panel_identity_active(
    monkeypatch, user, subscription, db
):
    """Extending paid access reconciles the shared UUID once and leaves it active."""
    subscription.user_id = user.id
    subscription.is_active = True
    subscription.is_trial = False
    subscription.autopay_enabled = False
    subscription.device_limit = 2
    subscription.traffic_used_gb = 0.0
    subscription.tariff_id = 10
    sibling = SimpleNamespace(
        id=24,
        user_id=user.id,
        status='trial',
        is_active=False,
        is_trial=True,
        autopay_enabled=True,
        end_date=datetime.now(UTC) + timedelta(days=7),
        remnawave_uuid=None,
    )
    user.remnawave_uuid = 'shared-user-uuid'
    user.subscriptions = [subscription, sibling]
    remote = {'status': 'disabled'}
    events: list[str] = []

    async def extend_with_hidden_trial_cleanup(_db, target, days, *, commit):
        assert target is subscription
        assert days == 3
        assert commit is False
        target.end_date += timedelta(days=days)
        sibling.status = 'disabled'
        sibling.is_trial = False
        sibling.autopay_enabled = False

    async def sync_primary(_db, synced_user, target, *, reset_traffic, action):
        assert synced_user is user
        assert target is subscription
        assert reset_traffic is False
        assert action == 'extend'
        remote['status'] = 'active'
        events.append('primary-active')

    async def disable_shared(panel_uuid, **_kwargs):
        assert panel_uuid == user.remnawave_uuid
        remote['status'] = 'disabled'
        events.append('shared-disabled')
        return True

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(admin_users, 'extend_subscription', extend_with_hidden_trial_cleanup)
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync_primary)
    disable = AsyncMock(side_effect=disable_shared)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(is_configured=True, disable_remnawave_user=disable),
    )
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action='extend', subscription_id=subscription.id, days=3),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert sibling.status == 'disabled'
    assert events == ['primary-active']
    assert remote == {'status': 'active'}
    disable.assert_not_awaited()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('create_variant', ['new', 'revive', 'trial_conversion'])
async def test_unified_create_variants_sync_every_deactivated_sibling_trial_before_commit(
    monkeypatch, user, db, create_variant
):
    """New, revived, and converted paid creates share the hidden sibling cleanup contract."""
    now = datetime.now(UTC)

    def sub(subscription_id, *, status, is_trial, panel_uuid):
        return SimpleNamespace(
            id=subscription_id,
            user_id=user.id,
            status=status,
            is_trial=is_trial,
            is_active=status in {'active', 'trial'},
            autopay_enabled=is_trial,
            end_date=now + timedelta(days=7),
            remnawave_uuid=panel_uuid,
            remnawave_short_uuid=None,
            remnawave_short_id=f'sub-{subscription_id}',
            subscription_url=None,
            subscription_crypto_link=None,
            traffic_limit_gb=20,
            traffic_used_gb=0.0,
            device_limit=2,
            connected_squads=[],
            tariff_id=10,
            tariff=SimpleNamespace(external_squad_uuid=None),
        )

    sibling = sub(42, status='trial', is_trial=True, panel_uuid='sibling-trial-uuid')
    if create_variant == 'new':
        primary = sub(41, status='active', is_trial=False, panel_uuid='new-paid-uuid')
        user.subscriptions = [sibling]
    elif create_variant == 'revive':
        primary = sub(41, status='expired', is_trial=False, panel_uuid='revived-paid-uuid')
        primary.is_active = False
        user.subscriptions = [primary, sibling]
    else:
        primary = sub(41, status='trial', is_trial=True, panel_uuid='converted-trial-uuid')
        user.subscriptions = [primary, sibling]

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        admin_users,
        'get_tariff_by_id',
        AsyncMock(
            return_value=SimpleNamespace(
                id=99, traffic_limit_gb=50, device_limit=3, allowed_squads=[], external_squad_uuid=None
            )
        ),
    )

    async def create_with_hidden_trial_cleanup(**kwargs):
        assert kwargs['commit'] is False
        primary.status = 'active'
        primary.is_trial = False
        sibling.status = 'disabled'
        sibling.is_trial = False
        sibling.autopay_enabled = False
        return primary

    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', create_with_hidden_trial_cleanup)
    primary_sync = AsyncMock(return_value={})
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', primary_sync)
    disable = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(is_configured=True, disable_remnawave_user=disable),
    )
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action='create', tariff_id=99, days=3),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    primary_sync.assert_awaited_once_with(db, user, primary, reset_traffic=False, action='create')
    disable.assert_awaited_once_with(
        'sibling-trial-uuid',
        user_id=user.id,
        subscription_id=sibling.id,
        action='create',
    )
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'skipped', 'failed'])
async def test_real_multisubscription_extend_commits_or_rolls_back_sibling_trial_as_one_unit(monkeypatch, outcome):
    """Exercise the real CRUD query/flush and real transaction over two subscription rows."""
    now = datetime.now(UTC)
    async with memory_session(monkeypatch, [User.__table__, Subscription.__table__]) as db:
        user_row = User(id=1701, username='real-multi-user')
        paid = Subscription(
            id=2301,
            user_id=user_row.id,
            status='active',
            is_trial=False,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=30),
            traffic_limit_gb=20,
            traffic_used_gb=0.0,
            device_limit=2,
            connected_squads=[],
            autopay_enabled=False,
            remnawave_uuid='paid-panel-uuid',
            remnawave_short_id='real-paid',
        )
        sibling = Subscription(
            id=2302,
            user_id=user_row.id,
            status='trial',
            is_trial=True,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=7),
            traffic_limit_gb=5,
            traffic_used_gb=0.0,
            device_limit=1,
            connected_squads=[],
            autopay_enabled=True,
            remnawave_uuid='real-sibling-panel-uuid',
            remnawave_short_id='real-trial',
        )
        db.add_all([user_row, paid, sibling])
        await db.commit()
        user_id = user_row.id
        paid_id = paid.id
        sibling_id = sibling.id
        user_row = await db.scalar(select(User).options(selectinload(User.subscriptions)).where(User.id == user_id))
        paid = next(subscription for subscription in user_row.subscriptions if subscription.id == paid_id)

        monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user_row))
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
        monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
        monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
        monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(return_value={}))
        disable = AsyncMock(return_value=outcome == 'success')
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(is_configured=outcome != 'skipped', disable_remnawave_user=disable),
        )
        monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
        original_commit = db.commit
        commit_spy = AsyncMock(wraps=original_commit)
        monkeypatch.setattr(db, 'commit', commit_spy)

        result = await admin_users.update_user_subscription(
            user_id=user_id,
            request=UpdateSubscriptionRequest(action='extend', subscription_id=paid_id, days=3),
            admin=SimpleNamespace(id=1),
            db=db,
        )

        db.expire_all()
        persisted = {
            subscription.id: subscription
            for subscription in (await db.scalars(select(Subscription).order_by(Subscription.id))).all()
        }
        if outcome == 'success':
            assert result.success is True
            assert commit_spy.await_count == 1
            assert persisted[sibling_id].status == 'disabled'
            assert persisted[sibling_id].is_trial is False
            disable.assert_awaited_once_with(
                'real-sibling-panel-uuid',
                user_id=user_id,
                subscription_id=sibling_id,
                action='extend',
            )
        else:
            assert result.success is False
            assert commit_spy.await_count == 0
            assert persisted[sibling_id].status == 'trial'
            assert persisted[sibling_id].is_trial is True


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'action'), UNIFIED_MUTATION_CASES)
async def test_every_unified_action_route_stages_exact_target_then_panel_then_one_commit(
    monkeypatch, user, subscription, db, case_key, action
):
    """Each public branch must preserve its response and use the selected subscription UUID."""
    await _configure_unified_route_action(monkeypatch, db, user, subscription, action)
    events: list[tuple[str, object]] = []
    original_subscription = dict(vars(subscription))

    async def sync(_db, _user, target, *, action: str, **_kwargs):
        if action == 'create':
            assert target.local_record_staged is True
            assert target in user.subscriptions
        else:
            assert dict(vars(subscription)) != original_subscription
        events.append(('local', action))
        events.append(('panel', (target.id, target.remnawave_uuid, action)))

    async def commit():
        events.append(('commit', None))

    if action == 'reset':

        async def reset_with_panel(_db, _user, target, *, commit):
            assert commit is False
            target.status = 'disabled'
            events.append(('local', 'reset'))
            events.append(('panel', (target.id, target.remnawave_uuid, 'reset')))
            return {'panel_disabled': True}

        monkeypatch.setattr('app.services.subscription_service.reset_subscription_with_panel', reset_with_panel)
    else:
        monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    db.commit.side_effect = commit

    request = _unified_request(action, subscription)
    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=request,
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert result.message == _unified_success_message(action, request)
    assert events == [
        ('local', action),
        ('panel', (23, 'subscription-level-uuid', action)),
        ('commit', None),
    ]
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'action'), UNIFIED_MUTATION_CASES)
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_unified_mandatory_action_typed_failure_rolls_back_once(
    monkeypatch, user, subscription, db, case_key, action, failure
):
    """Every unified action must inherit the same typed fail-closed transaction boundary."""
    sync = AsyncMock(side_effect=failure)
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)

    saved = await admin_users._sync_and_commit_admin_mutation(db, user, subscription, action=action)

    assert saved is False
    sync.assert_awaited_once_with(db, user, subscription, reset_traffic=False, action=action)
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', 'action'), UNIFIED_MUTATION_CASES)
async def test_unified_mandatory_action_success_orders_panel_before_one_commit(
    monkeypatch, user, subscription, db, case_key, action
):
    """Removing or moving the final commit would violate every unified route contract."""
    events: list[tuple[str, object]] = []

    async def sync(_db, _user, exact_subscription, *, reset_traffic, action):
        events.append(('panel', (exact_subscription.id, exact_subscription.remnawave_uuid, action)))

    async def commit():
        events.append(('commit', None))

    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', sync)
    db.commit.side_effect = commit

    saved = await admin_users._sync_and_commit_admin_mutation(db, user, subscription, action=action)

    assert saved is True
    assert events == [('panel', (23, 'subscription-level-uuid', action)), ('commit', None)]
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_trial_reset_requires_one_authoritative_complete_panel_wipe(monkeypatch, user, subscription, db, failure):
    """Mixed or total panel wipe failure must not stage or commit a partial trial reset."""
    subscription.is_trial = True
    subscription.is_active = False
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
    legacy_disable = AsyncMock()
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', legacy_disable)

    async def strict_wipe(_db, targets, *, require_all_panel_success=False):
        assert targets == [subscription]
        assert require_all_panel_success is True
        raise failure

    monkeypatch.setattr('app.database.crud.subscription.wipe_trial_subscriptions', strict_wipe)

    result = await admin_users.reset_user_trial(
        user_id=user.id,
        request=admin_users.ResetTrialRequest(),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    legacy_disable.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_reset_success_uses_one_panel_operation_and_one_commit(monkeypatch, user, subscription, db):
    """Reintroducing the legacy pre-disable would double-mutate the same panel identity."""
    subscription.is_trial = True
    subscription.is_active = False
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
    legacy_disable = AsyncMock()
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', legacy_disable)
    wipe = AsyncMock(return_value=1)
    monkeypatch.setattr('app.database.crud.subscription.wipe_trial_subscriptions', wipe)

    result = await admin_users.reset_user_trial(
        user_id=user.id,
        request=admin_users.ResetTrialRequest(),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    wipe.assert_awaited_once_with(db, [subscription], require_all_panel_success=True)
    legacy_disable.assert_not_awaited()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('remove_result', [False, RuntimeError('panel down')])
async def test_device_reset_requires_every_exact_target_removal(monkeypatch, user, subscription, db, remove_result):
    """A false or failed removal must not be reported as a complete device reset."""
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=subscription)
    )
    api = SimpleNamespace(
        get_user_devices_all=AsyncMock(return_value={'devices': [{'hwid': 'hw-1'}]}),
        remove_device=AsyncMock(side_effect=remove_result if isinstance(remove_result, Exception) else None),
    )
    if not isinstance(remove_result, Exception):
        api.remove_device.return_value = remove_result

    @asynccontextmanager
    async def get_api_client():
        yield api

    service = SimpleNamespace(get_api_client=get_api_client)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', lambda: service)

    result = await admin_users.reset_user_devices(
        user_id=user.id,
        admin=SimpleNamespace(id=1),
        db=db,
        subscription_id=subscription.id,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    api.get_user_devices_all.assert_awaited_once_with('subscription-level-uuid')
    api.remove_device.assert_awaited_once_with('subscription-level-uuid', 'hw-1')
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_reset_success_uses_exact_identity_and_preserves_response(monkeypatch, user, subscription, db):
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=subscription)
    )
    api = SimpleNamespace(
        get_user_devices_all=AsyncMock(return_value={'devices': [{'hwid': 'hw-1'}, {'hwid': 'hw-2'}]}),
        remove_device=AsyncMock(return_value=True),
    )

    @asynccontextmanager
    async def get_api_client():
        yield api

    monkeypatch.setattr(
        'app.services.remnawave_service.RemnaWaveService',
        lambda: SimpleNamespace(get_api_client=get_api_client),
    )

    result = await admin_users.reset_user_devices(
        user_id=user.id, admin=SimpleNamespace(id=1), db=db, subscription_id=subscription.id
    )

    assert result.success is True
    assert result.message == 'Deleted 2/2 devices'
    assert result.deleted_count == 2
    assert api.remove_device.await_args_list == [
        call('subscription-level-uuid', 'hw-1'),
        call('subscription-level-uuid', 'hw-2'),
    ]
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'delete_result, expected_success',
    [
        (
            SimpleNamespace(
                bot_deleted=False, panel_deleted=False, panel_error='panel unavailable', grace_blocked=False
            ),
            False,
        ),
        (SimpleNamespace(bot_deleted=True, panel_deleted=True, panel_error=None, grace_blocked=False), True),
    ],
)
async def test_full_delete_route_preserves_service_transaction_result_without_extra_commit(
    monkeypatch, user, db, delete_result, expected_success
):
    """The route maps the real deletion boundary result without committing around it a second time."""
    service_call = AsyncMock(return_value=delete_result)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.services.user_service.UserService.delete_user_account', service_call)

    result = await admin_users.full_delete_user(
        user_id=user.id,
        request=admin_users.FullDeleteUserRequest(delete_from_panel=True),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is expected_success
    assert result.deleted_from_bot is delete_result.bot_deleted
    assert result.deleted_from_panel is delete_result.panel_deleted
    assert result.panel_error == delete_result.panel_error
    if expected_success:
        assert result.message == 'User fully deleted from bot and panel'
    else:
        assert 'not saved' in result.message.lower()
    service_call.assert_awaited_once_with(db, user.id, 1, force_panel_delete=True)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_delete_explicit_false_cannot_bypass_mandatory_panel_delete(monkeypatch, user, db):
    user.subscriptions = []
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    delete_account = AsyncMock(
        return_value=SimpleNamespace(
            bot_deleted=True,
            panel_deleted=False,
            panel_error='panel unavailable',
            panel_reason_code=PanelSyncReason.PANEL_API_FAILED.value,
            grace_blocked=False,
        )
    )
    monkeypatch.setattr(UserService, 'delete_user_account', delete_account)

    result = await admin_users.full_delete_user(
        user_id=user.id,
        request=admin_users.FullDeleteUserRequest(delete_from_panel=False),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert result.message == panel_sync_failure_message()
    delete_account.assert_awaited_once_with(db, user.id, 1, force_panel_delete=True)


@pytest.mark.asyncio
async def test_standalone_delete_success_orders_exact_panel_local_stage_and_one_commit(
    monkeypatch, user, subscription, db
):
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    events: list[tuple[str, object]] = []

    async def panel(_user, targets, *, action):
        events.append(('panel', (targets[0].id, targets[0].remnawave_uuid, action)))

    async def local(_db, _user, *, commit):
        events.append(('local', commit))

    async def commit():
        events.append(('commit', None))

    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', panel)
    monkeypatch.setattr(admin_users, 'soft_delete_user', local)
    db.commit.side_effect = commit

    result = await admin_users.delete_user(
        user_id=user.id,
        request=admin_users.DeleteUserRequest(soft_delete=True),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert result.message == 'User soft deleted successfully'
    assert events == [('panel', (23, 'subscription-level-uuid', 'delete_user')), ('local', False), ('commit', None)]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_standalone_delete_typed_failure_preserves_local_user(monkeypatch, user, subscription, db, failure):
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', AsyncMock(side_effect=failure))
    local = AsyncMock()
    monkeypatch.setattr(admin_users, 'soft_delete_user', local)

    result = await admin_users.delete_user(
        user_id=user.id,
        request=admin_users.DeleteUserRequest(soft_delete=True),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    local.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_success_orders_exact_panel_local_stage_and_one_commit(monkeypatch, user, subscription, db):
    subscription.tariff = SimpleNamespace(is_daily=False)
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
    events: list[tuple[str, object]] = []

    async def panel(_user, targets, *, action):
        events.append(('panel', (targets[0].id, targets[0].remnawave_uuid, action)))

    async def deactivate(_db, target, *, commit):
        events.append(('local', (target.id, commit)))

    async def commit():
        events.append(('commit', None))

    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', panel)
    monkeypatch.setattr('app.database.crud.subscription.deactivate_subscription', deactivate)
    db.commit.side_effect = commit

    result = await admin_users.disable_user(user_id=user.id, admin=SimpleNamespace(id=1), db=db)

    assert result.success is True
    assert result.message == 'User disabled successfully'
    assert events == [
        ('panel', (23, 'subscription-level-uuid', 'disable_user')),
        ('local', (23, False)),
        ('commit', None),
    ]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED), PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)],
)
async def test_disable_typed_failure_prevents_local_stage(monkeypatch, user, subscription, db, failure):
    subscription.tariff = SimpleNamespace(is_daily=False)
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
    monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', AsyncMock(side_effect=failure))
    deactivate = AsyncMock()
    monkeypatch.setattr('app.database.crud.subscription.deactivate_subscription', deactivate)

    result = await admin_users.disable_user(user_id=user.id, admin=SimpleNamespace(id=1), db=db)

    assert result.success is False
    assert 'not saved' in result.message.lower()
    deactivate.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_extend_reaches_required_sync_boundary_with_attributable_action(user, subscription, db, monkeypatch):
    """Omitting ``action`` makes the bulk extend handler fail before panel sync."""
    subscription.is_active = True
    user.subscriptions = [subscription]
    observed_actions: list[str] = []

    async def required_sync(_db, _user, _subscription, *, action: str, **_kwargs):
        observed_actions.append(action)
        return {}

    monkeypatch.setattr(admin_bulk_actions, 'extend_subscription', AsyncMock())
    monkeypatch.setattr(admin_bulk_actions, '_sync_subscription_to_panel', required_sync)

    result = await admin_bulk_actions._do_extend_subscription(
        db, user, BulkActionParams(days=7), dry_run=False, sub_override=subscription
    )

    assert result.success is True
    assert observed_actions == ['extend_subscription']


def test_mutation_key_identifies_one_route_action_pair():
    mutation = AdminPanelMutation(
        route='update_user_subscription',
        action='extend',
        mutation_class='extend',
        integration_path='_sync_subscription_to_panel',
        transaction_owner='update_user_subscription',
        multi_tariff_target=PanelSyncTarget.EXACT_SUBSCRIPTION_UUID,
    )

    assert mutation.key == 'update_user_subscription:extend'


def test_multi_tariff_inventory_targets_exact_subscription_uuid_without_user_fallback():
    assert {entry.multi_tariff_target for entry in MANDATORY_ADMIN_PANEL_MUTATIONS} <= {
        PanelSyncTarget.EXACT_SUBSCRIPTION_UUID,
        PanelSyncTarget.EACH_EXACT_SUBSCRIPTION_UUID,
    }
    assert all(
        'user.remnawave_uuid' not in entry.multi_tariff_target.value for entry in MANDATORY_ADMIN_PANEL_MUTATIONS
    )


@pytest.mark.parametrize('reason_code', ['panel-token-secret', 'unknown_reason', object()])
def test_typed_failures_reject_unbounded_reason_values(reason_code):
    with pytest.raises(TypeError, match='PanelSyncReason') as error:
        PanelSyncFailed(reason_code)  # type: ignore[arg-type]

    assert 'panel-token-secret' not in str(error.value)
