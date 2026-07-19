from __future__ import annotations

import pytest

from app.config import settings
from app.services.system_settings_service import BotConfigurationService


NEW_KEYS = (
    'OVERPAY_SBP_TERMINAL_ID',
    'OVERPAY_CARD_TERMINAL_ID',
    'OVERPAY_INT_TERMINAL_ID',
    'OVERPAY_SBP_DIRECT_QR',
    'OVERPAY_INT_ENABLED',
    'OVERPAY_INT_MIN_EUR',
    'OVERPAY_RUB_PER_EUR',
    'OVERPAY_SERVER_IP',
)


def _enable_overpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OVERPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_USERNAME', 'login', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PASSWORD', 'secret', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PROJECT_ID', 'default-project', raising=False)


def test_new_settings_exist_with_safe_defaults() -> None:
    assert settings.OVERPAY_SBP_TERMINAL_ID is None
    assert settings.OVERPAY_CARD_TERMINAL_ID is None
    assert settings.OVERPAY_INT_TERMINAL_ID is None
    assert settings.OVERPAY_SBP_DIRECT_QR is False
    assert settings.OVERPAY_INT_ENABLED is False
    assert settings.OVERPAY_INT_MIN_EUR == 5.0
    assert settings.OVERPAY_RUB_PER_EUR == 0.0
    assert settings.OVERPAY_SERVER_IP is None


def test_new_keys_resolve_to_overpay_category() -> None:
    for key in NEW_KEYS:
        assert BotConfigurationService._resolve_category_key(key) == 'OVERPAY'


def test_terminal_id_falls_back_to_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_SBP_TERMINAL_ID', None, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_CARD_TERMINAL_ID', 'card-terminal', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_INT_TERMINAL_ID', None, raising=False)

    assert settings.get_overpay_terminal_id('fps') == 'default-project'
    assert settings.get_overpay_terminal_id('card') == 'card-terminal'
    assert settings.get_overpay_terminal_id('int') == 'default-project'
    assert settings.get_overpay_terminal_id(None) == 'default-project'

    monkeypatch.setattr(settings, 'OVERPAY_CARD_TERMINAL_ID', '', raising=False)
    assert settings.get_overpay_terminal_id('card') == 'default-project'
    assert settings.get_overpay_terminal_id('unknown') == 'default-project'


def test_int_enabled_requires_flag_and_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', True, raising=False)

    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 0.0, raising=False)
    assert settings.is_overpay_int_enabled() is False

    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 105.5, raising=False)
    assert settings.is_overpay_int_enabled() is True

    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', False, raising=False)
    assert settings.is_overpay_int_enabled() is False


def test_sbp_direct_qr_requires_server_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_SERVER_IP', None, raising=False)
    assert settings.is_overpay_sbp_direct_qr_enabled() is False

    monkeypatch.setattr(settings, 'OVERPAY_SERVER_IP', '   ', raising=False)
    assert settings.is_overpay_sbp_direct_qr_enabled() is False

    monkeypatch.setattr(settings, 'OVERPAY_SERVER_IP', '203.0.113.10', raising=False)
    assert settings.is_overpay_sbp_direct_qr_enabled() is True

    monkeypatch.setattr(settings, 'OVERPAY_SBP_DIRECT_QR', False, raising=False)
    assert settings.is_overpay_sbp_direct_qr_enabled() is False
