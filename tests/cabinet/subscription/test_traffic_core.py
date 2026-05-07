"""Unit tests for _traffic_core kind-parameterised helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_resolve_packages_wl_tariff_mode_uses_tariff_packages(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {10: 5000, 50: 20000}
    fake_tariff.can_topup_wl_traffic.return_value = True

    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        gbs = sorted(p['gb'] for p in packages)
        assert gbs == [10, 50]


@pytest.mark.asyncio
async def test_resolve_packages_wl_returns_empty_when_globally_disabled(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = False

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        assert packages == []


@pytest.mark.asyncio
async def test_resolve_packages_wl_returns_empty_when_unlimited(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(wl_traffic_limit_gb=0)
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.get_wl_traffic_packages.return_value = [{'gb': 10, 'price': 5000, 'enabled': True}]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        assert packages == []


@pytest.mark.asyncio
async def test_resolve_packages_wl_classic_uses_global_packages(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(wl_traffic_limit_gb=50)
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.get_wl_traffic_packages.return_value = [
        {'gb': 10, 'price': 5000, 'enabled': True},
        {'gb': 0, 'price': 100000, 'enabled': True},
        {'gb': 25, 'price': 9000, 'enabled': False},
        {'gb': 100, 'price': 0, 'enabled': True},
    ]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        gbs = sorted(p['gb'] for p in packages)
        assert gbs == [0, 10]


@pytest.mark.asyncio
async def test_resolve_packages_regular_returns_existing_logic(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.is_traffic_topup_enabled.return_value = True
    fake_settings.get_traffic_topup_packages.return_value = [
        {'gb': 5, 'price': 1000, 'enabled': True},
    ]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='regular')
        assert [p['gb'] for p in packages] == [5]
