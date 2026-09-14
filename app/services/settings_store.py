"""Изменяемые на ходу настройки бота живут в базе — общие помощники для сервисов-фасадов.

Раньше часть сервисов держала своё состояние в JSON-файлах в ``data/`` с вечным кэшем в памяти
процесса: файл не переживал контейнер и не был виден второму процессу, а кабинет его не видел.
Теперь такие настройки — обычные поля ``Settings``: запись через слой системных настроек (база
плюс живой процесс), чтение живьём из ``settings``. Старый файл переносится в базу один раз.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

IMPORTED_SUFFIX = '.imported'


async def store_setting(db: AsyncSession, key: str, value: Any) -> bool:
    """Сохранить настройку в базу и применить к процессу; ошибка — в лог, ответ False."""
    # Локальный импорт: слой системных настроек тянет половину конфигурации.
    from app.services.system_settings_service import bot_configuration_service

    try:
        await bot_configuration_service.set_value(db, key, value)
    except Exception as error:
        logger.error('Не удалось сохранить настройку', key=key, error=error)
        return False
    return True


def read_legacy_json(path: Path) -> dict[str, Any] | None:
    """Содержимое старого файла настроек; None — файла нет или он не читается (остаётся человеку)."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding='utf-8') or '{}')
    except (OSError, ValueError) as error:
        logger.error('Старый файл настроек не читается — оставлен как есть', path=str(path), error=error)
        return None
    return raw if isinstance(raw, dict) else {}


async def import_legacy_values(db: AsyncSession, path: Path, values: Mapping[str, Any]) -> dict[str, Any]:
    """Перенести значения из старого файла в базу и переименовать файл.

    Уже заданное в базе главнее файла. Возвращает перенесённые ключи со значениями.
    """
    from app.services.system_settings_service import bot_configuration_service

    imported: dict[str, Any] = {}
    for key, value in values.items():
        if bot_configuration_service.has_override(key):
            continue
        await bot_configuration_service.set_value(db, key, value, commit=False)
        imported[key] = value
    if imported:
        await db.commit()
    try:
        await asyncio.to_thread(path.rename, path.with_name(path.name + IMPORTED_SUFFIX))
    except OSError as error:
        logger.warning('Не удалось переименовать старый файл настроек', path=str(path), error=error)
    logger.info('Настройки перенесены из файла в базу', path=str(path), keys=sorted(imported))
    return imported
