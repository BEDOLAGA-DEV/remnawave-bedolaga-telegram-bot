from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.webapi.routes import subscriptions, users
from app.webapi.schemas.subscriptions import SubscriptionExtendRequest
from app.webapi.schemas.users import UserSubscriptionCreateRequest


def _build_subscription() -> SimpleNamespace:
    now = datetime.utcnow()
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

