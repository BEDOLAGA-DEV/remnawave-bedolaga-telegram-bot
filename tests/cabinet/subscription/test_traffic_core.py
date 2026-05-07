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


@pytest.mark.asyncio
async def test_resolve_package_price_wl_tariff_match(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {50: 12500}
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        price = await tc.resolve_package_price(mock_db, sub, gb=50, kind='wl')
        assert price == 12500


@pytest.mark.asyncio
async def test_resolve_package_price_wl_classic_match(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.get_wl_traffic_topup_price.return_value = 7777

    with patch.object(tc, 'settings', fake_settings):
        price = await tc.resolve_package_price(mock_db, sub, gb=25, kind='wl')
        assert price == 7777


@pytest.mark.asyncio
async def test_resolve_package_price_returns_zero_when_unknown(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {10: 1000}
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.get_wl_traffic_topup_price.return_value = 0

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        price = await tc.resolve_package_price(mock_db, sub, gb=999, kind='wl')
        assert price == 0


@pytest.mark.asyncio
async def test_apply_purchase_db_wl_calls_add_wl_crud(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    add_wl = AsyncMock()
    add_regular = AsyncMock()

    with (
        patch.object(tc, 'add_subscription_wl_traffic', add_wl),
        patch.object(tc, 'add_subscription_traffic', add_regular),
    ):
        await tc.apply_purchase_db(mock_db, sub, gb=50, kind='wl')

    add_wl.assert_awaited_once_with(mock_db, sub, 50)
    add_regular.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_purchase_db_regular_calls_add_regular_crud(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    add_wl = AsyncMock()
    add_regular = AsyncMock()

    with (
        patch.object(tc, 'add_subscription_wl_traffic', add_wl),
        patch.object(tc, 'add_subscription_traffic', add_regular),
    ):
        await tc.apply_purchase_db(mock_db, sub, gb=10, kind='regular')

    add_regular.assert_awaited_once_with(mock_db, sub, 10)
    add_wl.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_purchases_wl_uses_wl_purchase_table(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    captured = {}

    async def _exec(stmt):
        captured['sql'] = str(stmt)
        return MagicMock()

    mock_db.execute = AsyncMock(side_effect=_exec)

    await tc.delete_purchases_for_switch(mock_db, sub, kind='wl')
    assert 'wl_traffic_purchases' in captured['sql'].lower()


@pytest.mark.asyncio
async def test_delete_purchases_regular_uses_regular_purchase_table(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    captured = {}

    async def _exec(stmt):
        captured['sql'] = str(stmt)
        return MagicMock()

    mock_db.execute = AsyncMock(side_effect=_exec)

    await tc.delete_purchases_for_switch(mock_db, sub, kind='regular')
    sql = captured['sql'].lower()
    assert 'traffic_purchases' in sql
    assert 'wl_traffic_purchases' not in sql


@pytest.mark.asyncio
async def test_sync_remnawave_calls_update_when_uuid_present(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    user = make_user()
    sub = make_subscription(remnawave_uuid='sub-uuid')

    fake_service = MagicMock()
    fake_service.update_remnawave_user = AsyncMock()
    fake_service.create_remnawave_user = AsyncMock()
    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False

    with (
        patch.object(tc, 'SubscriptionService', return_value=fake_service),
        patch.object(tc, 'settings', fake_settings),
    ):
        await tc.sync_remnawave_after_purchase(mock_db, sub, user)

    fake_service.update_remnawave_user.assert_awaited_once_with(mock_db, sub)
    fake_service.create_remnawave_user.assert_not_awaited()
