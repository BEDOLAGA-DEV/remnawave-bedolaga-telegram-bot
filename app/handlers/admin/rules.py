import logging
import re

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


logger = logging.getLogger(__name__)


@admin_required
@error_handler
async def show_rules_management(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t('ADMIN_RULES_MANAGEMENT_TEXT')

    keyboard = [
        [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_EDIT_BUTTON'), callback_data='admin_edit_rules')],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_VIEW_BUTTON'), callback_data='admin_view_rules')],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_CLEAR_BUTTON'), callback_data='admin_clear_rules')],
        [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_HTML_HELP_BUTTON'), callback_data='admin_rules_help')],
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_RULES_BACK_TO_SETTINGS_BUTTON'), callback_data='admin_submenu_settings'
            )
        ],
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
            warning = texts.t('ADMIN_RULES_HTML_WARNING').format(error=error_msg)

        await callback.message.edit_text(
            texts.t('ADMIN_RULES_CURRENT_TEXT').format(rules=current_rules, warning=warning),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_EDIT_SHORT_BUTTON'), callback_data='admin_edit_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CLEAR_SHORT_BUTTON'), callback_data='admin_clear_rules'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_BACK_BUTTON'), callback_data='admin_rules')],
                ]
            ),
        )
        await callback.answer()
    except Exception as e:
        logger.error(f'Ошибка при показе правил: {e}')
        await callback.message.edit_text(
            texts.t('ADMIN_RULES_LOAD_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CLEAR_BUTTON'), callback_data='admin_clear_rules'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_BACK_BUTTON'), callback_data='admin_rules')],
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
        text = texts.t('ADMIN_RULES_EDIT_PROMPT').format(preview=preview)

        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_HTML_HELP_SHORT_BUTTON'), callback_data='admin_rules_help'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CANCEL_BUTTON'), callback_data='admin_rules'
                        )
                    ],
                ]
            ),
        )

        await state.set_state(AdminStates.editing_rules_page)
        await callback.answer()

    except Exception as e:
        logger.error(f'Ошибка при начале редактирования правил: {e}')
        await callback.answer(texts.t('ADMIN_RULES_EDIT_LOAD_ERROR_ALERT'), show_alert=True)


@admin_required
@error_handler
async def process_rules_edit(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    texts = get_texts(db_user.language)
    new_rules = message.text

    if len(new_rules) > 4000:
        await message.answer(texts.t('ADMIN_RULES_TEXT_TOO_LONG'))
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await message.answer(
            texts.t('ADMIN_RULES_HTML_INVALID').format(error=error_msg),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_HTML_HELP_SHORT_BUTTON'), callback_data='admin_rules_help'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CANCEL_BUTTON'), callback_data='admin_rules'
                        )
                    ],
                ]
            ),
        )
        return

    try:
        preview_text = texts.t('ADMIN_RULES_PREVIEW_TEXT').format(rules=new_rules)

        if len(preview_text) > 4000:
            preview_text = texts.t('ADMIN_RULES_PREVIEW_TRUNCATED_TEXT').format(
                rules_preview=_safe_preview(new_rules, 500), length=len(new_rules)
            )

        await message.answer(
            preview_text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_SAVE_BUTTON'), callback_data='admin_save_rules'
                        ),
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CANCEL_BUTTON'), callback_data='admin_rules'
                        ),
                    ]
                ]
            ),
        )

        await state.update_data(new_rules=new_rules)

    except Exception as e:
        logger.error(f'Ошибка при показе превью правил: {e}')
        await message.answer(
            texts.t('ADMIN_RULES_SAVE_CONFIRM_FALLBACK').format(length=len(new_rules)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_SAVE_BUTTON'), callback_data='admin_save_rules'
                        ),
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CANCEL_BUTTON'), callback_data='admin_rules'
                        ),
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
        await callback.answer(texts.t('ADMIN_RULES_TEXT_NOT_FOUND_ALERT'), show_alert=True)
        return

    is_valid, error_msg = validate_html_tags(new_rules)
    if not is_valid:
        await callback.message.edit_text(
            texts.t('ADMIN_RULES_SAVE_HTML_ERROR').format(error=error_msg),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_RETRY_BUTTON'), callback_data='admin_edit_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_TO_RULES_BUTTON'), callback_data='admin_rules'
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
            texts.t('ADMIN_RULES_SAVE_SUCCESS').format(length=len(new_rules)),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_VIEW_CURRENT_BUTTON'), callback_data='admin_view_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_TO_RULES_BUTTON'), callback_data='admin_rules'
                        )
                    ],
                ]
            ),
        )

        await state.clear()
        logger.info(f'Правила сервиса обновлены администратором {db_user.telegram_id}')
        await callback.answer()

    except Exception as e:
        logger.error(f'Ошибка сохранения правил: {e}')
        await callback.message.edit_text(
            texts.t('ADMIN_RULES_SAVE_ERROR'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_RETRY_BUTTON'), callback_data='admin_save_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_TO_RULES_BUTTON'), callback_data='admin_rules'
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
        texts.t('ADMIN_RULES_CLEAR_CONFIRM_TEXT'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_RULES_CONFIRM_CLEAR_BUTTON'), callback_data='admin_confirm_clear_rules'
                    ),
                    types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_CANCEL_BUTTON'), callback_data='admin_rules'),
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
            texts.t('ADMIN_RULES_CLEAR_SUCCESS'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_CREATE_NEW_BUTTON'), callback_data='admin_edit_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_VIEW_CURRENT_LONG_BUTTON'), callback_data='admin_view_rules'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('ADMIN_RULES_TO_RULES_BUTTON'), callback_data='admin_rules'
                        )
                    ],
                ]
            ),
        )

        logger.info(f'Правила очищены администратором {db_user.telegram_id}')
        await callback.answer()

    except Exception as e:
        logger.error(f'Ошибка при очистке правил: {e}')
        await callback.answer(texts.t('ADMIN_RULES_CLEAR_ERROR_ALERT'), show_alert=True)


@admin_required
@error_handler
async def show_html_help(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    help_text = get_html_help_text()

    await callback.message.edit_text(
        texts.t('ADMIN_RULES_HTML_HELP_TEXT').format(help=help_text),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_EDIT_BUTTON'), callback_data='admin_edit_rules')],
                [types.InlineKeyboardButton(text=texts.t('ADMIN_RULES_BACK_BUTTON'), callback_data='admin_rules')],
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
