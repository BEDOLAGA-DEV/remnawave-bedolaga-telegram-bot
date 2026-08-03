"""Load-bearing public-boundary contracts for every direct inventory row."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from app.cabinet.routes import admin_users
from app.config import settings
from app.services.admin_panel_sync import PanelSyncFailed, PanelSyncReason, PanelSyncSkipped
from app.services.user_service import DeleteUserResult, UserService
from tests.cabinet.admin_panel_sync_case_manifest import (
    DIRECT_FAILED_CASES,
    DIRECT_SKIPPED_CASES,
    DIRECT_SUCCESS_CASES,
)


@pytest.fixture
def direct_user():
    subscription = SimpleNamespace(
        id=23,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=30),
        remnawave_uuid='subscription-level-uuid',
        remnawave_short_uuid='short-uuid',
        remnawave_short_id='short-id',
        subscription_url='https://safe.invalid/subscription',
        subscription_crypto_link=None,
        traffic_limit_gb=20,
        traffic_used_gb=1.0,
        device_limit=2,
        connected_squads=[],
        tariff=SimpleNamespace(external_squad_uuid=None, is_daily=False),
        is_active=True,
        is_trial=True,
    )
    user = SimpleNamespace(
        id=17,
        full_name='Contract User',
        username='contract-user',
        telegram_id=1700,
        email='contract@example.test',
        remnawave_uuid='user-level-uuid',
        last_remnawave_sync=None,
        status='active',
        subscriptions=[subscription],
    )
    return user, subscription


@pytest.fixture
def direct_db():
    return AsyncMock()


def _safe_failure(result) -> None:
    assert result.success is False
    assert result.message
    assert 'token' not in result.message.lower()
    assert 'secret-value' not in result.message.lower()


def _assert_bounded_diagnostic(
    logs, *, user_id: int, subscription_id: int, action: str, reason: PanelSyncReason
) -> None:
    expected = {
        'user_id': user_id,
        'subscription_id': subscription_id,
        'action': action,
        'reason_code': reason.value,
    }
    assert any(all(event.get(key) == value for key, value in expected.items()) for event in logs), expected
    serialized = repr(logs)
    for credential in ('secret-value', 'token=', 'https://panel.invalid', 'payload-secret'):
        assert credential not in serialized


async def _device_case(monkeypatch, user, subscription, db, *, reset: bool, outcome: str):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=subscription)
    )
    api = SimpleNamespace(
        get_user_devices_all=AsyncMock(return_value={'devices': [{'hwid': 'hw-1'}]}),
        remove_device=AsyncMock(return_value=outcome == 'success'),
    )
    if outcome == 'failed':
        api.remove_device.side_effect = RuntimeError('secret-value')
    elif outcome == 'skipped':
        subscription.remnawave_uuid = None

    @asynccontextmanager
    async def get_api_client():
        yield api

    service_factory = MagicMock(return_value=SimpleNamespace(get_api_client=get_api_client))
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', service_factory)
    try:
        if reset:
            result = await admin_users.reset_user_devices(
                user_id=user.id, admin=SimpleNamespace(id=1), db=db, subscription_id=subscription.id
            )
        else:
            result = await admin_users.delete_user_device(
                user_id=user.id,
                hwid='hw-1',
                admin=SimpleNamespace(id=1),
                db=db,
                subscription_id=subscription.id,
            )
    except HTTPException as error:
        result = SimpleNamespace(success=False, message=str(error.detail))
    if outcome == 'skipped':
        service_factory.assert_not_called()
        api.get_user_devices_all.assert_not_awaited()
        api.remove_device.assert_not_awaited()
    else:
        if reset:
            api.get_user_devices_all.assert_awaited_once_with('subscription-level-uuid')
        api.remove_device.assert_awaited_once_with('subscription-level-uuid', 'hw-1')
    db.commit.assert_not_awaited()
    if outcome == 'success':
        assert result.success is True
    else:
        _safe_failure(result)
    return result


async def _typed_route_case(monkeypatch, user, subscription, db, *, route: str, outcome: str):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    reason = PanelSyncReason.NOT_CONFIGURED if outcome == 'skipped' else PanelSyncReason.PANEL_API_FAILED
    failure = PanelSyncSkipped(reason) if outcome == 'skipped' else PanelSyncFailed(reason)
    boundary = AsyncMock(return_value=None)
    if outcome != 'success':
        boundary.side_effect = failure
    panel_method = AsyncMock()
    delete_panel_user = AsyncMock()
    if outcome == 'failed':
        panel_method.side_effect = RuntimeError('secret=https://panel.invalid/?token=secret-value')
        delete_panel_user.side_effect = RuntimeError('secret=https://panel.invalid/?token=secret-value')

    @asynccontextmanager
    async def api_context():
        yield SimpleNamespace(delete_user=delete_panel_user)

    get_api_client = MagicMock(side_effect=api_context)
    if outcome in {'skipped', 'failed'}:
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(
                is_configured=outcome == 'failed',
                disable_remnawave_user=panel_method,
                get_api_client=get_api_client,
            ),
        )

    if route == 'delete_user':
        if outcome == 'success':
            monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', boundary)
        local = AsyncMock()
        monkeypatch.setattr(admin_users, 'soft_delete_user', local)
        result = await admin_users.delete_user(
            user_id=user.id,
            request=admin_users.DeleteUserRequest(soft_delete=True),
            admin=SimpleNamespace(id=1),
            db=db,
        )
        if outcome == 'success':
            boundary.assert_awaited_once_with(user, [subscription], action='delete_user')
        if outcome == 'success':
            local.assert_awaited_once_with(db, user, commit=False)
    elif route == 'reset_user_trial':
        monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
        if outcome in {'skipped', 'failed'}:
            monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
        else:
            wipe = AsyncMock(return_value=1)
            if outcome == 'failed':
                wipe.side_effect = failure
            monkeypatch.setattr('app.database.crud.subscription.wipe_trial_subscriptions', wipe)
        result = await admin_users.reset_user_trial(user_id=user.id, admin=SimpleNamespace(id=1), db=db)
        if outcome == 'success':
            wipe.assert_awaited_once_with(db, [subscription], require_all_panel_success=True)
    elif route == 'reset_user_subscription':
        if outcome == 'success':
            monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', boundary)
        monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
        monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
        monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
        result = await admin_users.reset_user_subscription(user_id=user.id, admin=SimpleNamespace(id=1), db=db)
        if outcome == 'success':
            boundary.assert_awaited_once_with(user, [subscription], action='reset_user_subscription')
    else:
        monkeypatch.setattr('app.database.crud.subscription.is_active_paid_subscription', lambda _: False)
        if outcome == 'success':
            monkeypatch.setattr(admin_users, '_require_panel_disable_for_subscriptions', boundary)
        local = AsyncMock()
        monkeypatch.setattr('app.database.crud.subscription.deactivate_subscription', local)
        result = await admin_users.disable_user(user_id=user.id, admin=SimpleNamespace(id=1), db=db)
        if outcome == 'success':
            boundary.assert_awaited_once_with(user, [subscription], action='disable_user')
        if outcome == 'success':
            local.assert_awaited_once_with(db, subscription, commit=False)

    if outcome == 'skipped':
        panel_method.assert_not_awaited()
        delete_panel_user.assert_not_awaited()
        get_api_client.assert_not_called()
    elif outcome == 'failed':
        if route == 'reset_user_trial':
            delete_panel_user.assert_awaited_once_with(subscription.remnawave_uuid)
            panel_method.assert_not_awaited()
        else:
            panel_method.assert_awaited_once_with(
                subscription.remnawave_uuid,
                user_id=user.id,
                subscription_id=subscription.id,
                action={
                    'delete_user': 'delete_user',
                    'reset_user_subscription': 'reset_user_subscription',
                    'disable_user': 'disable_user',
                }[route],
            )

    if outcome == 'success':
        assert result.success is True
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
    else:
        _safe_failure(result)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
    return result


async def _full_delete_case(monkeypatch, user, db, *, outcome: str):
    subscription = user.subscriptions[0]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    if outcome in {'skipped', 'failed'}:
        monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock())
        monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
        monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
        delete_user = AsyncMock(side_effect=RuntimeError('secret=https://panel.invalid/?token=secret-value'))
        panel_api = SimpleNamespace(delete_user=delete_user)

        @asynccontextmanager
        async def get_api_client():
            yield panel_api

        service_factory = MagicMock(
            return_value=SimpleNamespace(
                is_configured=outcome == 'failed',
                get_api_client=get_api_client,
            )
        )
        monkeypatch.setattr(
            'app.services.remnawave_service.RemnaWaveService',
            service_factory,
        )
        fallback_disable = AsyncMock(return_value=False)
        monkeypatch.setattr(
            'app.services.subscription_service.SubscriptionService',
            lambda: SimpleNamespace(disable_remnawave_user=fallback_disable),
        )
        monkeypatch.setattr(
            'app.services.remnawave_webhook_service.RemnaWaveWebhookService.mark_intentional_panel_deletion',
            MagicMock(),
        )
        result = await admin_users.full_delete_user(
            user_id=user.id,
            request=admin_users.FullDeleteUserRequest(delete_from_panel=True),
            admin=SimpleNamespace(id=1),
            db=db,
        )
        _safe_failure(result)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        if outcome == 'skipped':
            delete_user.assert_not_awaited()
            fallback_disable.assert_not_awaited()
        else:
            delete_user.assert_awaited_once_with(subscription.remnawave_uuid)
            fallback_disable.assert_awaited_once_with(
                subscription.remnawave_uuid,
                db=db,
                user_id=user.id,
                subscription_id=subscription.id,
                action='delete_user',
            )
        return result
    result_contract = DeleteUserResult(
        bot_deleted=outcome == 'success',
        panel_deleted=outcome == 'success',
        panel_error=None if outcome != 'failed' else 'panel_api_failed',
        panel_reason_code=None if outcome == 'success' else PanelSyncReason.PANEL_API_FAILED.value,
    )
    boundary = AsyncMock(return_value=result_contract)
    monkeypatch.setattr('app.services.user_service.UserService.delete_user_account', boundary)
    result = await admin_users.full_delete_user(
        user_id=user.id,
        request=admin_users.FullDeleteUserRequest(delete_from_panel=True),
        admin=SimpleNamespace(id=1),
        db=db,
    )
    boundary.assert_awaited_once_with(db, user.id, 1, force_panel_delete=True)
    db.commit.assert_not_awaited()
    if outcome == 'success':
        assert result.success is True
    else:
        _safe_failure(result)
    return result


async def _status_service_case(monkeypatch, user, subscription, db, *, unblock: bool, outcome: str):
    if unblock:
        user.status = 'blocked'
        subscription.status = 'disabled'
    monkeypatch.setattr('app.services.user_service.get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr('app.database.crud.subscription.deactivate_subscription', AsyncMock())
    method = AsyncMock(return_value=outcome == 'success')
    if outcome == 'failed':
        method.side_effect = RuntimeError('secret-value')
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(
            is_configured=outcome != 'skipped',
            **({'update_remnawave_user': method} if unblock else {'disable_remnawave_user': method}),
        ),
    )
    service = UserService()
    result = (
        await service.unblock_user(db, user.id, admin_id=1)
        if unblock
        else await service.block_user(db, user.id, admin_id=1)
    )
    if outcome == 'skipped':
        method.assert_not_awaited()
    elif unblock:
        method.assert_awaited_once_with(db, subscription, commit=False, diagnostic_action='unblock')
    else:
        method.assert_awaited_once_with(
            subscription.remnawave_uuid,
            db=db,
            user_id=user.id,
            subscription_id=subscription.id,
            action='block',
        )
    assert result is (outcome == 'success')
    if outcome == 'success':
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
    else:
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
    return SimpleNamespace(success=result, message='ok' if result else 'not saved')


async def _sync_case(monkeypatch, user, subscription, db, *, outcome: str):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    if outcome == 'skipped':
        subscription.remnawave_uuid = None
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(uuid=subscription.remnawave_uuid)))

    @asynccontextmanager
    async def get_api_client():
        yield api

    service_factory = MagicMock(return_value=SimpleNamespace(is_configured=True, get_api_client=get_api_client))
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', service_factory)
    update = AsyncMock(return_value=SimpleNamespace())
    if outcome == 'failed':
        update.side_effect = RuntimeError('secret-value')
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', update)
    monkeypatch.setattr('app.services.subscription_service.get_traffic_reset_strategy', lambda _: 'no_reset')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _: 2)
    monkeypatch.setattr(type(settings), 'build_remnawave_subscription_username', lambda self, **_: 'contract-user')
    monkeypatch.setattr(type(settings), 'format_remnawave_user_description', lambda self, **_: 'description')
    try:
        result = await admin_users.sync_user_to_panel(
            user_id=user.id,
            subscription_id=subscription.id,
            request=admin_users.SyncToPanelRequest(create_if_missing=False),
            admin=SimpleNamespace(id=1),
            db=db,
        )
    except HTTPException as error:
        result = SimpleNamespace(success=False, message=str(error.detail))
    if outcome == 'success':
        assert result.success is True
        api.get_user_by_uuid.assert_awaited_once_with(subscription.remnawave_uuid)
        db.commit.assert_awaited_once()
    else:
        _safe_failure(result)
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        if outcome == 'skipped':
            service_factory.assert_not_called()
            api.get_user_by_uuid.assert_not_awaited()
    return result


async def _exercise_direct_case(monkeypatch, user, subscription, db, *, case_key: str, outcome: str):
    route = case_key.split(':', 1)[0]
    if route == 'delete_user_device':
        return await _device_case(monkeypatch, user, subscription, db, reset=False, outcome=outcome)
    if route == 'reset_user_devices':
        return await _device_case(monkeypatch, user, subscription, db, reset=True, outcome=outcome)
    if route == 'full_delete_user':
        return await _full_delete_case(monkeypatch, user, db, outcome=outcome)
    if route in {'delete_user', 'reset_user_trial', 'reset_user_subscription', 'disable_user'}:
        return await _typed_route_case(monkeypatch, user, subscription, db, route=route, outcome=outcome)
    if route in {'block_user', 'unblock_user'}:
        return await _status_service_case(
            monkeypatch, user, subscription, db, unblock=route == 'unblock_user', outcome=outcome
        )
    if route == 'sync_user_to_panel':
        return await _sync_case(monkeypatch, user, subscription, db, outcome=outcome)
    raise AssertionError(f'unhandled direct contract row: {case_key}')


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', '_label'), DIRECT_SUCCESS_CASES)
async def test_direct_success_matrix_executes_public_contract(monkeypatch, direct_user, direct_db, case_key, _label):
    user, subscription = direct_user
    with capture_logs():
        result = await _exercise_direct_case(
            monkeypatch, user, subscription, direct_db, case_key=case_key, outcome='success'
        )
    assert result.success is True


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', '_label'), DIRECT_SKIPPED_CASES)
async def test_direct_skipped_matrix_executes_public_contract(monkeypatch, direct_user, direct_db, case_key, _label):
    user, subscription = direct_user
    with capture_logs() as logs:
        result = await _exercise_direct_case(
            monkeypatch, user, subscription, direct_db, case_key=case_key, outcome='skipped'
        )
    assert result.success is False
    action = {
        'delete_user_device': 'delete_device',
        'reset_user_devices': 'reset_devices',
        'full_delete_user': 'delete_user',
        'delete_user': 'delete_user',
        'reset_user_trial': 'reset_trial',
        'reset_user_subscription': 'reset_user_subscription',
        'disable_user': 'disable_user',
        'block_user': 'block',
        'unblock_user': 'unblock',
        'sync_user_to_panel': 'sync_to_panel',
    }[case_key.split(':', 1)[0]]
    reason = (
        PanelSyncReason.MISSING_SUBSCRIPTION_UUID
        if case_key.split(':', 1)[0] in {'delete_user_device', 'reset_user_devices', 'sync_user_to_panel'}
        else PanelSyncReason.NOT_CONFIGURED
    )
    _assert_bounded_diagnostic(
        logs,
        user_id=user.id,
        subscription_id=subscription.id,
        action=action,
        reason=reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(('case_key', '_label'), DIRECT_FAILED_CASES)
async def test_direct_failed_matrix_executes_public_contract(monkeypatch, direct_user, direct_db, case_key, _label):
    user, subscription = direct_user
    with capture_logs() as logs:
        result = await _exercise_direct_case(
            monkeypatch, user, subscription, direct_db, case_key=case_key, outcome='failed'
        )
    assert result.success is False
    action = {
        'delete_user_device': 'delete_device',
        'reset_user_devices': 'reset_devices',
        'full_delete_user': 'delete_user',
        'delete_user': 'delete_user',
        'reset_user_trial': 'reset_trial',
        'reset_user_subscription': 'reset_user_subscription',
        'disable_user': 'disable_user',
        'block_user': 'block',
        'unblock_user': 'unblock',
        'sync_user_to_panel': 'sync_to_panel',
    }[case_key.split(':', 1)[0]]
    _assert_bounded_diagnostic(
        logs,
        user_id=user.id,
        subscription_id=subscription.id,
        action=action,
        reason=PanelSyncReason.PANEL_API_FAILED,
    )


@pytest.mark.asyncio
async def test_sync_from_panel_unconfigured_uses_its_own_safe_503_branch(monkeypatch, direct_user, direct_db):
    user, subscription = direct_user
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.remnawave_service.RemnaWaveService',
        lambda: SimpleNamespace(is_configured=False),
    )

    with capture_logs() as logs:
        with pytest.raises(HTTPException) as raised:
            await admin_users.sync_user_from_panel(
                user_id=user.id,
                subscription_id=subscription.id,
                request=admin_users.SyncFromPanelRequest(),
                admin=SimpleNamespace(id=1),
                db=direct_db,
            )

    assert raised.value.status_code == 503
    assert 'push_sub' not in str(raised.value.detail)
    assert 'secret' not in str(raised.value.detail).lower()
    direct_db.rollback.assert_awaited_once()
    _assert_bounded_diagnostic(
        logs,
        user_id=user.id,
        subscription_id=subscription.id,
        action='sync_from_panel',
        reason=PanelSyncReason.NOT_CONFIGURED,
    )
