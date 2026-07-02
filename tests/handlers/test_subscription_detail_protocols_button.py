from types import SimpleNamespace

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _enable_protocols(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: True, raising=False)


def test_detail_keyboard_has_protocols_button(monkeypatch):
    # FreezeSettingsService.is_enabled() is called synchronously in the builder;
    # pin it so the test doesn't depend on freeze config/DB state.
    monkeypatch.setattr(
        'app.services.freeze_settings_service.FreezeSettingsService.is_enabled',
        staticmethod(lambda: False),
        raising=False,
    )
    from app.handlers.subscription.my_subscriptions import _build_subscription_detail_keyboard

    sub = SimpleNamespace(actual_status='active', frozen_at=None)
    kb = _build_subscription_detail_keyboard(7, sub=sub)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert 'nz!_subscription_protocols' in cbs


def test_inactive_subscription_hides_protocols_button(monkeypatch):
    monkeypatch.setattr(
        'app.services.freeze_settings_service.FreezeSettingsService.is_enabled',
        staticmethod(lambda: False),
        raising=False,
    )
    from app.handlers.subscription.my_subscriptions import _build_subscription_detail_keyboard

    sub = SimpleNamespace(actual_status='expired', frozen_at=None)
    kb = _build_subscription_detail_keyboard(7, sub=sub)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert 'nz!_subscription_protocols' not in cbs
