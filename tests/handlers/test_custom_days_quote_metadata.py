"""Custom-day purchase metadata must reproduce the authoritative group quote."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.config import settings
from app.database.models import PromoGroup, Tariff
from app.services.pricing_engine import pricing_engine


@pytest.mark.parametrize('offer', [0, 17, 100])
@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('default', [False, True])
@pytest.mark.parametrize('days', [45, 75, 105])
async def test_custom_days_metadata_matches_exact_period_quote(monkeypatch, default, days, fallback, offer):
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 101)
    group = PromoGroup(
        id=1, name='Test', is_default=default, period_discounts={'30': 30, '75': 25}, device_discount_percent=0
    )
    monkeypatch.setattr(settings, 'BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED', fallback)
    monkeypatch.setattr(settings, 'BASE_PROMO_GROUP_PERIOD_DISCOUNTS', '105:17,75:99')
    user = SimpleNamespace(get_primary_promo_group=lambda: group, promo_offer_discount_percent=offer)
    tariff = Tariff(
        id=1,
        name='Test',
        is_active=True,
        device_limit=1,
        traffic_limit_gb=100,
        period_prices={'45': 999},
        custom_days_enabled=True,
        price_per_day_kopeks=5,
        min_days=1,
        max_days=365,
        allowed_squads=[],
        device_price_kopeks=None,
    )
    source = SimpleNamespace(device_limit=3, tariff_id=None, is_trial=False)
    response = await _build_tariff_response(AsyncMock(), tariff, user=user, subscription=source)
    metadata = response['custom_days_pricing']
    assert metadata['price_per_day_kopeks'] == 5
    assert metadata['device_price_kopeks'] == 101
    assert metadata['period_prices'] == {'45': 999}
    rates = metadata['discounts_by_days'].get(str(days), {})
    base = metadata['period_prices'].get(str(days), days * metadata['price_per_day_kopeks'])
    devices = 2 * metadata['device_price_kopeks'] * days // 30
    quoted = pricing_engine.apply_discount(base, rates.get('period', 0)) + pricing_engine.apply_discount(
        devices, rates.get('devices', metadata['default_device_discount_percent'])
    )
    actual = await pricing_engine.calculate_tariff_purchase_price(tariff, days, device_limit=3, user=user)
    assert pricing_engine.apply_discount(quoted, offer) == actual.final_total
