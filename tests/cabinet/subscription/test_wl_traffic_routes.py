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


@pytest.mark.asyncio
async def test_wl_switch_upgrade_charges_diff(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_limit_gb=50, wl_purchased_traffic_gb=0)
    sub.user_id = user.id

    fake_settings = MagicMock()
    fake_settings.get_wl_traffic_price.side_effect = lambda gb: {50: 4000, 100: 9000}.get(gb, 0)
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.format_price = lambda k: f'{k / 100:.2f} ₽'

    with (
        patch.object(wt, 'settings', fake_settings),
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 5000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'calculate_prorated_price', return_value=(5000, 30)),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'delete_purchases_for_switch', AsyncMock()),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'sync_remnawise_after_purchase', AsyncMock(), create=True),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
    ):
        result = await wt.switch_wl_traffic(
            request=TrafficPurchaseRequest(gb=100),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['old_wl_traffic_gb'] == 50
    assert result['new_wl_traffic_gb'] == 100
    assert result['charged_kopeks'] == 5000


@pytest.mark.asyncio
async def test_wl_switch_downgrade_no_charge(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_purchased_traffic_gb=0)
    fake_settings = MagicMock()
    fake_settings.get_wl_traffic_price.side_effect = lambda gb: {100: 9000, 50: 4000}.get(gb, 0)

    with (
        patch.object(wt, 'settings', fake_settings),
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 0, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'delete_purchases_for_switch', AsyncMock()),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
    ):
        result = await wt.switch_wl_traffic(
            request=TrafficPurchaseRequest(gb=50),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['charged_kopeks'] == 0
    assert result['new_wl_traffic_gb'] == 50


@pytest.mark.asyncio
async def test_wl_switch_same_gb_400(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=50)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.switch_wl_traffic(
                request=TrafficPurchaseRequest(gb=50),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_wl_reset_success(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_used_gb=12.5)
    sub.user_id = user.id

    fake_api = MagicMock()
    fake_api.get_user_by_username = AsyncMock(return_value=MagicMock(uuid='wl-uuid'))
    fake_api.reset_user_traffic = AsyncMock()

    fake_remnawave = MagicMock()
    fake_remnawave.get_api_client = MagicMock(return_value=AsyncMock())
    fake_remnawave.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_remnawave.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_subscription_service = MagicMock()
    fake_subscription_service._build_wl_username = MagicMock(return_value=('wl_user', 'wl_user_legacy'))

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=5000),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'RemnaWaveService', return_value=fake_remnawave),
        patch.object(wt, 'SubscriptionService', return_value=fake_subscription_service),
    ):
        result = await wt.reset_wl_traffic(
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['new_wl_traffic_used_gb'] == 0
    assert sub.wl_traffic_used_gb == 0.0
    fake_api.reset_user_traffic.assert_awaited_once_with('wl-uuid')


@pytest.mark.asyncio
async def test_wl_reset_insufficient_balance_402(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100)
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=10000),
    ):
        with pytest.raises(HTTPException) as exc:
            await wt.reset_wl_traffic(user=user, db=mock_db, subscription_id=None)
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_wl_reset_remnawave_failure_is_non_fatal(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_used_gb=8.0)

    fake_api = MagicMock()
    fake_api.get_user_by_username = AsyncMock(return_value=MagicMock(uuid='wl-uuid'))
    fake_api.reset_user_traffic = AsyncMock(side_effect=Exception('upstream down'))

    fake_remnawave = MagicMock()
    fake_remnawave.get_api_client = MagicMock(return_value=AsyncMock())
    fake_remnawave.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_remnawave.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_subscription_service = MagicMock()
    fake_subscription_service._build_wl_username = MagicMock(return_value=('p', 'l'))

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=5000),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'RemnaWaveService', return_value=fake_remnawave),
        patch.object(wt, 'SubscriptionService', return_value=fake_subscription_service),
    ):
        result = await wt.reset_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['success'] is True
    assert sub.wl_traffic_used_gb == 0.0


@pytest.mark.asyncio
async def test_wl_refresh_success_returns_panel_data(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_traffic_used_gb=0.0)

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(
            wt,
            'refresh_used_from_panel',
            AsyncMock(return_value={'used_traffic_gb': 5.0, 'used_traffic_bytes': 1024**3 * 5, 'lifetime_used_traffic_gb': 5.0}),
        ),
        patch.object(wt.cache, 'set', AsyncMock()),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['success'] is True
    assert result['source'] == 'remnawave'
    assert result['wl_traffic_used_gb'] == 5.0
    assert result['wl_traffic_limit_gb'] == 100


@pytest.mark.asyncio
async def test_wl_refresh_rate_limited_returns_cached(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=50)

    cached_payload = {
        'wl_traffic_used_gb': 3.0,
        'wl_traffic_used_bytes': 1024**3 * 3,
        'wl_traffic_limit_gb': 50,
        'wl_traffic_limit_bytes': 1024**3 * 50,
        'wl_traffic_used_percent': 6.0,
        'is_unlimited': False,
        'lifetime_used_bytes': 0,
        'lifetime_used_gb': 0.0,
    }

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=True)),
        patch.object(wt.cache, 'get', AsyncMock(return_value=cached_payload)),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['cached'] is True
    assert result['rate_limited'] is True
    assert result['wl_traffic_used_gb'] == 3.0


@pytest.mark.asyncio
async def test_wl_refresh_no_panel_data_returns_database(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_traffic_used_gb=2.0)

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(wt, 'refresh_used_from_panel', AsyncMock(return_value=None)),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['source'] == 'database'
    assert result['wl_traffic_used_gb'] == 2.0


@pytest.mark.asyncio
async def test_wl_save_cart_persists_correct_mode(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription()
    sub.status = 'active'

    save_cart = AsyncMock()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=4000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 4000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'calculate_prorated_price', return_value=(4000, 30)),
        patch.object(wt.user_cart_service, 'save_user_cart', save_cart),
    ):
        result = await wt.save_wl_traffic_cart(
            request=TrafficPurchaseRequest(gb=10),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result == {'success': True, 'cart_saved': True}
    save_cart.assert_awaited_once()
    cart_arg = save_cart.await_args[0][1]
    assert cart_arg['cart_mode'] == 'add_wl_traffic'


@pytest.mark.asyncio
async def test_auto_purchase_handles_add_wl_traffic_cart(make_subscription, make_user, mock_db):
    """The auto-purchase service runs when a cabinet 402 cart is consumed."""
    import importlib

    auto = importlib.import_module('app.services.subscription_auto_purchase_service')

    if not hasattr(auto, 'process_cart'):
        pytest.skip('process_cart not present in this version')

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription()
    cart = {
        'cart_mode': 'add_wl_traffic',
        'subscription_id': sub.id,
        'traffic_gb': 25,
        'price_kopeks': 5000,
        'base_price_kopeks': 5000,
        'discount_percent': 0,
        'source': 'cabinet',
        'description': 'Докупка 25 ГБ WL-трафика',
    }

    with (
        patch.object(auto, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(auto, 'add_subscription_wl_traffic', AsyncMock()),
        patch.object(auto, 'reactivate_subscription', AsyncMock()),
        patch.object(auto, 'create_transaction', AsyncMock()),
    ):
        result = await auto.process_cart(mock_db, user, sub, cart)

    assert result['success'] is True
    assert result['mode'] == 'add_wl_traffic'
