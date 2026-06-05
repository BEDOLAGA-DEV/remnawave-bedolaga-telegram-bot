"""Regression tests for the inactive-target-tariff guard in auto-extend paths.

Background — Telegram bug report #595885
----------------------------------------
A multi-tariff user had an expired subscription bound to a tariff that the
operator had marked `is_active=False` in admin ("ТЕСТОВЫЙ" / `Неактивен`).
After the user topped up their balance, the bot's
`try_auto_extend_expired_after_topup` ran, picked the expired subscription,
loaded the (inactive) tariff, computed `period_days = tariff.get_shortest_period()`,
and silently charged 300₽ for a 1-day extension on a tariff the operator
explicitly deactivated.

Same gap existed in cart-driven `_prepare_auto_extend_context` — it validated
`period_prices` membership but never checked `is_active`.

These tests pin the guard so the regression cannot return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus


@pytest.mark.asyncio
async def test_try_auto_extend_skips_when_target_tariff_is_inactive(monkeypatch) -> None:
    """Operator deactivated the tariff → user must NOT be billed even though
    the subscription is expired and the balance is sufficient."""
    from app.services import subscription_auto_purchase_service as svc

    inactive_tariff = SimpleNamespace(
        id=99,
        name='ТЕСТОВЫЙ',
        is_active=False,
        get_shortest_period=lambda: 1,
    )
    subscription = SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        end_date=datetime.now(UTC) - timedelta(hours=2),
        tariff=inactive_tariff,
    )

    # Single-tariff branch (multi-tariff branch is exercised by the next test).
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)

    async def fake_get_subscription_by_user_id(_db, _user_id):
        return subscription

    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        fake_get_subscription_by_user_id,
    )

    # If the guard fails to trip, the function will reach the pricing engine /
    # balance-deduction path and call these. We assert below that they were
    # never reached — that is exactly the bug we're guarding against.
    pricing_engine_spy = AsyncMock()
    subtract_balance_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract_balance_spy)

    user = MagicMock()
    user.id = 7
    user.balance_kopeks = 1_000_000
    db = AsyncMock()

    result = await svc.try_auto_extend_expired_after_topup(db, user, bot=None)

    assert result is False, 'must refuse to extend onto an inactive tariff'
    pricing_engine_spy.assert_not_called()
    subtract_balance_spy.assert_not_called()


@pytest.mark.asyncio
async def test_try_auto_extend_skips_inactive_tariff_in_multi_tariff_mode(monkeypatch) -> None:
    """Same guard for the multi-tariff selection branch."""
    from app.services import subscription_auto_purchase_service as svc

    inactive_tariff = SimpleNamespace(
        id=99,
        name='ТЕСТОВЫЙ',
        is_active=False,
        get_shortest_period=lambda: 1,
    )
    expired_sub = SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        end_date=datetime.now(UTC) - timedelta(hours=2),
        tariff=inactive_tariff,
    )

    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: True)

    async def fake_get_all_subs(_db, _user_id):
        return [expired_sub]

    monkeypatch.setattr(
        'app.database.crud.subscription.get_all_subscriptions_by_user_id',
        fake_get_all_subs,
    )

    pricing_engine_spy = AsyncMock()
    subtract_balance_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract_balance_spy)

    user = MagicMock()
    user.id = 7
    user.balance_kopeks = 1_000_000
    db = AsyncMock()

    result = await svc.try_auto_extend_expired_after_topup(db, user, bot=None)

    assert result is False
    pricing_engine_spy.assert_not_called()
    subtract_balance_spy.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_auto_extend_context_skips_inactive_target_tariff(monkeypatch) -> None:
    """Cart-driven autopay path — the inactive-target guard belongs there too."""
    from app.services import subscription_auto_purchase_service as svc

    inactive_tariff = SimpleNamespace(
        id=99,
        name='ТЕСТОВЫЙ',
        is_active=False,
        is_daily=False,
        period_prices={1: 30_000, 30: 300_000},
    )
    subscription = SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        end_date=datetime.now(UTC) - timedelta(hours=2),
        tariff=inactive_tariff,
        tariff_id=99,
        device_limit=1,
        traffic_limit_gb=0,
    )

    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(type(svc.settings), 'is_tariffs_mode', lambda _self: False)

    async def fake_get_subscription_by_user_id(_db, _user_id):
        return subscription

    async def fake_get_tariff_by_id(_db, _tariff_id):
        return inactive_tariff

    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        fake_get_subscription_by_user_id,
    )
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id',
        fake_get_tariff_by_id,
    )

    # The guard short-circuits before any pricing call — these spies must stay clean.
    pricing_engine_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)

    user = MagicMock()
    user.id = 7
    db = AsyncMock()
    cart_data = {'period_days': 1, 'tariff_id': 99, 'subscription_id': 1}

    ctx = await svc._prepare_auto_extend_context(db, user, cart_data)

    assert ctx is None, 'cart-driven auto-extend must refuse inactive target tariff'
    pricing_engine_spy.assert_not_called()
