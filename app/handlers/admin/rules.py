import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rules import clear_all_rules, create_or_update_rules, get_current_rules_content
from app.database.models import User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
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
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_RULES_MANAGEMENT_TEXT',
        '📋 <b>Управление правилами сервиса</b>\n\n'
        'Текущие правила показываются пользователям при регистрации и в главном меню.\n\n'
        'Выберите действие:',
    )

    keyboard = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RULES_BUTTON_EDIT', '📝 Редактировать правила'),
                callback_data='admin_edit_rules',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RULES_BUTTON_VIEW', '👀 Просмотр правил'),
                callback_data='admin_view_rules',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RULES_BUTTON_CLEAR', '🗑️ Очистить правила'),
                callback_data='admin_clear_rules',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RULES_BUTTON_HTML_HELP', 'ℹ️ Помощь по HTML'),
                callback_data='admin_rules_help',
            )
        ],
        [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')],
    ]

    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@admin_required
@error_handler
async def view_current_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        is_valid, error_msg = validate_html_tags(current_rules)
        warning = ''
        if not is_valid:
            warning = '\n\n' + texts.t(
                'ADMIN_RULES_HTML_WARNING',
                '⚠️ <b>Внимание:</b> В правилах найдена ошибка HTML: {error}',
            ).format(error=error_msg)

        await callback.message.edit_text(
            texts.t(
                'ADMIN_RULES_CURRENT_TEXT',
                '📋 <b>Текущие правила сервиса</b>\n\n{rules}{warning}',
            ).format(rules=current_rules, warning=warning),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_CAMPAIGN_EDIT', '✏️ Редактировать'),
                            callback_data='admin_edit_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_MONITORING_CLEAR', '🗑️ Очистить'),
                            callback_data='admin_clear_rules',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()
    except Exception as e:
        logger.error('Ошибка при показе правил', error=e)
        await callback.message.edit_text(
            texts.t(
                'ADMIN_RULES_LOAD_ERROR',
                '❌ Ошибка при загрузке правил. Возможно, в тексте есть некорректные HTML теги.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_CLEAR', '🗑️ Очистить правила'),
                            callback_data='admin_clear_rules',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def start_edit_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        current_rules = await get_current_rules_content(db, db_user.language)

        preview = _safe_preview(current_rules, 500)

        text = texts.t(
            'ADMIN_RULES_EDIT_TEXT',
            '✏️ <b>Редактирование правил</b>\n\n'
            '<b>Текущие правила:</b>\n<code>{preview}</code>\n\n'
            'Отправьте новый текст правил сервиса.\n\n'
            '<i>Поддерживается HTML разметка. Все теги будут проверены перед сохранением.</i>\n\n'
            '💡 <b>Совет:</b> Нажмите /html_help для просмотра поддерживаемых тегов',
        ).format(preview=preview)

        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_FAQ_HTML_HELP', 'ℹ️ HTML помощь'),
                            callback_data='admin_rules_help',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rules')],
                ]
            ),
        )

        await state.set_state(AdminStates.editing_rules_page)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка при начале редактирования правил', error=e)
        await callback.answer(
            texts.t('ADMIN_RULES_EDIT_LOAD_ERROR', '❌ Ошибка при загрузке правил для редактирования'),
            show_alert=True,
        )


@admin_required
@error_handler
async def process_rules_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    new_rules = message.text or ''

    if len(new_rules) > 4000:
        await message.answer(
            texts.t('ADMIN_RULES_TEXT_TOO_LONG', '❌ Текст правил слишком длинный (максимум 4000 символов)')
        )
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_RULES_HTML_ERROR_WITH_HELP',
                '❌ <b>Ошибка в HTML разметке:</b>\n{error}\n\n'
                'Пожалуйста, исправьте ошибки и отправьте текст заново.\n\n'
                '💡 Используйте /html_help для просмотра правильного синтаксиса',
            ).format(error=error_msg),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_FAQ_HTML_HELP', 'ℹ️ HTML помощь'),
                            callback_data='admin_rules_help',
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rules')],
                ]
            ),
        )
        return

    try:
        preview_text = texts.t(
            'ADMIN_RULES_PREVIEW_FULL',
            '📋 <b>Предварительный просмотр новых правил:</b>\n\n'
            '{rules}\n\n'
            '⚠️ <b>Внимание!</b> Новые правила будут показываться всем пользователям.\n\n'
            'Сохранить изменения?',
        ).format(rules=new_rules)

        if len(preview_text) > 4000:
            preview_text = texts.t(
                'ADMIN_RULES_PREVIEW_SHORT',
                '📋 <b>Предварительный просмотр новых правил:</b>\n\n'
                '{preview}\n\n'
                '⚠️ <b>Внимание!</b> Новые правила будут показываться всем пользователям.\n\n'
                'Текст правил: {length} символов\n'
                'Сохранить изменения?',
            ).format(
                preview=_safe_preview(new_rules, 500),
                length=len(new_rules),
            )

        await message.answer(
            preview_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_CAMPAIGNS_AUTO_010', '✅ Сохранить'),
                            callback_data='admin_save_rules',
                        ),
                        types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)

    except Exception as e:
        logger.error('Ошибка при показе превью правил', error=e)
        await message.answer(
            texts.t(
                'ADMIN_RULES_SAVE_CONFIRMATION_TEXT',
                '⚠️ <b>Подтверждение сохранения правил</b>\n\n'
                'Новые правила готовы к сохранению ({length} символов).\n'
                'HTML теги проверены и корректны.\n\n'
                'Сохранить изменения?',
            ).format(length=len(new_rules)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_CAMPAIGNS_AUTO_010', '✅ Сохранить'),
                            callback_data='admin_save_rules',
                        ),
                        types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rules'),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)


@admin_required
@error_handler
async def save_rules(callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    new_rules = data.get('new_rules')

    if not new_rules:
        await callback.answer(
            texts.t('ADMIN_RULES_SAVE_TEXT_NOT_FOUND', '❌ Ошибка: текст правил не найден'),
            show_alert=True,
        )
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_RULES_SAVE_HTML_ERROR',
                '❌ <b>Ошибка при сохранении:</b>\n{error}\n\n'
                'Правила не были сохранены из-за ошибок в HTML разметке.',
            ).format(error=error_msg),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_LOG_ANALYSIS_RETRY', '🔄 Попробовать снова'),
                            callback_data='admin_edit_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_TO_RULES', '📋 К правилам'),
                            callback_data='admin_rules',
                        )
                    ],
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
            texts.t(
                'ADMIN_RULES_SAVE_SUCCESS_TEXT',
                '✅ <b>Правила сервиса успешно обновлены!</b>\n\n'
                '✓ Новые правила сохранены в базе данных\n'
                '✓ HTML теги проверены и корректны\n'
                '✓ Кеш правил очищен и обновлен\n'
                '✓ Правила будут показываться пользователям\n\n'
                '📊 Размер текста: {length} символов',
            ).format(length=len(new_rules)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_VIEW_SHORT', '👀 Просмотреть'),
                            callback_data='admin_view_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_TO_RULES', '📋 К правилам'),
                            callback_data='admin_rules',
                        )
                    ],
                ]
            ),
        )

        await state.clear()
        logger.info('Правила сервиса обновлены администратором', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка сохранения правил', error=e)
        await callback.message.edit_text(
            texts.t(
                'ADMIN_RULES_SAVE_DB_ERROR',
                '❌ <b>Ошибка при сохранении правил</b>\n\n'
                'Произошла ошибка при записи в базу данных. Попробуйте еще раз.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_REFERRALS_LOG_ANALYSIS_RETRY', '🔄 Попробовать снова'),
                            callback_data='admin_save_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_TO_RULES', '📋 К правилам'),
                            callback_data='admin_rules',
                        )
                    ],
                ]
            ),
        )
        await callback.answer()


@admin_required
@error_handler
async def clear_rules_confirmation(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_RULES_CLEAR_CONFIRM_TEXT',
            '🗑️ <b>Очистка правил сервиса</b>\n\n'
            '⚠️ <b>ВНИМАНИЕ!</b> Вы собираетесь полностью удалить все правила сервиса.\n\n'
            'После очистки пользователи будут видеть стандартные правила по умолчанию.\n\n'
            'Это действие нельзя отменить. Продолжить?',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_MONITORING_CONFIRM_CLEAR', '✅ Да, очистить'),
                        callback_data='admin_confirm_clear_rules',
                    ),
                    types.InlineKeyboardButton(text=texts.CANCEL, callback_data='admin_rules'),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def confirm_clear_rules(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        await clear_all_rules(db, db_user.language)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        await callback.message.edit_text(
            texts.t(
                'ADMIN_RULES_CLEAR_SUCCESS_TEXT',
                '✅ <b>Правила успешно очищены!</b>\n\n'
                '✓ Все пользовательские правила удалены\n'
                '✓ Теперь используются стандартные правила\n'
                '✓ Кеш правил очищен\n\n'
                'Пользователи будут видеть правила по умолчанию.',
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_CREATE_NEW', '📝 Создать новые'),
                            callback_data='admin_edit_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_VIEW_CURRENT', '👀 Посмотреть текущие'),
                            callback_data='admin_view_rules',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_BUTTON_TO_RULES', '📋 К правилам'),
                            callback_data='admin_rules',
                        )
                    ],
                ]
            ),
        )

        logger.info('Правила очищены администратором', telegram_id=db_user.telegram_id)
        await callback.answer()

    except Exception as e:
        logger.error('Ошибка при очистке правил', error=e)
        await callback.answer(
            texts.t('ADMIN_RULES_CLEAR_ERROR_ALERT', '❌ Ошибка при очистке правил'),
            show_alert=True,
        )


@admin_required
@error_handler
async def show_html_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    help_text = get_html_help_text()

    await callback.message.edit_text(
        texts.t(
            'ADMIN_RULES_HTML_HELP_TEXT',
            'ℹ️ <b>Справка по HTML форматированию</b>\n\n{help_text}',
        ).format(help_text=help_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_RULES_BUTTON_EDIT', '📝 Редактировать правила'),
                        callback_data='admin_edit_rules',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_rules')],
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_rules_management, F.data == 'admin_rules')
    dp.callback_query.register(view_current_rules, F.data == 'admin_view_rules')
    dp.callback_query.register(start_edit_rules, F.data == 'admin_edit_rules')
    dp.callback_query.register(save_rules, F.data == 'admin_save_rules')

    dp.callback_query.register(clear_rules_confirmation, F.data == 'admin_clear_rules')
    dp.callback_query.register(confirm_clear_rules, F.data == 'admin_confirm_clear_rules')

    dp.callback_query.register(show_html_help, F.data == 'admin_rules_help')

    dp.message.register(process_rules_edit, AdminStates.editing_rules_page)
