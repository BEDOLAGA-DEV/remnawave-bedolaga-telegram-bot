from types import SimpleNamespace

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _enable_protocols(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: True, raising=False)


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_protocols_button_present_without_tariff():
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=None, subscription=None)
    assert 'nz!_subscription_protocols' in _cbs(kb)


def test_protocols_button_present_with_tariff():
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    tariff = SimpleNamespace(device_price_kopeks=0)
    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=tariff, subscription=None)
    assert 'nz!_subscription_protocols' in _cbs(kb)
