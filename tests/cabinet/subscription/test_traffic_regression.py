"""Regression coverage for regular traffic endpoints after _traffic_core refactor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_regular_packages_endpoint_still_returns(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import traffic as t

    user = make_user()
    sub = make_subscription()

    with (
        patch.object(t, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(
            t,
            'resolve_traffic_packages',
            AsyncMock(return_value=[{'gb': 5, 'price': 1000, 'is_unlimited': False}]),
        ),
    ):
        result = await t.get_traffic_packages(user=user, db=mock_db, subscription_id=None)

    assert len(result) == 1
    assert result[0].gb == 5


@pytest.mark.asyncio
async def test_regular_purchase_calls_apply_purchase_db_with_regular_kind(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import traffic as t
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=10_000_000)
    sub = make_subscription()
    sub.user_id = user.id

    apply_db = AsyncMock()

    with (
        patch.object(t, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(t, 'resolve_package_price', AsyncMock(return_value=1000)),
        patch.object(t, '_apply_addon_discount', return_value={'discounted': 1000, 'discount': 0, 'percent': 0}),
        patch.object(t, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(t, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(t, 'apply_purchase_db', apply_db),
        patch.object(t, 'reactivate_subscription', AsyncMock()),
        patch.object(t, 'create_transaction', AsyncMock()),
        patch.object(t, 'sync_remnawave_after_purchase', AsyncMock()),
        patch.object(t, 'calculate_prorated_price', return_value=(1000, 30)),
    ):
        await t.purchase_traffic(
            request=TrafficPurchaseRequest(gb=5),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    apply_db.assert_awaited_once()
    call_kwargs = apply_db.await_args.kwargs
    assert call_kwargs['kind'] == 'regular'
    assert call_kwargs['gb'] == 5
