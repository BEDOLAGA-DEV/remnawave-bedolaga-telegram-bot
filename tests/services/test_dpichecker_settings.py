"""DPI//CHECKER: настройки в реестре, ключ — секрет, права read/run, адрес вебхука от WEBHOOK_URL."""

from __future__ import annotations

from app.config import settings
from app.services.permission_service import PERMISSION_REGISTRY
from app.services.rbac_bootstrap_service import _PRESET_ROLES
from app.services.system_settings_service import BotConfigurationService


def test_permission_section_has_read_and_run():
    assert PERMISSION_REGISTRY['dpichecker'] == ['read', 'run']


def test_roles_with_bscheker_get_dpichecker_too():
    for role in _PRESET_ROLES:
        permissions = role['permissions']
        if 'reachability:*' in permissions:
            assert 'dpichecker:*' in permissions, role['name']


def test_settings_live_in_own_category():
    assert BotConfigurationService.CATEGORY_TITLES['DPICHECKER'] == '🧱 DPI//CHECKER'
    assert BotConfigurationService.CATEGORY_PREFIX_OVERRIDES['DPICHECKER_'] == 'DPICHECKER'
    assert 'DPICHECKER' in BotConfigurationService.CATEGORY_DESCRIPTIONS


def test_api_key_is_masked_secret():
    assert BotConfigurationService.is_masked_secret('DPICHECKER_API_KEY', 'abc') is True


def test_disabled_by_default_and_needs_key(monkeypatch):
    assert type(settings).model_fields['DPICHECKER_ENABLED'].default is False
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', True)
    monkeypatch.setattr(settings, 'DPICHECKER_API_KEY', None)
    assert settings.is_dpichecker_enabled() is True
    assert settings.is_dpichecker_configured() is False


def test_webhook_url_follows_public_bot_url(monkeypatch):
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example/')
    assert settings.get_dpichecker_webhook_url() == 'https://bot.example/dpichecker/webhook'
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None)
    assert settings.get_dpichecker_webhook_url() is None
