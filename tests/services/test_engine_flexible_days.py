import pytest

from app.database.models import Tariff
from app.services.pricing_engine import pricing_engine


def _t():
    t = Tariff(name='t', device_limit=1)
    t.period_prices = {'30': 3000, '90': 7000, '180': 10000}
    t.flexible_days_enabled = True
    t.device_price_kopeks = None
    t.device_price_tiers = {}
    t.is_daily = False
    return t


@pytest.mark.asyncio
async def test_engine_flexible_base_for_custom_day():
    t = _t()
    # 50 days -> floor-anchor base 5000 kopeks; no extra devices (device_limit=1)
    result = await pricing_engine.calculate_tariff_purchase_price(t, 50, device_limit=1)
    assert result.base_price == 5000
