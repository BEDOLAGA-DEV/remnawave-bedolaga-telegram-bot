"""Monthly device rates must follow the configured tariff duration without rounding months."""

from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.database.models import Tariff
from app.handlers.subscription.tariff_purchase import _calc_extra_devices_cost
from app.services.pricing_engine import PricingEngine


@pytest.mark.parametrize('days', [1, 7, 14, 29, 30, 31, 45, 365, 10000])
@pytest.mark.parametrize('surface', ['engine', 'custom_days', 'renewal', 'telegram', 'cabinet'])
async def test_device_price_follows_arbitrary_tariff_days(days, surface):
    tariff = Tariff(
        id=1,
        name='Test',
        description='',
        tier_level=1,
        is_active=True,
        is_daily=False,
        device_limit=2,
        device_price_kopeks=3000,
        traffic_limit_gb=100,
        period_prices={} if surface == 'custom_days' else {str(days): days * 200},
        custom_days_enabled=True,
        price_per_day_kopeks=200,
        min_days=1,
        max_days=10000,
        allowed_squads=[],
        display_order=1,
    )
    expected = days * 100
    if surface in ['engine', 'custom_days']:
        result = await PricingEngine().calculate_tariff_purchase_price(tariff, days, device_limit=3)
        assert result.devices_price == expected
        assert result.final_total == days * 200 + expected
    elif surface == 'renewal':
        result = await PricingEngine().calculate_renewal_price(
            None, SimpleNamespace(tariff_id=1, tariff=tariff, device_limit=3), days
        )
        assert result.devices_price == expected
        assert result.final_total == days * 200 + expected
    elif surface == 'telegram':
        assert _calc_extra_devices_cost(tariff, 3, days) == expected
    else:
        result = await _build_tariff_response(
            SimpleNamespace(),
            tariff,
            current_tariff_id=1,
            subscription=SimpleNamespace(tariff_id=1, device_limit=3, is_trial=False),
        )
        assert result['periods'][0]['extra_devices_cost_kopeks'] == expected
        assert result['periods'][0]['price_kopeks'] == days * 200 + expected


@pytest.mark.parametrize('daily', [False, True])
@pytest.mark.parametrize('included', [0, 2])
async def test_daily_rate_and_unlimited_devices_are_preserved(daily, included):
    tariff = Tariff(
        id=1,
        is_daily=daily,
        daily_price_kopeks=200,
        period_prices={'1': 200},
        device_limit=included,
        device_price_kopeks=3000,
    )
    result = await PricingEngine().calculate_tariff_purchase_price(tariff, 1, device_limit=4)
    expected = 0 if included == 0 else 6000 if daily else 200
    assert result.devices_price == expected
    assert result.final_total == 200 + expected
    assert _calc_extra_devices_cost(tariff, 4, 1) == expected


async def test_telegram_preview_uses_configured_fallback_device_rate(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 3000)
    tariff = Tariff(id=1, is_daily=False, period_prices={'14': 200}, device_limit=2, device_price_kopeks=None)
    result = await PricingEngine().calculate_tariff_purchase_price(tariff, 14, device_limit=3)
    assert result.devices_price == 1400
    assert _calc_extra_devices_cost(tariff, 3, 14) == result.devices_price
