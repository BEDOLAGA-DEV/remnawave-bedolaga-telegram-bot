import itertools

import pytest

from app.services.pricing_engine import PricingEngine, RenewalPricing


def test_renewal_pricing_is_frozen():
    p = RenewalPricing(
        base_price=29000,
        servers_price=5000,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=34000,
        period_days=30,
        is_tariff_mode=False,
    )
    assert p.final_total == 34000
    with pytest.raises(AttributeError):
        p.final_total = 0


class TestApplyDiscount:
    def test_basic_discount(self):
        assert PricingEngine.apply_discount(10000, 20) == 8000

    def test_zero_discount(self):
        assert PricingEngine.apply_discount(10000, 0) == 10000

    def test_full_discount(self):
        assert PricingEngine.apply_discount(10000, 100) == 0

    def test_negative_clamped(self):
        assert PricingEngine.apply_discount(10000, -5) == 10000

    def test_over_100_clamped(self):
        assert PricingEngine.apply_discount(10000, 150) == 0

    def test_integer_floor_division(self):
        assert PricingEngine.apply_discount(99900, 30) == 69930


class TestStackedDiscounts:
    def test_group_then_offer(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 10)
        assert final == 7200
        assert g_val == 2000
        assert o_val == 800

    def test_no_discounts(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 0, 0)
        assert final == 10000
        assert g_val == 0
        assert o_val == 0

    def test_only_offer(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 0, 15)
        assert final == 8500
        assert g_val == 0
        assert o_val == 1500

    def test_only_group(self):
        result, gd, od = PricingEngine.apply_stacked_discounts(10000, 20, 0)
        assert result == 8000
        assert gd == 2000
        assert od == 0

    def test_both_100_percent(self):
        result, gd, od = PricingEngine.apply_stacked_discounts(10000, 100, 100)
        assert result == 0
        assert gd == 10000
        assert od == 0  # offer discount on 0 is 0


from unittest.mock import AsyncMock, MagicMock, patch


class TestPeriodDaysValidation:
    @pytest.mark.asyncio
    async def test_negative_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, -1)

    @pytest.mark.asyncio
    async def test_zero_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, 0)

    @pytest.mark.asyncio
    async def test_float_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, 30.0)


_server_id_seq = itertools.count(1)


def _make_server(
    price_kopeks=5000, is_available=True, is_full=False, allowed_promo_groups=None, server_id=None, squad_uuid=None
):
    if server_id is None:
        server_id = next(_server_id_seq)
    server = MagicMock()
    server.id = server_id
    server.squad_uuid = squad_uuid
    server.price_kopeks = price_kopeks
    server.is_available = is_available
    server.is_full = is_full
    server.allowed_promo_groups = allowed_promo_groups or []
    return server


class TestCalculateServersPrice:
    @pytest.mark.asyncio
    async def test_available_server(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 5000
        assert len(details) == 1
        assert details[0]['price'] == 5000

    @pytest.mark.asyncio
    async def test_unavailable_server_uses_real_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=7000, is_available=False, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 7000  # NOT 0!
        assert details[0]['status'] == 'unavailable'

    @pytest.mark.asyncio
    async def test_full_server_uses_real_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=3000, is_full=True, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 3000  # NOT 0!

    @pytest.mark.asyncio
    async def test_server_not_found_zero_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[]):
            total, details = await engine._calculate_servers_price(['uuid-orphan'], db, promo_group_id=None)
        assert total == 0
        assert details[0]['status'] == 'not_found'

    @pytest.mark.asyncio
    async def test_multiple_servers(self):
        engine = PricingEngine()
        db = AsyncMock()
        s1 = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        s2 = _make_server(price_kopeks=3000, is_available=False, squad_uuid='uuid-2')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[s1, s2]):
            total, details = await engine._calculate_servers_price(['uuid-1', 'uuid-2'], db, promo_group_id=None)
        assert total == 8000

    @pytest.mark.asyncio
    async def test_server_ids_and_prices_alignment_with_not_found(self):
        """Verify server_ids and servers_individual_prices have same length when some servers are not found."""
        engine = PricingEngine()
        db = AsyncMock()
        s1 = _make_server(price_kopeks=5000, server_id=10, squad_uuid='uuid-1')
        s3 = _make_server(price_kopeks=3000, server_id=30, squad_uuid='uuid-3')
        # uuid-orphan not in batch result — should be excluded from BOTH lists
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[s1, s3]):
            total, details = await engine._calculate_servers_price(
                ['uuid-1', 'uuid-orphan', 'uuid-3'], db, promo_group_id=None
            )
        assert total == 8000  # 5000 + 0 + 3000
        assert len(details) == 3
        # Verify id fields
        assert details[0]['id'] == 10
        assert details[1]['id'] is None
        assert details[2]['id'] == 30

    @pytest.mark.asyncio
    async def test_db_exception_path(self):
        """Verify batch DB exception returns price=0 and status=error for all UUIDs."""
        engine = PricingEngine()
        db = AsyncMock()
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', side_effect=RuntimeError('DB error')):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 0
        assert details[0]['status'] == 'error'
        assert details[0]['id'] is None

    @pytest.mark.asyncio
    async def test_empty_uuids_returns_empty(self):
        """Verify empty UUIDs list returns 0 total and empty details."""
        engine = PricingEngine()
        db = AsyncMock()
        total, details = await engine._calculate_servers_price([], db, promo_group_id=None)
        assert total == 0
        assert details == []


class TestCalculateTrafficPrice:
    def test_base_only(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {25: 3000, 50: 5000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=25, purchased_traffic_gb=0)
        assert price == 3000

    def test_purchased_separated(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {25: 3000, 100: 8000, 125: 12000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=125, purchased_traffic_gb=100)
        assert price == 11000  # NOT 12000

    def test_unlimited_traffic_has_price(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 20000, 5: 2000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=0, purchased_traffic_gb=0)
        assert price == 20000  # 0 GB = unlimited, charged at unlimited tier

    def test_unlimited_traffic_ignores_purchased(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 20000, 50: 5000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=0, purchased_traffic_gb=50)
        assert price == 20000  # unlimited tier, purchased ignored

    def test_purchased_exceeds_total(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 0, 100: 8000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=80, purchased_traffic_gb=100)
        assert price == 8000  # base_gb clamped to 0


class TestCalculateRenewalPriceTariffMode:
    @pytest.mark.asyncio
    async def test_tariff_basic(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 2
        subscription.device_limit = 2
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.is_tariff_mode is True
        assert result.final_total == 19000

    @pytest.mark.asyncio
    async def test_tariff_extra_devices(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 2
        subscription.device_limit = 4
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.devices_price == 10000
        assert result.final_total == 29000

    @pytest.mark.asyncio
    async def test_tariff_device_price_from_tariff(self):
        """When tariff has device_price_kopeks set, use it instead of settings."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 10000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = 3000  # tariff-specific price
        subscription.tariff.id = 2
        subscription.device_limit = 4  # 2 extra devices
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000  # should NOT be used
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.devices_price == 6000  # 2 extra × 3000 (tariff price)
        assert result.final_total == 16000  # 10000 + 6000

    @pytest.mark.asyncio
    async def test_tariff_with_discounts(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 18000  # 20000 discounted by 10%
        assert result.promo_group_discount == 2000
        # After group: 18000, then 5% off 18000 = 900
        assert result.promo_offer_discount == 900
        assert result.final_total == 17100

    @pytest.mark.asyncio
    async def test_tariff_missing_period_returns_zero_base(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 60, user=user)
        assert result.base_price == 0
        assert result.final_total == 0

    @pytest.mark.asyncio
    async def test_tariff_device_limit_below_tariff_included(self):
        """When subscription device_limit < tariff device_limit, extra_devices is 0 (not negative)."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 10000}
        tariff.device_price_kopeks = 5000
        tariff.device_limit = 5
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 2  # less than tariff's 5
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None

        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        assert result.devices_price == 0
        assert result.final_total == 10000
        assert result.breakdown.get('extra_devices') == 0

    @pytest.mark.asyncio
    async def test_tariff_user_none(self):
        """When user=None, no discounts are applied."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=None)
        assert result.final_total == 20000
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 0


class TestCalculateRenewalPriceClassicMode:
    @pytest.mark.asyncio
    async def test_classic_all_components(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-1']
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 2
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 29000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 29000}),
        ):
            ms.get_traffic_price.return_value = 3000
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.is_tariff_mode is False
        assert result.base_price == 29000
        assert result.servers_price == 5000
        assert result.traffic_price == 3000
        assert result.final_total == 37000

    @pytest.mark.asyncio
    async def test_classic_with_discounts(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.id = 1
        promo_group.get_discount_percent.return_value = 20
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1
        user.promo_offer_discount_percent = 10
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=10),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 10000}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.final_total == 7200
        assert result.promo_group_discount == 2000
        assert result.promo_offer_discount == 800

    @pytest.mark.asyncio
    async def test_classic_fallback_to_period_prices(self):
        """When CLASSIC_PERIOD_PRICES has no entry, falls back to PERIOD_PRICES."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 99000}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 99000
        assert result.final_total == 99000

    @pytest.mark.asyncio
    async def test_classic_extra_devices(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 5
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 3000
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # 5 - 2 = 3 extra devices * 3000 = 9000
        assert result.devices_price == 9000
        assert result.final_total == 19000

    @pytest.mark.asyncio
    async def test_classic_breakdown_server_ids_and_prices_alignment(self):
        """Verify server_ids and servers_individual_prices have same length when orphaned UUIDs present."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-found', 'uuid-orphan', 'uuid-found2']
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        s1 = _make_server(price_kopeks=5000, server_id=10, squad_uuid='uuid-found')
        s3 = _make_server(price_kopeks=3000, server_id=30, squad_uuid='uuid-found2')
        with (
            patch(
                'app.services.pricing_engine.get_server_squads_by_uuids',
                return_value=[s1, s3],
            ),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # Verify breakdown alignment — both lists must have same length
        ids = result.breakdown['server_ids']
        prices = result.breakdown['servers_individual_prices']
        assert len(ids) == len(prices), f'server_ids({len(ids)}) != prices({len(prices)})'
        assert ids == [10, 30]
        assert prices == [5000, 3000]
        # Total servers_price includes only found servers
        assert result.servers_price == 8000

    @pytest.mark.asyncio
    async def test_classic_fixed_traffic_ignores_subscription_values(self):
        """When is_traffic_fixed() is True, use fixed limit and zero purchased."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 999  # should be ignored
        subscription.purchased_traffic_gb = 500  # should be ignored
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.is_traffic_fixed.return_value = True
            ms.get_fixed_traffic_limit.return_value = 50
            ms.get_traffic_price.side_effect = lambda gb: {50: 4000}.get(gb, 0)
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.traffic_price == 4000
        assert result.breakdown['purchased_traffic_gb'] == 0

    @pytest.mark.asyncio
    async def test_classic_default_traffic_limit_when_none(self):
        """When subscription.traffic_limit_gb is None, use DEFAULT_TRAFFIC_LIMIT_GB."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = None  # should fallback to default
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50
            ms.get_traffic_price.side_effect = lambda gb: {50: 4000}.get(gb, 0)
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.traffic_price == 4000

    @pytest.mark.asyncio
    async def test_classic_multi_month_period(self):
        """90-day period multiplies monthly prices by 3."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 50
        sub.purchased_traffic_gb = 0
        sub.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None

        server = _make_server(price_kopeks=3000, squad_uuid='uuid-s1')

        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {90: 27000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.PRICE_PER_DEVICE = 5000
            ms.get_traffic_price.return_value = 2000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50

            result = await engine.calculate_renewal_price(db, sub, 90, user=user)

        assert result.period_days == 90
        assert result.base_price == 27000
        # Servers and traffic are monthly x 3 months
        assert result.servers_price == 3000 * 3
        assert result.traffic_price == 2000 * 3
        assert result.devices_price == 0  # no extra devices
        assert result.final_total == 27000 + 9000 + 6000

    @pytest.mark.asyncio
    async def test_classic_per_category_different_discounts(self):
        """Different discount percents per category (period=10%, servers=20%, traffic=30%, devices=0%)."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 100
        sub.purchased_traffic_gb = 0
        sub.device_limit = 3  # 2 extra devices

        user = MagicMock()
        promo_group = MagicMock()

        def discount_by_category(category, period_days):
            return {'period': 10, 'servers': 20, 'traffic': 30, 'devices': 0}[category]

        promo_group.get_discount_percent = MagicMock(side_effect=discount_by_category)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1

        server = _make_server(price_kopeks=6000, squad_uuid='uuid-s1')

        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.PRICE_PER_DEVICE = 4000
            ms.get_traffic_price.return_value = 5000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 100

            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        # period: 10000 * 10% = 1000 discount -> 9000
        assert result.base_price == 9000
        # servers: 6000 * 20% = 1200 discount -> 4800 per month x 1
        assert result.servers_price == 4800
        # traffic: 5000 * 30% = 1500 discount -> 3500 per month x 1
        assert result.traffic_price == 3500
        # devices: 2 extra x 4000 = 8000, 0% discount -> 8000
        assert result.devices_price == 8000
        # total group discount = 1000 + 1200 + 1500 + 0 = 3700
        assert result.promo_group_discount == 3700
        assert result.final_total == 9000 + 4800 + 3500 + 8000

    @pytest.mark.asyncio
    async def test_classic_user_none(self):
        """When user=None, no discounts are applied."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 15000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=None)
        assert result.final_total == 15000
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 0


class TestServerPromoGroupFiltering:
    @pytest.mark.asyncio
    async def test_server_not_allowed_for_promo_group(self):
        """Server with restricted promo groups still charges real price."""
        engine = PricingEngine()
        db = AsyncMock()
        pg_mock = MagicMock()
        pg_mock.id = 99
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1', allowed_promo_groups=[pg_mock])
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=5)
        assert total == 5000  # real price still charged
        assert details[0]['status'] == 'not_allowed'

    @pytest.mark.asyncio
    async def test_server_empty_allowed_groups_is_open(self):
        """Server with empty allowed_promo_groups is available to all."""
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1', allowed_promo_groups=[])
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=5)
        assert total == 5000
        assert details[0]['status'] == 'available'


class TestFromPayloadRoundTrip:
    def test_renewal_pricing_snapshot_roundtrip(self):
        """RenewalPricing serialized via asdict() is correctly restored by from_payload()."""
        import dataclasses

        from app.services.subscription_renewal_service import SubscriptionRenewalPricing

        pricing = RenewalPricing(
            base_price=29000,
            servers_price=5000,
            traffic_price=3000,
            devices_price=0,
            promo_group_discount=2000,
            promo_offer_discount=800,
            final_total=34200,
            period_days=30,
            is_tariff_mode=False,
            breakdown={
                'server_ids': [1, 2],
                'servers_individual_prices': [5000, 3000],
                'offer_discount_pct': 5,
            },
        )
        payload = dataclasses.asdict(pricing)
        restored = SubscriptionRenewalPricing.from_payload(payload)

        assert restored.final_total == 34200
        assert restored.period_days == 30
        assert restored.promo_discount_value == 800  # mapped from promo_offer_discount
        assert restored.server_ids == [1, 2]
        assert restored.details.get('servers_individual_prices') == [5000, 3000]
        assert restored.months == 1
        assert restored.per_month == 34200


# ---------------------------------------------------------------------------
# Patch-target constants used in new tests below
# ---------------------------------------------------------------------------
SERVERS_BATCH_PATH = 'app.services.pricing_engine.get_server_squads_by_uuids'
SETTINGS_PATH = 'app.services.pricing_engine.settings'


class TestFromPayloadLegacyRoundTrip:
    def test_legacy_to_payload_roundtrip(self):
        """Legacy SubscriptionRenewalPricing.to_payload() -> from_payload() preserves all fields."""
        from app.services.subscription_renewal_service import SubscriptionRenewalPricing, build_renewal_period_id

        original = SubscriptionRenewalPricing(
            period_days=30,
            period_id=build_renewal_period_id(30),
            months=1,
            base_original_total=15000,
            discounted_total=12000,
            final_total=10800,
            promo_discount_value=1200,
            promo_discount_percent=10,
            overall_discount_percent=28,
            per_month=10800,
            server_ids=[1, 2, 3],
            details={'servers_individual_prices': [5000, 3000, 2000]},
        )
        payload = original.to_payload()
        restored = SubscriptionRenewalPricing.from_payload(payload)

        assert restored.period_days == original.period_days
        assert restored.period_id == original.period_id
        assert restored.months == original.months
        assert restored.base_original_total == original.base_original_total
        assert restored.discounted_total == original.discounted_total
        assert restored.final_total == original.final_total
        assert restored.promo_discount_value == original.promo_discount_value
        assert restored.promo_discount_percent == original.promo_discount_percent
        assert restored.overall_discount_percent == original.overall_discount_percent
        assert restored.per_month == original.per_month
        assert restored.server_ids == original.server_ids


class TestOriginalPriceIdentity:
    @pytest.mark.asyncio
    async def test_tariff_mode_identity(self):
        """final_total + promo_group_discount + promo_offer_discount == undiscounted subtotal."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 20000}
        tariff.device_price_kopeks = 3000
        tariff.device_limit = 1
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 3  # 2 extra
        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=25)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group

        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=15):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        subtotal = 20000 + 2 * 3000  # 26000
        assert result.final_total + result.promo_group_discount + result.promo_offer_discount == subtotal

    @pytest.mark.asyncio
    async def test_classic_mode_identity(self):
        """final_total + promo_group_discount + promo_offer_discount == undiscounted total in classic mode."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 50
        sub.purchased_traffic_gb = 0
        sub.device_limit = 2  # 1 extra

        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=20)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1

        server = _make_server(price_kopeks=4000, squad_uuid='uuid-s1')

        with (
            patch(SERVERS_BATCH_PATH, return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=10),
            patch(SETTINGS_PATH) as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.get_traffic_price.return_value = 3000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50

            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        # Reconstruct original undiscounted total
        original = result.final_total + result.promo_group_discount + result.promo_offer_discount
        # original should equal base_original + servers_original + traffic_original + devices_original
        expected_original = 10000 + 4000 + 3000 + 5000  # 22000
        assert original == expected_original

    @pytest.mark.asyncio
    async def test_original_total_property_tariff(self):
        """original_total property returns correct value."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 20000}
        tariff.device_price_kopeks = None
        tariff.device_limit = 1
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 1
        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=10)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        sub.tariff.is_daily = False
        sub.tariff.can_purchase_custom_days.return_value = False
        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)
        assert result.original_total == 20000  # undiscounted subtotal
