import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rules import clear_all_rules, create_or_update_rules, get_current_rules_content
from app.database.models import User
from app.handlers.admin.display_mode_button import cycle_display_mode_setting
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.display_mode import display_mode_label
from app.utils.validators import get_html_help_text, validate_html_tags


def _safe_preview(html_text: str, limit: int = 500) -> str:
    """Создаёт превью текста, безопасно обрезая HTML-теги."""
    plain = re.sub(r'<[^>]+>', '', html_text)
    if len(plain) <= limit:
        return plain
    return plain[:limit] + '...'


logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_rules_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    text = """
📋 <b>Управление правилами сервиса</b>

Текущие правила показываются пользователям при регистрации и в главном меню.

Выберите действие:
"""

    keyboard = [
        [types.InlineKeyboardButton(text='📝 Редактировать правила', callback_data='admin_edit_rules')],
        [types.InlineKeyboardButton(text='👀 Просмотр правил', callback_data='admin_view_rules')],
        [types.InlineKeyboardButton(text='🗑️ Очистить правила', callback_data='admin_clear_rules')],
        [
            types.InlineKeyboardButton(
                text=f'👁 Отображение: {display_mode_label(settings.SERVICE_RULES_DISPLAY_MODE)}',
                callback_data='admin_rules_display_mode',
            )
        ],
        [types.InlineKeyboardButton(text='ℹ️ Помощь по HTML', callback_data='admin_rules_help')],
        [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_submenu_settings')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def cycle_rules_display_mode(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    new_mode = await cycle_display_mode_setting(callback, db, 'SERVICE_RULES_DISPLAY_MODE')
    if new_mode is None:
        return
    await callback.answer(f'Отображение: {display_mode_label(new_mode)}')
    await show_rules_management(callback, db_user=db_user, db=db)


@admin_required
@error_handler
async def view_current_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        is_valid, error_msg = validate_html_tags(current_rules)
        warning = ''
        if not is_valid:
            warning = f'\n\n⚠️ <b>Внимание:</b> В правилах найдена ошибка HTML: {error_msg}'

        await callback.message.edit_text(
            f'📋 <b>Текущие правила сервиса</b>\n\n{current_rules}{warning}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='✏️ Редактировать', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='🗑️ Очистить', callback_data='admin_clear_rules')],
                    [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()
    except Exception as e:
        logger.error('Ошибка при показе правил', error=e)
        await callback.message.edit_text(
            '❌ Ошибка при загрузке правил. Возможно, в тексте есть некорректные HTML теги.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🗑️ Очистить правила', callback_data='admin_clear_rules')],
                    [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def start_edit_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        preview = _safe_preview(current_rules, 500)

        text = (
            '✏️ <b>Редактирование правил</b>\n\n'
            f'<b>Текущие правила:</b>\n<code>{preview}</code>\n\n'
            'Отправьте новый текст правил сервиса.\n\n'
            '<i>Поддерживается HTML разметка. Все теги будут проверены перед сохранением.</i>\n\n'
            '💡 <b>Совет:</b> Нажмите /html_help для просмотра поддерживаемых тегов'
        )

        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='ℹ️ HTML помощь', callback_data='admin_rules_help')],
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_rules')],
                ]
            ),
        )

        await state.set_state(AdminStates.editing_rules_page)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка при начале редактирования правил', error=e)
        await callback.answer('❌ Ошибка при загрузке правил для редактирования', show_alert=True)


@admin_required
@error_handler
async def process_rules_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    new_rules = message.text

    if len(new_rules) > 4000:
        await message.answer('❌ Текст правил слишком длинный (максимум 4000 символов)')
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await message.answer(
            f'❌ <b>Ошибка в HTML разметке:</b>\n{error_msg}\n\n'
            f'Пожалуйста, исправьте ошибки и отправьте текст заново.\n\n'
            f'💡 Используйте /html_help для просмотра правильного синтаксиса',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='ℹ️ HTML помощь', callback_data='admin_rules_help')],
                    [types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_rules')],
                ]
            ),
        )
        return

    try:
        preview_text = f'📋 <b>Предварительный просмотр новых правил:</b>\n\n{new_rules}\n\n'
        preview_text += '⚠️ <b>Внимание!</b> Новые правила будут показываться всем пользователям.\n\n'
        preview_text += 'Сохранить изменения?'

        if len(preview_text) > 4000:
            preview_text = (
                '📋 <b>Предварительный просмотр новых правил:</b>\n\n'
                f'{_safe_preview(new_rules, 500)}\n\n'
                f'⚠️ <b>Внимание!</b> Новые правила будут показываться всем пользователям.\n\n'
                f'Текст правил: {len(new_rules)} символов\n'
                f'Сохранить изменения?'
            )

        await message.answer(
            preview_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text='✅ Сохранить', callback_data='admin_save_rules'),
                        types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)

    except Exception as e:
        logger.error('Ошибка при показе превью правил', error=e)
        await message.answer(
            '⚠️ <b>Подтверждение сохранения правил</b>\n\n'
            f'Новые правила готовы к сохранению ({len(new_rules)} символов).\n'
            f'HTML теги проверены и корректны.\n\n'
            f'Сохранить изменения?',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text='✅ Сохранить', callback_data='admin_save_rules'),
                        types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)


@admin_required
@error_handler
async def save_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    data = await state.get_data()
    new_rules = data.get('new_rules')

    if not new_rules:
        await callback.answer('❌ Ошибка: текст правил не найден', show_alert=True)
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await callback.message.edit_text(
            f'❌ <b>Ошибка при сохранении:</b>\n{error_msg}\n\nПравила не были сохранены из-за ошибок в HTML разметке.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='📋 К правилам', callback_data='admin_rules')],
                ]
            ),
        )
        await state.clear()
        await callback.answer()
        return

    try:
        await create_or_update_rules(db=db, content=new_rules, language=db_user.language)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        from app.localization.texts import refresh_rules_cache

        await refresh_rules_cache(db_user.language)

        await callback.message.edit_text(
            '✅ <b>Правила сервиса успешно обновлены!</b>\n\n'
            '✓ Новые правила сохранены в базе данных\n'
            '✓ HTML теги проверены и корректны\n'
            '✓ Кеш правил очищен и обновлен\n'
            '✓ Правила будут показываться пользователям\n\n'
            f'📊 Размер текста: {len(new_rules)} символов',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='👀 Просмотреть', callback_data='admin_view_rules')],
                    [types.InlineKeyboardButton(text='📋 К правилам', callback_data='admin_rules')],
                ]
            ),
        )

        await state.clear()
        logger.info('Правила сервиса обновлены администратором', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка сохранения правил', error=e)
        await callback.message.edit_text(
            '❌ <b>Ошибка при сохранении правил</b>\n\nПроизошла ошибка при записи в базу данных. Попробуйте еще раз.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='admin_save_rules')],
                    [types.InlineKeyboardButton(text='📋 К правилам', callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def clear_rules_confirmation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        '🗑️ <b>Очистка правил сервиса</b>\n\n'
        '⚠️ <b>ВНИМАНИЕ!</b> Вы собираетесь полностью удалить все правила сервиса.\n\n'
        'После очистки пользователи будут видеть стандартные правила по умолчанию.\n\n'
        'Это действие нельзя отменить. Продолжить?',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text='✅ Да, очистить', callback_data='admin_confirm_clear_rules'),
                    types.InlineKeyboardButton(text='❌ Отмена', callback_data='admin_rules'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_clear_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        await clear_all_rules(db, db_user.language)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        await callback.message.edit_text(
            '✅ <b>Правила успешно очищены!</b>\n\n'
            '✓ Все пользовательские правила удалены\n'
            '✓ Теперь используются стандартные правила\n'
            '✓ Кеш правил очищен\n\n'
            'Пользователи будут видеть правила по умолчанию.',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text='📝 Создать новые', callback_data='admin_edit_rules')],
                    [types.InlineKeyboardButton(text='👀 Посмотреть текущие', callback_data='admin_view_rules')],
                    [types.InlineKeyboardButton(text='📋 К правилам', callback_data='admin_rules')],
                ]
            ),
        )

        logger.info('Правила очищены администратором', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка при очистке правил', error=e)
        await callback.answer('❌ Ошибка при очистке правил', show_alert=True)


@admin_required
@error_handler
async def show_html_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    help_text = get_html_help_text()

    await callback.message.edit_text(
        f'ℹ️ <b>Справка по HTML форматированию</b>\n\n{help_text}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='📝 Редактировать правила', callback_data='admin_edit_rules')],
                [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_rules')],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_rules_management, F.data == 'admin_rules')
    dp.callback_query.register(cycle_rules_display_mode, F.data == 'admin_rules_display_mode')
    dp.callback_query.register(view_current_rules, F.data == 'admin_view_rules')
    dp.callback_query.register(start_edit_rules, F.data == 'admin_edit_rules')
    dp.callback_query.register(save_rules, F.data == 'admin_save_rules')

    dp.callback_query.register(clear_rules_confirmation, F.data == 'admin_clear_rules')
    dp.callback_query.register(confirm_clear_rules, F.data == 'admin_confirm_clear_rules')

    dp.callback_query.register(show_html_help, F.data == 'admin_rules_help')

    dp.message.register(process_rules_edit, AdminStates.editing_rules_page)
