import html
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.subscription import devices, my_subscriptions, pricing
from app.localization.texts import get_texts


@pytest.fixture
def link_env(monkeypatch):
    sub = SimpleNamespace(
        id=7,
        user_id=1,
        tariff_id=None,
        tariff=None,
        is_pending_trial=False,
        subscription_url='https://vpn.example/sub/local?a=1&b=2',
        subscription_crypto_link='happ://crypt4/local-test-payload',
        traffic_limit_gb=0,
        traffic_used_gb=0,
        device_limit=2,
        end_date=datetime.now(UTC) + timedelta(days=30),
        status_display='Active',
        is_trial=False,
        is_active=True,
        connected_squads=[],
        autopay_enabled=False,
    )
    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr(my_subscriptions, '_build_subscription_detail_keyboard', lambda *a, **k: None)
    monkeypatch.setattr(devices, '_resolve_subscription', AsyncMock(return_value=(sub, 7)))
    monkeypatch.setattr(devices, 'get_device_management_help_keyboard', lambda *a: None)
    monkeypatch.setattr(pricing, '_get_countries_info', AsyncMock(return_value=[]))
    monkeypatch.setattr(pricing, 'get_subscription_cost', AsyncMock(return_value=0))
    monkeypatch.setattr(type(settings), 'is_devices_selection_enabled', lambda self: False)
    return sub


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['guide', 'happ_cryptolink'])
@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('surface', ['multi-card', 'classic-info', 'device-help'])
async def test_text_surfaces_use_display_link_and_existing_hide_policy(monkeypatch, link_env, mode, hidden, surface):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', mode)
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', hidden)
    user = SimpleNamespace(id=1, language='ru')
    callback = SimpleNamespace(data='sub_view:7', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    if surface == 'multi-card':
        await my_subscriptions.show_subscription_detail(callback, user, AsyncMock(), AsyncMock())
        text = callback.message.edit_text.await_args.args[0]
    elif surface == 'classic-info':
        text = await pricing.get_subscription_info_text(link_env, get_texts('ru'), user, AsyncMock())
    else:
        await devices.show_device_connection_help(callback, user, AsyncMock(), AsyncMock())
        text = callback.message.edit_text.await_args.args[0]
    # Crypto links remain visible under the existing shared Happ policy, never raw URLs.
    if mode == 'happ_cryptolink':
        assert link_env.subscription_crypto_link in text
        assert link_env.subscription_url not in text
        assert html.escape(link_env.subscription_url) not in text
    elif hidden:
        assert link_env.subscription_url not in text
        assert html.escape(link_env.subscription_url) not in text
        assert '<code>' not in text
    else:
        assert html.escape(link_env.subscription_url) in text
