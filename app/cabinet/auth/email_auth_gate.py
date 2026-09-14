"""Выключатель email-входа в кабинете — один на UI, API и список провайдеров.

Переключатель в админке (PATCH /cabinet/branding/email-auth) пишет строку
`CABINET_EMAIL_AUTH_ENABLED` в system_settings и не трогает `settings` в памяти,
а `settings` из окружения о ней не знает. Поэтому единственно честный ответ на
вопрос «включён ли email-вход» — строка в БД, если она есть, иначе окружение.
Именно так его показывает публичный GET /cabinet/branding/email-auth, и ровно так
же должны решать роуты: иначе кнопка пропадает, а прямые запросы к API проходят.
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.services.system_settings_service import BotConfigurationService


logger = structlog.get_logger(__name__)


EMAIL_AUTH_ENABLED_KEY = 'CABINET_EMAIL_AUTH_ENABLED'  # в БД хранится "true" / "false"
EMAIL_AUTH_DISABLED_CODE = 'email_auth_disabled'


def _parse_stored_flag(stored: str) -> bool | None:
    """Тем же парсером, что общий редактор настроек: строку пишут и админский
    переключатель ('true'), и редактор, и перенос .env в БД (сырое '1')."""
    try:
        return bool(BotConfigurationService.deserialize_value(EMAIL_AUTH_ENABLED_KEY, stored))
    except (ValueError, KeyError):
        logger.warning('Нечитаемое значение флага email-входа в БД, берём окружение', value=stored)
        return None


async def is_email_auth_enabled(db: AsyncSession) -> bool:
    """Строка в system_settings важнее значения из окружения."""
    stored = await get_setting_value(db, EMAIL_AUTH_ENABLED_KEY)
    parsed = _parse_stored_flag(stored) if stored is not None else None
    if parsed is not None:
        return parsed
    return settings.is_cabinet_email_auth_enabled()


async def require_email_auth_enabled(db: AsyncSession) -> None:
    """Первый шаг любого email/password-роута: до rate-limit и до таблицы users."""
    if await is_email_auth_enabled(db):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={'code': EMAIL_AUTH_DISABLED_CODE, 'message': 'Email authentication is disabled'},
    )
