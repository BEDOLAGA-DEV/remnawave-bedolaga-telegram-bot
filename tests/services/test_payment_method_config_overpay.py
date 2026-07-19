from __future__ import annotations

import pytest

from app.config import settings
from app.services.payment_method_config_service import _get_method_defaults, _get_overpay_sub_options


def _enable_overpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OVERPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_USERNAME', 'login', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PASSWORD', 'secret', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PROJECT_ID', 'project', raising=False)


def test_sub_options_without_int(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', False, raising=False)

    assert [o['id'] for o in _get_overpay_sub_options()] == ['card', 'fps']


def test_sub_options_with_int(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 100.0, raising=False)

    options = _get_overpay_sub_options()
    assert [o['id'] for o in options] == ['card', 'fps', 'int']
    assert options[-1]['name'] == 'Международная карта (EUR)'


def test_method_defaults_use_dynamic_sub_options(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 100.0, raising=False)

    defaults = _get_method_defaults()
    assert [o['id'] for o in defaults['overpay']['available_sub_options']] == ['card', 'fps', 'int']
