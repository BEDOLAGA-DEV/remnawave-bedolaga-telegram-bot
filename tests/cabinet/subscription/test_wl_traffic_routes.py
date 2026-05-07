"""Integration tests for /cabinet/subscription/wl-* endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_wl_packages_returns_resolved_list(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(
            wt,
            'resolve_traffic_packages',
            AsyncMock(return_value=[{'gb': 10, 'price': 5000, 'is_unlimited': False}]),
        ),
    ):
        result = await wt.get_wl_traffic_packages(user=user, db=mock_db, subscription_id=None)

    assert len(result) == 1
    pkg = result[0]
    assert pkg.gb == 10
    assert pkg.price_kopeks == 5000
    assert pkg.price_rubles == 50.0
    assert pkg.is_unlimited is False


@pytest.mark.asyncio
async def test_wl_packages_returns_empty_when_no_subscription(make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=None)):
        result = await wt.get_wl_traffic_packages(user=make_user(), db=mock_db, subscription_id=None)

    assert result == []


@pytest.mark.asyncio
async def test_wl_purchase_success(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=10_000_000)
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=4000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 4000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'apply_purchase_db', AsyncMock()),
        patch.object(wt, 'reactivate_subscription', AsyncMock()),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
        patch.object(wt, 'calculate_prorated_price', return_value=(4000, 30)),
    ):
        sub.wl_traffic_limit_gb = 60
        result = await wt.purchase_wl_traffic(
            request=TrafficPurchaseRequest(gb=10),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['gb_added'] == 10
    assert result['new_wl_traffic_limit_gb'] == 60
    assert result['amount_paid_kopeks'] == 4000


@pytest.mark.asyncio
async def test_wl_purchase_rejects_trial(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(is_trial=True)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_wl_purchase_rejects_unlimited(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=0)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_wl_purchase_insufficient_saves_cart_and_402(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=100)
    sub = make_subscription()

    save_cart = AsyncMock()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=10000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 10000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'calculate_prorated_price', return_value=(10000, 30)),
        patch.object(wt.user_cart_service, 'save_user_cart', save_cart),
    ):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )

    assert exc.value.status_code == 402
    save_cart.assert_awaited_once()
    cart = save_cart.await_args[0][1]
    assert cart['cart_mode'] == 'add_wl_traffic'
    assert cart['traffic_gb'] == 10
    assert cart['source'] == 'cabinet'
