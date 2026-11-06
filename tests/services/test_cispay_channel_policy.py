from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import payment_method_config_service as service


@pytest.mark.parametrize('method', ['card', 'sbp', 'crypto'])
async def test_telegram_env_is_not_disabled_by_legacy_cabinet_row(monkeypatch, method):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    for name in ('CARD', 'SBP', 'CRYPTO'):
        monkeypatch.setattr(settings, f'CISPAY_{name}_ENABLED', True)
    lookup = AsyncMock(
        return_value=SimpleNamespace(
            is_enabled=False,
            user_type_filter='all',
            first_topup_filter='any',
            promo_group_filter_mode='all',
        )
    )
    monkeypatch.setattr(service, 'get_config_by_method_id', lookup)
    monkeypatch.setattr(service, 'get_enabled_methods_for_user', AsyncMock(return_value=[]))
    user = SimpleNamespace(telegram_id=1, has_made_first_topup=False)
    policy = await service.get_cispay_checkout_policy(AsyncMock(), user, method, telegram_checkout=True)
    assert policy['selected_method'] == method.upper()
    lookup.assert_not_awaited()
    # The identical Telegram-linked user in the cabinet must still obey its disabled method.
    assert await service.get_cispay_checkout_policy(AsyncMock(), user, method) is None


@pytest.mark.parametrize('enabled,crypto,requested', [(False, True, 'card'), (True, False, 'crypto')])
async def test_telegram_still_obeys_env_disabled_methods(monkeypatch, enabled, crypto, requested):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', enabled)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', crypto)
    assert (
        await service.get_cispay_checkout_policy(
            AsyncMock(), SimpleNamespace(telegram_id=1), requested, telegram_checkout=True
        )
        is None
    )


@pytest.mark.parametrize('user', [None, SimpleNamespace(telegram_id=None)])
async def test_telegram_policy_cannot_be_used_for_guests(monkeypatch, user):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    assert await service.get_cispay_checkout_policy(AsyncMock(), user, 'card', telegram_checkout=True) is None
