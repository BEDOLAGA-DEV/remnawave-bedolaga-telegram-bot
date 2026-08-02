from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from app.cabinet.routes import admin_bulk_actions, admin_users
from app.cabinet.schemas.bulk_actions import BulkActionParams
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import settings
from app.services.admin_panel_sync import (
    MANDATORY_ADMIN_PANEL_MUTATIONS,
    AdminPanelMutation,
    PanelSyncFailed,
    PanelSyncReason,
    PanelSyncSkipped,
    PanelSyncTarget,
    panel_sync_failure_message,
)
from app.services.remnawave_service import RemnaWaveService


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
async def test_sync_not_configured_raises_skipped_without_commit(monkeypatch, user, subscription, db):
    """Removing the typed skip would let callers commit a mutation without a panel sync."""
    monkeypatch.setattr(RemnaWaveService, 'is_configured', False)

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
async def test_multi_tariff_missing_subscription_uuid_never_uses_user_uuid(
    configured_panel, user, subscription, db
):
    """Falling back to the user UUID would reset a sibling tariff's panel user."""
    subscription.remnawave_uuid = None
    user.remnawave_uuid = 'wrong-user-level-uuid'

    with pytest.raises(PanelSyncSkipped) as raised:
        await admin_users._sync_subscription_to_panel(
            db, user, subscription, reset_traffic=True, action='reset'
        )

    assert raised.value.reason_code is PanelSyncReason.MISSING_SUBSCRIPTION_UUID
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_sync_does_not_commit(configured_panel, user, subscription, db):
    """Restoring a helper-level commit would split the caller-owned transaction."""
    changes = await admin_users._sync_subscription_to_panel(db, user, subscription, action='extend')

    assert changes == {'action': 'updated'}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_extend_reaches_required_sync_boundary_with_attributable_action(
    monkeypatch, user, subscription, db
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
        request=UpdateSubscriptionRequest(action='extend', days=7),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is True
    assert observed_actions == ['extend']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [
        PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED),
        PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED),
    ],
)
async def test_admin_extend_panel_failure_rolls_back_without_false_success(monkeypatch, user, subscription, db, failure):
    """The route, not a nested helper, owns the one final transaction commit."""
    subscription.is_active = True
    user.subscriptions = [subscription]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock())
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(side_effect=failure))

    result = await admin_users.update_user_subscription(
        user_id=user.id,
        request=UpdateSubscriptionRequest(action='extend', days=7),
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert result.success is False
    assert 'not saved' in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


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
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock()
    )
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock()
    )
    monkeypatch.setattr(
        'app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock()
    )
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
