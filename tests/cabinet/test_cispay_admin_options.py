from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_payment_methods as routes
from app.config import settings
from app.services import payment_method_config_service as service


def config():
    return SimpleNamespace(
        method_id='cispay',
        sort_order=0,
        is_enabled=True,
        display_name=None,
        description=None,
        sub_options={'card': True, 'sbp': True, 'crypto': False},
        quick_amounts=None,
        min_amount_kopeks=None,
        max_amount_kopeks=None,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        allowed_promo_groups=[],
        open_url_direct=False,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.parametrize('endpoint', ['detail', 'list'])
@pytest.mark.parametrize('crypto_enabled', [False, True])
async def test_admin_shows_crypto_even_when_provider_switch_is_off(monkeypatch, crypto_enabled, endpoint):
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', crypto_enabled)
    monkeypatch.setattr(routes, 'get_config_by_method_id', AsyncMock(return_value=config()))
    monkeypatch.setattr(routes, 'get_all_configs', AsyncMock(return_value=[config()]))
    if endpoint == 'detail':
        result = await routes.get_payment_method('cispay', admin=SimpleNamespace(), db=AsyncMock())
    else:
        result = (await routes.list_payment_methods(admin=SimpleNamespace(), db=AsyncMock()))[0]
    options = {opt.id: opt for opt in result.available_sub_options}
    assert set(options) == {'card', 'sbp', 'crypto'}
    assert options['crypto'].is_provider_enabled is crypto_enabled
    assert result.sub_options['crypto'] is False


@pytest.mark.parametrize('crypto_enabled', [False, True])
@pytest.mark.parametrize('cabinet_enabled', [False, True])
async def test_admin_catalog_does_not_enable_customer_checkout(monkeypatch, crypto_enabled, cabinet_enabled):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-key')
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', crypto_enabled)
    row = config()
    row.sub_options['crypto'] = cabinet_enabled
    monkeypatch.setattr(service, 'get_all_configs', AsyncMock(return_value=[row]))
    monkeypatch.setattr(service, 'get_config_by_method_id', AsyncMock(return_value=row))
    methods = await service.get_enabled_methods_for_user(AsyncMock())
    ids = {opt['id'] for opt in methods[0]['options']}
    assert ('crypto' in ids) is (crypto_enabled and cabinet_enabled)
    policy = await service.get_cispay_checkout_policy(AsyncMock(), None, 'crypto')
    assert (policy is not None) is (crypto_enabled and cabinet_enabled)
