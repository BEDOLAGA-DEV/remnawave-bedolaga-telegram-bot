from aiogram import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.localization.texts import get_texts
from app.services.system_settings_service import BotConfigurationService
from app.utils.display_mode import next_display_mode


async def cycle_display_mode_setting(
    callback: types.CallbackQuery,
    db: AsyncSession,
    key: str,
    language: str = 'ru',
) -> str | None:
    if BotConfigurationService.is_env_overridden(key):
        await callback.answer(
            get_texts(language).t(
                'DISPLAY_MODE_ENV_OVERRIDE', '🔒 Значение задано через .env, измените переменную окружения'
            ),
            show_alert=True,
        )
        return None

    new_mode = next_display_mode(getattr(settings, key, None))
    await BotConfigurationService.set_value(db, key, new_mode)
    return new_mode
