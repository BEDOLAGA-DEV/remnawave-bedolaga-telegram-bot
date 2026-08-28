"""Техработы не должны включаться сами после перезапуска.

Жалоба: админ выключает режим, а после рестарта он снова включён. Причин две, и
обе воспроизводятся здесь.

1. MAINTENANCE_MODE, заданный в .env, попадает в ENV_OVERRIDE_KEYS: переключение
   из панели ложится в БД, но к settings не применяется, а на старте set_bot
   читает именно settings. Значение из окружения побеждает при каждом запуске.
   Кодом это не лечится — .env сильнее по устройству, — поэтому панель обязана
   об этом сказать, а .env.example не обязан пиннить ключ по умолчанию.
2. Статус техработ кэшируется на час. Кэш нужен, чтобы перезапуск во время аварии
   не снимал АВТОМАТИЧЕСКИ включённый режим, но ручной режим он воскрешать не
   должен: там источник истины — сохранённая настройка.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.maintenance_service import MaintenanceService


@pytest.fixture
def service() -> MaintenanceService:
    return MaintenanceService()


class TestCacheDoesNotResurrectManualMode:
    async def test_manual_mode_is_not_restored_when_setting_says_off(self, service, monkeypatch):
        monkeypatch.setattr(settings, 'MAINTENANCE_MODE', False, raising=False)
        monkeypatch.setattr(
            'app.services.maintenance_service.cache',
            SimpleNamespace(get=AsyncMock(return_value={'is_active': True, 'auto_enabled': False})),
        )

        await service._load_status_from_cache()

        assert service.is_maintenance_active() is False

    async def test_auto_mode_survives_restart(self, service, monkeypatch):
        """Перезапуск во время аварии не должен снимать авто-режим."""
        monkeypatch.setattr(settings, 'MAINTENANCE_MODE', False, raising=False)
        monkeypatch.setattr(
            'app.services.maintenance_service.cache',
            SimpleNamespace(get=AsyncMock(return_value={'is_active': True, 'auto_enabled': True})),
        )

        await service._load_status_from_cache()

        assert service.is_maintenance_active() is True

    async def test_manual_mode_is_restored_when_setting_agrees(self, service, monkeypatch):
        monkeypatch.setattr(settings, 'MAINTENANCE_MODE', True, raising=False)
        monkeypatch.setattr(
            'app.services.maintenance_service.cache',
            SimpleNamespace(get=AsyncMock(return_value={'is_active': True, 'auto_enabled': False})),
        )

        await service._load_status_from_cache()

        assert service.is_maintenance_active() is True


class TestEnvPinning:
    def test_env_example_does_not_pin_maintenance_keys(self):
        """Пиннить редактируемый из админки ключ — значит сломать его редактирование.

        Ровно из-за раскомментированного MAINTENANCE_MODE переключение из панели не
        переживало перезапуск.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        lines = (root / '.env.example').read_text(encoding='utf-8').splitlines()

        pinned = [line for line in lines if line.strip().startswith('MAINTENANCE_')]

        assert pinned == [], f'Эти ключи заданы в .env.example и перестанут редактироваться из админки: {pinned}'

    def test_editable_maintenance_keys_are_declared(self):
        """Страховка от обратного: ключи действительно редактируются из админки."""
        from app.services.system_settings_service import BotConfigurationService

        overrides = BotConfigurationService.CATEGORY_KEY_OVERRIDES
        hints = BotConfigurationService.SETTING_HINTS

        for key in ('MAINTENANCE_MODE', 'MAINTENANCE_AUTO_ENABLE', 'MAINTENANCE_MESSAGE'):
            assert key in overrides or key in hints, key


class TestPanelWarnsWhenEnvLocked:
    def test_warning_is_shown_only_for_env_locked_key(self, monkeypatch):
        from app.handlers.admin import maintenance as panel

        monkeypatch.setattr(panel.bot_configuration_service, 'is_env_overridden', lambda key: key == 'MAINTENANCE_MODE')
        assert panel._maintenance_env_locked() is True

        monkeypatch.setattr(panel.bot_configuration_service, 'is_env_overridden', lambda key: False)
        assert panel._maintenance_env_locked() is False

    def test_warning_names_the_file_and_the_consequence(self):
        from app.handlers.admin.maintenance import _ENV_LOCKED_WARNING

        assert '.env' in _ENV_LOCKED_WARNING
        assert 'перезапуск' in _ENV_LOCKED_WARNING


class TestReferralKeysAreEditable:
    """Та же ловушка, что и с техработами: пиннинг в .env отключает редактирование.

    Реферальные настройки — предмет отдельной задачи «настраиваемая реферальная
    система», и она невыполнима, пока ключи заданы в окружении: запись из админки
    ложится в БД, но к settings не применяется.
    """

    def test_env_example_does_not_pin_referral_keys(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        lines = (root / '.env.example').read_text(encoding='utf-8').splitlines()

        pinned = [line for line in lines if line.strip().startswith('REFERRAL_')]

        assert pinned == [], f'Эти ключи перестанут редактироваться из админки: {pinned}'

    def test_settings_screen_names_locked_keys_instead_of_blaming_env(self, monkeypatch):
        """Экран обязан отличать «залочено окружением» от «правьте .env всегда»."""
        from app.handlers.admin import referrals

        monkeypatch.setattr(referrals, 'ENV_OVERRIDE_KEYS', set())
        assert 'кабинете' in referrals._settings_hint()

        monkeypatch.setattr(referrals, 'ENV_OVERRIDE_KEYS', {'REFERRAL_COMMISSION_PERCENT'})
        hint = referrals._settings_hint()
        assert 'REFERRAL_COMMISSION_PERCENT' in hint
        assert '.env' in hint
