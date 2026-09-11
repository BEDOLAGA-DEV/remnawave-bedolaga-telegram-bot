"""Переключатели уведомлений истёкшим — обычные настройки бота в базе, а не файл на диске.

Жалоба оператора (бот 4.9.1, кабинет 1.73.0): в боте выключены «1 день после истечения» и обе
волны скидок, а предложения expired_discount_wave2/wave3 всё равно уходят пользователям.
Переключатели жили в ``data/notification_settings.json`` и читались в память процесса один раз
навсегда: второй процесс с тем же образом или потерянный/недоступный на запись каталог ``data/``
после перезапуска возвращали волнам «включено», хотя меню показывало «Выкл». Кабинет их и вовсе
не видел — там только шаблоны ручных рассылок, к волнам они не относятся.

Теперь это ключи ``NOTIFICATION_*`` в ``Settings``: хранятся в базе через слой системных настроек,
видны в кабинете, читаются живьём из ``settings`` каждым циклом мониторинга; старый файл
импортируется один раз при старте и переименовывается.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.database.models import SystemSetting
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from app.services.system_settings_service import bot_configuration_service
from tests.fixtures.sqlite_memory import memory_session


TABLES = [SystemSetting.__table__]
KEYS = (
    'NOTIFICATION_TRIAL_CHANNEL_UNSUBSCRIBED_ENABLED',
    'NOTIFICATION_EXPIRED_1D_ENABLED',
    'NOTIFICATION_EXPIRED_WAVE2_ENABLED',
    'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT',
    'NOTIFICATION_EXPIRED_WAVE2_VALID_HOURS',
    'NOTIFICATION_EXPIRED_WAVE3_ENABLED',
    'NOTIFICATION_EXPIRED_WAVE3_DISCOUNT_PERCENT',
    'NOTIFICATION_EXPIRED_WAVE3_VALID_HOURS',
    'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS',
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Настройки — глобальный объект: значения возвращаются после теста; файла по умолчанию нет."""
    bot_configuration_service.initialize_definitions()
    for key in KEYS:
        monkeypatch.setattr(settings, key, getattr(settings, key))
    monkeypatch.setattr(NotificationSettingsService, '_legacy_path', tmp_path / 'notification_settings.json')
    yield
    for key in KEYS:
        bot_configuration_service._overrides_raw.pop(key, None)


def test_keys_are_bot_settings_in_the_user_notifications_category() -> None:
    for key in KEYS:
        definition = bot_configuration_service.get_definition(key)
        assert definition.category_key == 'NOTIFICATIONS', key
        assert bot_configuration_service.SETTING_HINTS[key]['description'], f'{key}: подсказка словами для кабинета'
    assert settings.NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT == 10
    assert settings.NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS == 5


def test_getters_read_live_settings_not_the_file(monkeypatch, tmp_path) -> None:
    stale = tmp_path / 'notification_settings.json'
    stale.write_text(json.dumps({'expired_second_wave': {'enabled': False, 'discount_percent': 55}}))
    monkeypatch.setattr(NotificationSettingsService, '_legacy_path', stale)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED', True)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT', 10)
    assert NotificationSettingsService.is_second_wave_enabled() is True, 'файл — не источник'
    assert NotificationSettingsService.get_second_wave_discount_percent() == 10
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED', False)
    assert NotificationSettingsService.is_second_wave_enabled() is False, 'выключили — следующий цикл уже видит'
    assert NotificationSettingsService.get_config()['expired_second_wave'] == {
        'enabled': False,
        'discount_percent': 10,
        'valid_hours': 24,
    }
    assert set(NotificationSettingsService.get_config()) == {
        'trial_channel_unsubscribed',
        'expired_1d',
        'expired_second_wave',
        'expired_third_wave',
    }


def test_getters_clamp_bad_values(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS', 0)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT', 150)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE3_VALID_HOURS', 0)
    assert NotificationSettingsService.get_third_wave_trigger_days() == 2
    assert NotificationSettingsService.get_second_wave_discount_percent() == 100
    assert NotificationSettingsService.get_third_wave_valid_hours() == 1


@pytest.mark.asyncio
async def test_setters_persist_to_db_and_apply_live(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        assert await NotificationSettingsService.set_second_wave_enabled(db, False) is True
        assert await NotificationSettingsService.set_third_wave_trigger_days(db, 7) is True
        assert await NotificationSettingsService.set_expired_1d_enabled(db, False) is True
        assert await NotificationSettingsService.set_trial_channel_unsubscribed_enabled(db, False) is True
        # Сессия кабинета закрывается без коммита — запись обязана коммитить сама.
        await db.rollback()
        assert await get_setting_value(db, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED') == 'false'
        assert await get_setting_value(db, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS') == '7'
    assert settings.NOTIFICATION_EXPIRED_WAVE2_ENABLED is False
    assert NotificationSettingsService.is_second_wave_enabled() is False
    assert NotificationSettingsService.is_expired_1d_enabled() is False
    assert NotificationSettingsService.is_trial_channel_unsubscribed_enabled() is False
    assert NotificationSettingsService.get_third_wave_trigger_days() == 7


@pytest.mark.asyncio
async def test_setters_clamp_and_reject_garbage(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        assert await NotificationSettingsService.set_second_wave_discount_percent(db, 'abc') is False
        assert await NotificationSettingsService.set_second_wave_valid_hours(db, 500) is True
        assert await NotificationSettingsService.set_third_wave_discount_percent(db, -5) is True
        assert await NotificationSettingsService.set_third_wave_valid_hours(db, 0) is True
        assert await NotificationSettingsService.set_third_wave_trigger_days(db, 1) is True
    assert NotificationSettingsService.get_second_wave_valid_hours() == 168
    assert NotificationSettingsService.get_third_wave_discount_percent() == 0
    assert NotificationSettingsService.get_third_wave_valid_hours() == 1
    assert NotificationSettingsService.get_third_wave_trigger_days() == 2


@pytest.mark.asyncio
async def test_legacy_file_is_imported_once_and_never_overrides_the_db(monkeypatch, tmp_path) -> None:
    legacy = tmp_path / 'notification_settings.json'
    legacy.write_text(
        json.dumps(
            {
                'trial_channel_unsubscribed': {'enabled': False},
                'expired_1d': {'enabled': False},
                'expired_second_wave': {'enabled': False, 'discount_percent': 15, 'valid_hours': 48},
                'expired_third_wave': {'enabled': True, 'discount_percent': 'мусор', 'trigger_days': 7},
            }
        )
    )
    monkeypatch.setattr(NotificationSettingsService, '_legacy_path', legacy)
    async with memory_session(monkeypatch, TABLES) as db:
        # В базе уже есть своё значение — оно главнее файла.
        await bot_configuration_service.set_value(db, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS', 9)
        imported = await NotificationSettingsService.import_legacy_file(db)
        await db.rollback()
        assert imported == {
            'NOTIFICATION_TRIAL_CHANNEL_UNSUBSCRIBED_ENABLED': False,
            'NOTIFICATION_EXPIRED_1D_ENABLED': False,
            'NOTIFICATION_EXPIRED_WAVE2_ENABLED': False,
            'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT': 15,
            'NOTIFICATION_EXPIRED_WAVE2_VALID_HOURS': 48,
            'NOTIFICATION_EXPIRED_WAVE3_ENABLED': True,
        }, 'мусор пропущен, уже заданное в базе не тронуто'
        assert await get_setting_value(db, 'NOTIFICATION_EXPIRED_WAVE2_DISCOUNT_PERCENT') == '15'
        assert await get_setting_value(db, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS') == '9'
    assert settings.NOTIFICATION_EXPIRED_1D_ENABLED is False
    assert settings.NOTIFICATION_EXPIRED_WAVE2_VALID_HOURS == 48
    assert settings.NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS == 9
    assert not legacy.exists() and legacy.with_name('notification_settings.json.imported').exists()
    async with memory_session(monkeypatch, TABLES) as db:
        assert await NotificationSettingsService.import_legacy_file(db) == {}, 'второй старт — импортировать нечего'


@pytest.mark.asyncio
async def test_missing_or_broken_legacy_file_is_ignored(monkeypatch, tmp_path) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        assert await NotificationSettingsService.import_legacy_file(db) == {}
        broken = tmp_path / 'notification_settings.json'
        broken.write_text('{не json')
        monkeypatch.setattr(NotificationSettingsService, '_legacy_path', broken)
        assert await NotificationSettingsService.import_legacy_file(db) == {}
    assert broken.exists(), 'битый файл остаётся человеку'


@pytest.mark.asyncio
async def test_followups_do_not_query_when_all_three_switches_are_off(monkeypatch) -> None:
    """Все три выключены — мониторинг не ходит в базу и ничего не шлёт."""
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_1D_ENABLED', False)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED', False)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE3_ENABLED', False)
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())
    db = SimpleNamespace(execute=AsyncMock())
    await service._check_expired_subscription_followups(db)
    db.execute.assert_not_awaited()
    service.bot.send_message.assert_not_awaited()


def test_admin_menu_view_reflects_live_settings(monkeypatch) -> None:
    """Меню «Уведомления пользователям» строится из тех же живых значений, что читает мониторинг."""
    from app.handlers.admin import monitoring as admin_monitoring

    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED', False)
    monkeypatch.setattr(settings, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS', 7)
    text, keyboard = admin_monitoring._build_notification_settings_view('ru')
    assert '2-3 дня (скидка 10% / 24 ч): 🔴 Выкл' in text
    assert '7 дней (скидка 20% / 24 ч): 🟢 Вкл' in text
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert '🔴 Выкл • 2-3 дня со скидкой' in labels and '🟢 Вкл • 7 дней со скидкой' in labels
