from types import SimpleNamespace

import pytest

from app.database.models import Tariff


@pytest.mark.parametrize('surface', ['cabinet', 'miniapp'])
async def test_catalog_omits_missing_period_and_keeps_explicit_zero(surface):
    tariff = Tariff(
        id=3,
        name='Configured',
        description='',
        tier_level=1,
        is_active=True,
        is_daily=False,
        period_prices={'14': None, '30': 3000, '60': 0},
        traffic_limit_gb=2,
        device_limit=1,
        allowed_squads=[],
        display_order=1,
    )
    if surface == 'cabinet':
        from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response

        result = await _build_tariff_response(SimpleNamespace(), tariff)
        periods = result['periods']
    else:
        from app.webapi.routes.miniapp import _build_tariff_model

        result = await _build_tariff_model(SimpleNamespace(), tariff)
        periods = [period.model_dump() for period in result.periods]
    assert [(period['days'], period['price_kopeks']) for period in periods] == [(30, 3000), (60, 0)]
