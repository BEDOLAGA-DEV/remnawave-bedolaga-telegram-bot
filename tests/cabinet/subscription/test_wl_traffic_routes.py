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
