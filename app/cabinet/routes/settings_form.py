"""Сохранение формы админки в system_settings.

Формы с несколькими полями (партнёрка, тикеты) раньше меняли ``settings`` в памяти
и переписывали файл ``.env`` — в контейнере это не тот файл, что читает docker
при старте, или его нет вовсе, и после перезагрузки всё возвращалось. Все
настройки идут через ``bot_configuration_service``: строка в базе, применение
в памяти, а ключ, закреплённый в ``.env``, база перекрыть не может — о нём
форма сообщает в ``env_locked``, чтобы кабинет показал подсказку и не давал
крутить бесполезный переключатель.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_settings_service import bot_configuration_service


async def save_settings_form(db: AsyncSession, updates: Mapping[str, Any]) -> None:
    """Записать значения по ключам Settings; ``set_value`` сам применяет их в памяти."""
    for key, value in updates.items():
        await bot_configuration_service.set_value(db, key, value)
    await db.commit()


def env_locked_fields(field_keys: Mapping[str, str]) -> list[str]:
    """Имена полей формы, чьи ключи закреплены в .env (порядок — как в форме)."""
    return [field for field, key in field_keys.items() if bot_configuration_service.is_env_locked(key)]


def form_updates(field_keys: Mapping[str, str], values: Mapping[str, Any]) -> dict[str, Any]:
    """Переданные (не None) поля формы → ключи Settings."""
    return {field_keys[field]: value for field, value in values.items() if value is not None and field in field_keys}
