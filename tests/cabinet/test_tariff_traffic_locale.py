from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.database.models import Tariff


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('language', 'finite', 'unlimited'),
    [
        ('ru', '200 ГБ', '♾️ Безлимит'),
        ('en', '200 GB', '♾️ Unlimited'),
        ('fa', '200 گیگ', '♾️ نامحدود'),
        ('zh', '200 GB', '♾️ 无限'),
        ('ua', '200 ГБ', '♾️ Безліміт'),
    ],
)
@pytest.mark.parametrize('traffic', [0, 200])
async def test_tariff_response_localizes_traffic_without_changing_limits(language, finite, unlimited, traffic):
    tariff = Tariff(
        id=1,
        name='Example',
        description='',
        is_active=True,
        is_daily=True,
        daily_price_kopeks=1500,
        period_prices={},
        traffic_limit_gb=traffic,
        device_limit=1,
        allowed_squads=[],
        display_order=1,
    )
    response = await _build_tariff_response(SimpleNamespace(), tariff, language=language)
    assert response['traffic_limit_label'] == (unlimited if traffic == 0 else finite)
    assert response['traffic_limit_gb'] == traffic
    assert response['is_unlimited_traffic'] is (traffic == 0)
    assert response['daily_price_kopeks'] == 1500
