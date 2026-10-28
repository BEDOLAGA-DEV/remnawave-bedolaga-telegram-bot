from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.database.models import Tariff
from app.services.tariff_purchase_quote import PurchaseQuoteError, calculate_purchase_quote


def make_tariff(**changes):
    return Tariff(
        **(
            {
                'id': 7,
                'device_limit': 1,
                'device_price_kopeks': 10000,
                'max_device_limit': 5,
                'is_daily': False,
                'period_prices': {'1': 1000, '7': 7000, '30': 30000, '45': 45000, '365': 200000, '10000': 1000000},
                'custom_days_enabled': False,
                'custom_traffic_enabled': False,
                'traffic_limit_gb': 100,
            }
            | changes
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('days', 'extra'), [(1, 666), (7, 4666), (30, 20000), (45, 30000), (365, 243333), (10000, 6666666)]
)
async def test_period_price_uses_existing_exact_day_pricing(days, extra):
    tariff = make_tariff()
    quote = await calculate_purchase_quote(tariff, None, None, period_days=days, selected_device_limit=3)
    assert quote.pricing.devices_price == extra
    assert quote.pricing.final_total == tariff.period_prices[str(days)] + extra
    assert quote.devices.effective_limit == 3
    assert quote.purchase_kind == 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [0, -1, True, 1.5, 31])
async def test_invalid_or_unconfigured_period_rejected(days):
    with pytest.raises(PurchaseQuoteError):
        await calculate_purchase_quote(make_tariff(), None, None, period_days=days)


@pytest.mark.asyncio
async def test_date_overflow_rejected_before_pricing():
    with pytest.raises(PurchaseQuoteError, match='period_out_of_range'):
        await calculate_purchase_quote(
            make_tariff(), None, None, period_days=365, now=datetime(9999, 12, 1, tzinfo=UTC)
        )


@pytest.mark.asyncio
async def test_money_overflow_rejected_even_when_discount_could_hide_it():
    tariff = make_tariff(period_prices={'30': 2147483647})
    with pytest.raises(PurchaseQuoteError, match='price_out_of_range'):
        await calculate_purchase_quote(tariff, None, None, period_days=30, selected_device_limit=3)


@pytest.mark.asyncio
async def test_free_base_still_charges_devices():
    quote = await calculate_purchase_quote(
        make_tariff(period_prices={'30': 0}), None, None, period_days=30, selected_device_limit=3
    )
    assert quote.pricing.final_total == 20000


@pytest.mark.asyncio
@pytest.mark.parametrize('traffic', [0, 9, 201, True])
async def test_custom_traffic_outside_config_rejected(traffic):
    tariff = make_tariff(
        custom_traffic_enabled=True, traffic_price_per_gb_kopeks=100, min_traffic_gb=10, max_traffic_gb=200
    )
    with pytest.raises(PurchaseQuoteError, match='invalid_traffic'):
        await calculate_purchase_quote(tariff, None, None, period_days=30, traffic_gb=traffic)


@pytest.mark.asyncio
async def test_custom_days_traffic_and_devices_share_one_quote():
    tariff = make_tariff(
        custom_days_enabled=True,
        price_per_day_kopeks=1000,
        min_days=1,
        max_days=10000,
        custom_traffic_enabled=True,
        traffic_price_per_gb_kopeks=100,
        min_traffic_gb=10,
        max_traffic_gb=200,
    )
    quote = await calculate_purchase_quote(tariff, None, None, period_days=33, traffic_gb=50, selected_device_limit=3)
    assert quote.pricing.final_total == 33000 + 22000 + 5000
    assert quote.traffic_limit_gb == 50


@pytest.mark.asyncio
async def test_fixed_traffic_rejects_override_in_new_contract():
    with pytest.raises(PurchaseQuoteError, match='invalid_traffic'):
        await calculate_purchase_quote(make_tariff(), None, None, period_days=30, traffic_gb=999)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('is_trial', 'tariff_id', 'kind'),
    [(True, 7, 'trial_conversion'), (False, 7, 'paid_renewal'), (False, None, 'classic_conversion')],
)
async def test_purchase_kind_depends_on_resolved_subscription(is_trial, tariff_id, kind):
    subscription = SimpleNamespace(is_trial=is_trial, tariff_id=tariff_id, device_limit=1, end_date=None)
    quote = await calculate_purchase_quote(make_tariff(), None, subscription, period_days=30)
    assert quote.purchase_kind == kind


@pytest.mark.asyncio
async def test_daily_quote_rejects_multiple_days():
    with pytest.raises(PurchaseQuoteError, match='invalid_period'):
        await calculate_purchase_quote(
            make_tariff(is_daily=True, daily_price_kopeks=1000), None, None, period_days=2, selected_device_limit=3
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('pct', [0, 10, 100])
async def test_category_discounts_and_offer_applied_once(pct):
    group = SimpleNamespace(
        id=8,
        apply_discounts_to_addons=True,
        get_discount_percent=lambda category, days: {'period': 20, 'devices': 50, 'traffic': 10}[category],
    )
    user = SimpleNamespace(
        get_primary_promo_group=lambda: group, promo_offer_discount_percent=pct, promo_offer_discount_expires_at=None
    )
    tariff = make_tariff(
        custom_traffic_enabled=True, traffic_price_per_gb_kopeks=100, min_traffic_gb=10, max_traffic_gb=200
    )
    quote = await calculate_purchase_quote(tariff, user, None, period_days=30, selected_device_limit=3, traffic_gb=50)
    assert quote.pricing.base_price == 24000
    assert quote.pricing.devices_price == 10000
    assert quote.pricing.traffic_price == 4500
    assert quote.pricing.final_total == 38500 * (100 - pct) // 100
    assert quote.pricing.original_total == 55000


@pytest.mark.asyncio
async def test_negative_configured_price_rejected_before_engine_can_clamp_it():
    with pytest.raises(PurchaseQuoteError, match='price_out_of_range'):
        await calculate_purchase_quote(make_tariff(period_prices={'30': -100}), None, None, period_days=30)


@pytest.mark.asyncio
async def test_overflow_of_original_price_cannot_hide_behind_full_discount():
    group = SimpleNamespace(id=8, get_discount_percent=lambda category, days: 100)
    user = SimpleNamespace(get_primary_promo_group=lambda: group, promo_offer_discount_percent=0)
    with pytest.raises(PurchaseQuoteError, match='price_out_of_range'):
        await calculate_purchase_quote(
            make_tariff(period_prices={'30': 2147483647}), user, None, period_days=30, selected_device_limit=3
        )


@pytest.mark.asyncio
async def test_daily_quote_includes_selected_devices_for_one_day():
    quote = await calculate_purchase_quote(
        make_tariff(is_daily=True, daily_price_kopeks=1000, device_price_kopeks=3000),
        None,
        None,
        period_days=1,
        selected_device_limit=3,
    )
    assert quote.pricing.final_total == 1200
    assert quote.devices.effective_limit == 3


@pytest.mark.asyncio
async def test_daily_quote_distinguishes_oneoff_offer_from_next_charge():
    from datetime import timedelta

    user = SimpleNamespace(
        promo_group=None,
        get_primary_promo_group=lambda: None,
        promo_offer_discount_percent=50,
        promo_offer_discount_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    quote = await calculate_purchase_quote(
        make_tariff(is_daily=True, daily_price_kopeks=1000, device_price_kopeks=3000),
        user,
        None,
        period_days=1,
        selected_device_limit=3,
    )
    assert quote.pricing.final_total == 600
    assert quote.next_daily_charge_kopeks == 1200
