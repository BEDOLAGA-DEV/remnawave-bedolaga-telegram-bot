from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database.models import User
from app.webapi.routes import subscriptions, users
from app.webapi.schemas.subscriptions import SubscriptionExtendRequest
from app.webapi.schemas.users import UserSubscriptionCreateRequest


def _build_subscription() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=10,
        user_id=1,
        status='active',
        actual_status='active',
        is_trial=True,
        start_date=now,
        end_date=now + timedelta(days=3),
        traffic_limit_gb=10,
        traffic_used_gb=0.0,
        purchased_traffic_gb=0,
        traffic_reset_at=None,
        device_limit=1,
        autopay_enabled=False,
        autopay_days_before=None,
        subscription_url='https://old',
        subscription_crypto_link='https://old-crypto',
        connected_squads=[],
        remnawave_short_uuid='short',
        tariff_id=None,
        is_daily_paused=False,
        last_daily_charge_at=None,
        updated_at=now,
        created_at=now,
    )


@pytest.mark.anyio('asyncio')
async def test_users_subscription_trial_calls_remnawave_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_user = SimpleNamespace(id=1)
    created_subscription = _build_subscription()
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=None),
        create_remnawave_user=AsyncMock(return_value=SimpleNamespace(uuid='new')),
    )

    monkeypatch.setattr(users, '_get_user_by_id_or_telegram_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(users, 'create_trial_subscription', AsyncMock(return_value=created_subscription))
    monkeypatch.setattr(users, 'SubscriptionService', lambda: service_instance)
    monkeypatch.setattr(users, 'get_user_by_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, '_serialize_user', lambda user: {'id': user.id})

    payload = UserSubscriptionCreateRequest(is_trial=True, duration_days=7, replace_existing=False)
    result = await users.create_user_subscription(user_id=1, payload=payload, _=None, db=SimpleNamespace())

    assert result == {'id': 1}
    service_instance.update_remnawave_user.assert_awaited_once()
    service_instance.create_remnawave_user.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_users_subscription_paid_calls_remnawave_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_user = SimpleNamespace(id=1)
    created_subscription = _build_subscription()
    created_subscription.is_trial = False
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=None),
        create_remnawave_user=AsyncMock(return_value=SimpleNamespace(uuid='new')),
    )

    monkeypatch.setattr(users, '_get_user_by_id_or_telegram_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(users, 'create_paid_subscription', AsyncMock(return_value=created_subscription))
    monkeypatch.setattr(users, 'SubscriptionService', lambda: service_instance)
    monkeypatch.setattr(users, 'get_user_by_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, '_serialize_user', lambda user: {'id': user.id})

    payload = UserSubscriptionCreateRequest(
        is_trial=False,
        duration_days=30,
        replace_existing=False,
    )
    result = await users.create_user_subscription(user_id=1, payload=payload, _=None, db=SimpleNamespace())

    assert result == {'id': 1}
    service_instance.update_remnawave_user.assert_awaited_once()
    service_instance.create_remnawave_user.assert_awaited_once()


def test_users_search_filter_adds_internal_id_for_int32() -> None:
    query = users._apply_search_filter(select(User), '123')
    where_expr = query._where_criteria[0]

    assert len(list(where_expr.clauses)) == 6


def test_users_search_filter_skips_internal_id_for_out_of_int32() -> None:
    query = users._apply_search_filter(select(User), str(2**40))
    where_expr = query._where_criteria[0]

    assert len(list(where_expr.clauses)) == 5


@pytest.mark.anyio('asyncio')
async def test_subscriptions_extend_calls_remnawave_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = _build_subscription()
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=SimpleNamespace(uuid='ok')),
        create_remnawave_user=AsyncMock(return_value=None),
    )
    get_subscription_mock = AsyncMock(side_effect=[subscription, subscription])

    monkeypatch.setattr(subscriptions, '_get_subscription', get_subscription_mock)
    monkeypatch.setattr(subscriptions, 'extend_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscriptions, 'SubscriptionService', lambda: service_instance)
    monkeypatch.setattr(subscriptions, '_serialize_subscription', lambda sub: {'id': sub.id})

    payload = SubscriptionExtendRequest(days=30)
    result = await subscriptions.extend_subscription_endpoint(
        subscription_id=subscription.id,
        payload=payload,
        _=None,
        db=SimpleNamespace(),
    )

    assert result == {'id': subscription.id}
    service_instance.update_remnawave_user.assert_awaited_once()
    service_instance.create_remnawave_user.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_subscriptions_extend_rolls_back_when_sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = _build_subscription()
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=None),
        create_remnawave_user=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(subscriptions, '_get_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscriptions, 'extend_subscription', AsyncMock(return_value=subscription))
    restore_mock = AsyncMock()
    monkeypatch.setattr(subscriptions, '_restore_subscription_state', restore_mock)
    monkeypatch.setattr(subscriptions, 'SubscriptionService', lambda: service_instance)

    payload = SubscriptionExtendRequest(days=30)
    with pytest.raises(HTTPException) as error:
        await subscriptions.extend_subscription_endpoint(
            subscription_id=subscription.id,
            payload=payload,
            _=None,
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 500
    restore_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_subscriptions_extend_returns_500_when_rollback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = _build_subscription()
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=None),
        create_remnawave_user=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(subscriptions, '_get_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscriptions, 'extend_subscription', AsyncMock(return_value=subscription))
    restore_mock = AsyncMock(side_effect=RuntimeError('rollback failed'))
    monkeypatch.setattr(subscriptions, '_restore_subscription_state', restore_mock)
    monkeypatch.setattr(subscriptions, 'SubscriptionService', lambda: service_instance)

    payload = SubscriptionExtendRequest(days=30)
    with pytest.raises(HTTPException) as error:
        await subscriptions.extend_subscription_endpoint(
            subscription_id=subscription.id,
            payload=payload,
            _=None,
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 500
    restore_mock.assert_awaited_once()


@pytest.mark.anyio('asyncio')
async def test_users_patch_subscription_delegates_to_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH /users/{id}/subscription is a documented alias for POST and must route
    through the same handler. Without this test a refactor of the delegation chain could
    silently break the PATCH endpoint while the POST tests stay green."""
    fake_user = SimpleNamespace(id=42)
    created_subscription = _build_subscription()
    service_instance = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=SimpleNamespace(uuid='ok')),
        create_remnawave_user=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(users, '_get_user_by_id_or_telegram_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(users, 'create_trial_subscription', AsyncMock(return_value=created_subscription))
    monkeypatch.setattr(users, 'SubscriptionService', lambda: service_instance)
    monkeypatch.setattr(users, 'get_user_by_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, '_serialize_user', lambda user: {'id': user.id})

    payload = UserSubscriptionCreateRequest(is_trial=True, duration_days=7, replace_existing=False)
    result = await users.patch_user_subscription(user_id=42, payload=payload, _=None, db=SimpleNamespace())

    assert result == {'id': 42}
    service_instance.update_remnawave_user.assert_awaited_once()


def test_users_patch_subscription_route_returns_201() -> None:
    """The PATCH-as-upsert alias is intentionally annotated 201 (not the REST-typical 200)
    so external clients can rely on the same status code as POST. Pin this to catch any
    future change that drifts the contract."""
    patch_route = next(
        route
        for route in users.router.routes
        if getattr(route, 'path', None) == '/{user_id}/subscription' and 'PATCH' in route.methods
    )
    assert patch_route.status_code == 201


@pytest.mark.anyio('asyncio')
async def test_users_subscription_replace_existing_restores_on_sync_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When replace_existing=True and Remnawave sync fails, the user's prior subscription
    state must be restored from the pre-mutation snapshot — NOT hard-deleted. The earlier
    two create-sync tests both used replace_existing=False, which exercises the
    _delete_subscription_if_exists branch; this test pins the snapshot-restore branch."""
    fake_user = SimpleNamespace(id=7)
    existing_subscription = _build_subscription()
    replaced_subscription = _build_subscription()
    replaced_subscription.id = existing_subscription.id

    sync_failure_service = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=None),
        create_remnawave_user=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(users, '_get_user_by_id_or_telegram_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, 'get_subscription_by_user_id', AsyncMock(return_value=existing_subscription))
    monkeypatch.setattr(users, 'replace_subscription', AsyncMock(return_value=replaced_subscription))
    monkeypatch.setattr(users, 'SubscriptionService', lambda: sync_failure_service)
    monkeypatch.setattr(users, 'get_user_by_id', AsyncMock(return_value=fake_user))
    monkeypatch.setattr(users, '_serialize_user', lambda user: {'id': user.id})

    restore_mock = AsyncMock()
    delete_mock = AsyncMock()
    monkeypatch.setattr(users, '_restore_subscription_state', restore_mock)
    monkeypatch.setattr(users, '_delete_subscription_if_exists', delete_mock)

    payload = UserSubscriptionCreateRequest(is_trial=True, duration_days=7, replace_existing=True)

    with pytest.raises(HTTPException) as error:
        await users.create_user_subscription(user_id=7, payload=payload, _=None, db=SimpleNamespace())

    assert error.value.status_code == 500
    restore_mock.assert_awaited_once()
    restore_args = restore_mock.await_args
    assert restore_args.args[1] == existing_subscription.id
    delete_mock.assert_not_awaited()
