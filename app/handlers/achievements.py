import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.achievement import (
    _get_user_stat,
    get_active_templates,
    get_user_achievements,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def show_achievements(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    if not settings.ACHIEVEMENTS_ENABLED:
        await callback.answer('\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u044b', show_alert=True)
        return

    texts = get_texts(db_user.language)
    templates = await get_active_templates(db)
    user_achievements = await get_user_achievements(db, db_user.id)
    unlocked_ids = {ua.template_id: ua for ua in user_achievements}

    lines = ['\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f</b>\n']

    for template in templates:
        if template.id in unlocked_ids:
            ua = unlocked_ids[template.id]
            date_str = ua.unlocked_at.strftime('%d.%m.%Y') if ua.unlocked_at else ''
            lines.append(
                f'\u2705 {template.emoji} <b>{template.name}</b> \u2014 {date_str}'
            )
        else:
            # Show progress
            current = await _get_user_stat(db, db_user, template.condition_type)
            target = template.condition_value
            lines.append(
                f'\U0001f512 {template.emoji} <b>{template.name}</b> \u2014 {current}/{target}'
            )

    if not templates:
        lines.append('\n\u041f\u043e\u043a\u0430 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 \u043d\u0435\u0442.')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BACK, callback_data='menu')],
        ]
    )

    await callback.message.edit_text(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=keyboard,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_achievements, F.data == 'nz!_achievements')
