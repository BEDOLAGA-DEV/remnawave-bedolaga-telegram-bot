"""Режим поддержки из JSON не доезжал до ``settings`` после рестарта.

``SupportSettingsService`` хранит режим в ``data/support_settings.json``.
``set_system_mode`` писал JSON *и* обновлял ``settings.SUPPORT_SYSTEM_MODE`` в
памяти — поэтому в рамках одного запуска всё выглядело согласованно. Но
``_load`` при старте поднимал JSON и ``settings`` не трогал.

После рестарта источников истины становилось два:

* бот читает режим через сервис — видит значение из JSON;
* веб-кабинет (``cabinet/routes/tickets.py``, ``cabinet/routes/info.py``)
  читает ``settings.is_support_tickets_enabled()`` — видит значение из ``.env``.

Итог: режим ``contact``, выставленный из админки бота, после рестарта в
кабинете игнорировался и тикеты снова открывались.

Фикс: ``_load`` зеркалит persisted-значения в ``settings`` через
``_sync_settings``.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services.support_settings_service import SupportSettingsService


@pytest.fixture
def support_storage(tmp_path, monkeypatch):
    """Изолированное JSON-хранилище + сброс кеша класса на каждый тест."""
    storage = tmp_path / 'support_settings.json'
    monkeypatch.setattr(SupportSettingsService, '_storage_path', storage)
    monkeypatch.setattr(SupportSettingsService, '_data', {})
    monkeypatch.setattr(SupportSettingsService, '_loaded', False)
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'both')
    monkeypatch.setattr(settings, 'SUPPORT_MENU_ENABLED', True)
    return storage


def _simulate_restart(monkeypatch) -> None:
    """Сбросить кеш класса, как при старте нового процесса."""
    monkeypatch.setattr(SupportSettingsService, '_data', {})
    monkeypatch.setattr(SupportSettingsService, '_loaded', False)


def test_load_syncs_system_mode_into_settings(support_storage, monkeypatch):
    """REGRESSION: persisted-режим должен доезжать до settings при загрузке —
    иначе кабинет продолжает отдавать значение из .env."""
    support_storage.write_text(json.dumps({'system_mode': 'contact'}), encoding='utf-8')

    assert SupportSettingsService.get_system_mode() == 'contact'
    assert settings.SUPPORT_SYSTEM_MODE == 'contact'
    # Главное следствие: кабинетный guard тикетов теперь согласован с ботом
    assert settings.is_support_tickets_enabled() is False


def test_mode_survives_restart_for_cabinet(support_storage, monkeypatch):
    """REGRESSION (сквозной сценарий): админ выключил тикеты в боте, бот
    перезапустился — кабинет обязан по-прежнему считать тикеты выключенными."""
    assert SupportSettingsService.set_system_mode('contact') is True
    assert settings.is_support_tickets_enabled() is False

    # Рестарт: settings поднимается из .env, кеш сервиса пуст
    _simulate_restart(monkeypatch)
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'both')

    SupportSettingsService._load()

    assert settings.SUPPORT_SYSTEM_MODE == 'contact'
    assert settings.is_support_tickets_enabled() is False


def test_load_syncs_menu_enabled_into_settings(support_storage):
    """REGRESSION: у menu_enabled была ровно та же проблема."""
    support_storage.write_text(json.dumps({'menu_enabled': False}), encoding='utf-8')

    assert SupportSettingsService.is_support_menu_enabled() is False
    assert settings.SUPPORT_MENU_ENABLED is False


def test_set_support_menu_enabled_syncs_settings(support_storage):
    """Сеттер меню тоже обязан обновлять settings (раньше не обновлял вовсе)."""
    assert SupportSettingsService.set_support_menu_enabled(False) is True
    assert settings.SUPPORT_MENU_ENABLED is False


def test_absent_json_keeps_env_value(support_storage):
    """Без сохранённого значения settings остаётся как задан в .env."""
    SupportSettingsService._load()

    assert settings.SUPPORT_SYSTEM_MODE == 'both'
    assert settings.SUPPORT_MENU_ENABLED is True


def test_invalid_persisted_mode_does_not_clobber_settings(support_storage):
    """Мусор в JSON не должен затирать settings невалидным режимом."""
    support_storage.write_text(json.dumps({'system_mode': 'nonsense'}), encoding='utf-8')

    assert SupportSettingsService.get_system_mode() == 'both'  # нормализация
    assert settings.SUPPORT_SYSTEM_MODE == 'both'


def test_corrupt_json_does_not_clobber_settings(support_storage, monkeypatch):
    """Битый JSON: _load глотает ошибку, settings остаётся из .env."""
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'tickets')
    support_storage.write_text('{not json', encoding='utf-8')

    SupportSettingsService._load()

    assert settings.SUPPORT_SYSTEM_MODE == 'tickets'
