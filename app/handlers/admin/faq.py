import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.handlers.admin.display_mode_button import cycle_display_mode_setting
from app.localization.texts import get_texts
from app.services.faq_service import FaqService
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
from app.utils.display_mode import display_mode_label
from app.utils.validators import get_html_help_text, validate_html_tags


logger = structlog.get_logger(__name__)


def _format_timestamp(value: datetime | None) -> str:
    if not value:
        return ''
    try:
        return value.strftime('%d.%m.%Y %H:%M')
    except Exception:
        return ''


async def _build_overview(
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    normalized_language = FaqService.normalize_language(db_user.language)
    setting = await FaqService.get_setting(
        db,
        db_user.language,
        fallback=False,
    )

    pages = await FaqService.get_pages(
        db,
        db_user.language,
        include_inactive=True,
        fallback=False,
    )

    total_pages = len(pages)
    active_pages = sum(1 for page in pages if page.is_active)

    description = texts.t(
        'ADMIN_FAQ_DESCRIPTION',
        'FAQ отображается в разделе «Инфо».',
    )

    if setting and not setting.is_enabled:
        status_text = texts.t(
            'ADMIN_FAQ_STATUS_DISABLED',
            '⚠️ Показ FAQ выключен.',
        )
    elif active_pages:
        status_text = texts.t(
            'ADMIN_FAQ_STATUS_ENABLED',
            '✅ FAQ включён. Активных страниц: {count}.',
        ).format(count=active_pages)
    elif total_pages:
        status_text = texts.t(
            'ADMIN_FAQ_STATUS_ENABLED_EMPTY',
            '⚠️ FAQ включён, но нет активных страниц.',
        )
    else:
        status_text = texts.t(
            'ADMIN_FAQ_STATUS_EMPTY',
            '⚠️ FAQ ещё не настроен.',
        )

    pages_overview = texts.t(
        'ADMIN_FAQ_PAGES_EMPTY',
        'Страницы ещё не созданы.',
    )

    if pages:
        rows: list[str] = []
        for index, page in enumerate(pages, start=1):
            title = (page.title or '').strip()
            if not title:
                title = texts.t('FAQ_PAGE_UNTITLED', 'Без названия')
            if len(title) > 60:
                title = f'{title[:57]}...'

            status_label = texts.t(
                'ADMIN_FAQ_PAGE_STATUS_ACTIVE',
                '✅ Активна',
            )
            if not page.is_active:
                status_label = texts.t(
                    'ADMIN_FAQ_PAGE_STATUS_INACTIVE',
                    '🚫 Выключена',
                )

            updated = _format_timestamp(getattr(page, 'updated_at', None))
            updated_block = f' ({updated})' if updated else ''
            rows.append(f'{index}. {html.escape(title)} — {status_label}{updated_block}')

        pages_list_header = texts.t(
            'ADMIN_FAQ_PAGES_OVERVIEW',
            '<b>Список страниц:</b>\n{items}',
        )
        pages_overview = pages_list_header.format(items='\n'.join(rows))

    language_block = texts.t(
        'ADMIN_FAQ_LANGUAGE',
        'Язык: <code>{lang}</code>',
    ).format(lang=normalized_language)

    stats_block = texts.t(
        'ADMIN_FAQ_PAGE_STATS',
        'Всего страниц: {total}',
    ).format(total=total_pages)

    header = texts.t('ADMIN_FAQ_HEADER', '❓ <b>FAQ</b>')
    actions_prompt = texts.t(
        'ADMIN_FAQ_ACTION_PROMPT',
        'Выберите действие:',
    )

    message_parts = [
        header,
        description,
        language_block,
        status_text,
        stats_block,
        pages_overview,
        actions_prompt,
    ]

    overview_text = '\n\n'.join(part for part in message_parts if part)

    buttons: list[list[types.InlineKeyboardButton]] = []

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_FAQ_ADD_PAGE_BUTTON',
                    '➕ Добавить страницу',
                ),
                callback_data='admin_faq_create',
            )
        ]
    )

    for page in pages[:25]:
        title = (page.title or '').strip()
        if not title:
            title = texts.t('FAQ_PAGE_UNTITLED', 'Без названия')
        if len(title) > 40:
            title = f'{title[:37]}...'
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'{page.display_order}. {title}',
                    callback_data=f'admin_faq_page:{page.id}',
                )
            ]
        )

    toggle_text = texts.t(
        'ADMIN_FAQ_ENABLE_BUTTON',
        '✅ Включить показ',
    )
    if setting and setting.is_enabled:
        toggle_text = texts.t(
            'ADMIN_FAQ_DISABLE_BUTTON',
            '🚫 Отключить показ',
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=toggle_text,
                callback_data='admin_faq_toggle',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_FAQ_DISPLAY_MODE_BUTTON',
                    '👁 Отображение: {mode}',
                ).format(mode=display_mode_label(settings.FAQ_DISPLAY_MODE)),
                callback_data='admin_faq_display_mode',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_HTML_HELP', 'ℹ️ HTML помощь'),
                callback_data='admin_faq_help',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.BACK,
                callback_data='admin_submenu_settings',
            )
        ]
    )

    return overview_text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_required
@error_handler
async def show_faq_management(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    overview_text, markup = await _build_overview(db_user, db)

    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_faq(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    setting = await FaqService.toggle_enabled(db, db_user.language)

    if setting.is_enabled:
        alert_text = texts.t(
            'ADMIN_FAQ_ENABLED_ALERT',
            '✅ FAQ включён.',
        )
    else:
        alert_text = texts.t(
            'ADMIN_FAQ_DISABLED_ALERT',
            '🚫 FAQ отключён.',
        )

    overview_text, markup = await _build_overview(db_user, db)

    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer(alert_text, show_alert=True)


@admin_required
@error_handler
async def cycle_faq_display_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    new_mode = await cycle_display_mode_setting(callback, db, 'FAQ_DISPLAY_MODE')
    if new_mode is None:
        return

    overview_text, markup = await _build_overview(db_user, db)
    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer(
        texts.t(
            'ADMIN_DISPLAY_MODE_CHANGED',
            'Отображение: {mode}',
        ).format(mode=display_mode_label(new_mode))
    )


@admin_required
@error_handler
async def start_create_faq_page(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    await state.set_state(AdminStates.creating_faq_title)
    await state.update_data(faq_language=db_user.language)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_FAQ_ENTER_TITLE',
            'Введите заголовок для новой страницы FAQ:',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_FAQ_CANCEL_BUTTON',
                            '⬅️ Отмена',
                        ),
                        callback_data='admin_faq_cancel',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def cancel_faq_creation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    await state.clear()
    await show_faq_management(callback, db_user, db)


@admin_required
@error_handler
async def process_new_faq_title(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    title = (message.text or '').strip()

    if not title:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_TITLE_EMPTY',
                '❌ Заголовок не может быть пустым.',
            )
        )
        return

    if len(title) > 255:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_TITLE_TOO_LONG',
                '❌ Заголовок слишком длинный. Максимум 255 символов.',
            )
        )
        return

    await state.update_data(faq_title=title)
    await state.set_state(AdminStates.creating_faq_content)

    await message.answer(
        texts.t(
            'ADMIN_FAQ_ENTER_CONTENT',
            'Отправьте содержимое страницы FAQ. Допускается HTML.',
        )
    )


@admin_required
@error_handler
async def process_new_faq_content(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    content = message.text or ''

    if len(content) > 6000:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_CONTENT_TOO_LONG',
                '❌ Текст слишком длинный. Максимум 6000 символов.',
            )
        )
        return

    if not content.strip():
        await message.answer(
            texts.t(
                'ADMIN_FAQ_CONTENT_EMPTY',
                '❌ Текст не может быть пустым.',
            )
        )
        return

    is_valid, error_message = validate_html_tags(content)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_HTML_ERROR',
                '❌ Ошибка в HTML: {error}',
            ).format(error=error_message)
        )
        return

    data = await state.get_data()
    title = data.get('faq_title') or texts.t('FAQ_PAGE_UNTITLED', 'Без названия')
    language = data.get('faq_language', db_user.language)

    await FaqService.create_page(
        db,
        language=language,
        title=title,
        content=content,
    )

    logger.info('Админ создал страницу FAQ (символов)', telegram_id=db_user.telegram_id, content_count=len(content))

    await state.clear()

    success_text = texts.t(
        'ADMIN_FAQ_PAGE_CREATED',
        '✅ Страница FAQ создана.',
    )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_FAQ_BACK_TO_LIST',
                        '⬅️ К настройкам FAQ',
                    ),
                    callback_data='admin_faq',
                )
            ]
        ]
    )

    await message.answer(success_text, reply_markup=reply_markup)


@admin_required
@error_handler
async def show_faq_page_details(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    raw_id = (callback.data or '').split(':', 1)[-1]
    try:
        page_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await callback.answer(
            texts.t(
                'ADMIN_FAQ_PAGE_NOT_FOUND',
                '⚠️ Страница не найдена.',
            ),
            show_alert=True,
        )
        return

    header = texts.t('ADMIN_FAQ_PAGE_HEADER', '📄 <b>Страница FAQ</b>')
    title = (page.title or '').strip() or texts.t('FAQ_PAGE_UNTITLED', 'Без названия')
    status_label = texts.t(
        'ADMIN_FAQ_PAGE_STATUS_ACTIVE',
        '✅ Активна',
    )
    if not page.is_active:
        status_label = texts.t(
            'ADMIN_FAQ_PAGE_STATUS_INACTIVE',
            '🚫 Выключена',
        )

    updated_at = _format_timestamp(getattr(page, 'updated_at', None))
    updated_block = ''
    if updated_at:
        updated_block = texts.t(
            'ADMIN_FAQ_PAGE_UPDATED',
            'Обновлено: {timestamp}',
        ).format(timestamp=updated_at)

    preview = (page.content or '').strip()
    preview_text = texts.t(
        'ADMIN_FAQ_PAGE_PREVIEW_EMPTY',
        'Текст ещё не задан.',
    )
    if preview:
        preview_trimmed = preview[:400]
        if len(preview) > 400:
            preview_trimmed += '...'
        preview_text = texts.t('ADMIN_FAQ_PAGE_PREVIEW', '<b>Превью:</b>\n{content}').format(
            content=html.escape(preview_trimmed)
        )

    message_parts = [
        header,
        texts.t(
            'ADMIN_FAQ_PAGE_TITLE',
            '<b>Заголовок:</b> {title}',
        ).format(title=html.escape(title)),
        texts.t(
            'ADMIN_FAQ_PAGE_STATUS',
            'Статус: {status}',
        ).format(status=status_label),
        preview_text,
        updated_block,
    ]

    message_text = '\n\n'.join(part for part in message_parts if part)

    buttons: list[list[types.InlineKeyboardButton]] = []

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_EDIT_TITLE_BUTTON', '✏️ Изменить заголовок'),
                callback_data=f'admin_faq_edit_title:{page.id}',
            )
        ]
    )
    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_EDIT_CONTENT_BUTTON', '📝 Изменить текст'),
                callback_data=f'admin_faq_edit_content:{page.id}',
            )
        ]
    )

    toggle_text = texts.t('ADMIN_FAQ_PAGE_ENABLE_BUTTON', '✅ Включить страницу')
    if page.is_active:
        toggle_text = texts.t(
            'ADMIN_FAQ_PAGE_DISABLE_BUTTON',
            '🚫 Выключить страницу',
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=toggle_text,
                callback_data=f'admin_faq_toggle_page:{page.id}',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_PAGE_MOVE_UP', '⬆️ Выше'),
                callback_data=f'admin_faq_move:{page.id}:up',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_PAGE_MOVE_DOWN', '⬇️ Ниже'),
                callback_data=f'admin_faq_move:{page.id}:down',
            ),
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_PAGE_DELETE_BUTTON', '🗑️ Удалить'),
                callback_data=f'admin_faq_delete:{page.id}',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_BACK_TO_LIST', '⬅️ К настройкам FAQ'),
                callback_data='admin_faq',
            )
        ]
    )

    await callback.message.edit_text(
        message_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@admin_required
@error_handler
async def start_edit_faq_title(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    raw_id = (callback.data or '').split(':', 1)[-1]
    try:
        page_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await callback.answer(
            texts.t(
                'ADMIN_FAQ_PAGE_NOT_FOUND',
                '⚠️ Страница не найдена.',
            ),
            show_alert=True,
        )
        return

    await state.set_state(AdminStates.editing_faq_title)
    await state.update_data(faq_page_id=page.id)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_FAQ_EDIT_TITLE_PROMPT',
            'Введите новый заголовок для страницы:',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_FAQ_CANCEL_BUTTON',
                            '⬅️ Отмена',
                        ),
                        callback_data=f'admin_faq_page:{page.id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_faq_title(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    title = (message.text or '').strip()

    if not title:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_TITLE_EMPTY',
                '❌ Заголовок не может быть пустым.',
            )
        )
        return

    if len(title) > 255:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_TITLE_TOO_LONG',
                '❌ Заголовок слишком длинный. Максимум 255 символов.',
            )
        )
        return

    data = await state.get_data()
    page_id = data.get('faq_page_id')

    if not page_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_FAQ_UNEXPECTED_STATE', '⚠️ Состояние сброшено.'))
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await message.answer(
            texts.t('ADMIN_FAQ_PAGE_NOT_FOUND', '⚠️ Страница не найдена.'),
        )
        await state.clear()
        return

    await FaqService.update_page(db, page, title=title)
    await state.clear()

    await message.answer(
        texts.t('ADMIN_FAQ_TITLE_UPDATED', '✅ Заголовок обновлён.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_FAQ_BACK_TO_LIST', '⬅️ К настройкам FAQ'),
                        callback_data='admin_faq',
                    )
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def start_edit_faq_content(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    raw_id = (callback.data or '').split(':', 1)[-1]
    try:
        page_id = int(raw_id)
    except ValueError:
        await callback.answer()
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await callback.answer(
            texts.t('ADMIN_FAQ_PAGE_NOT_FOUND', '⚠️ Страница не найдена.'),
            show_alert=True,
        )
        return

    await state.set_state(AdminStates.editing_faq_content)
    await state.update_data(faq_page_id=page.id)

    await callback.message.edit_text(
        texts.t(
            'ADMIN_FAQ_EDIT_CONTENT_PROMPT',
            'Отправьте новый текст для страницы FAQ.',
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t(
                            'ADMIN_FAQ_CANCEL_BUTTON',
                            '⬅️ Отмена',
                        ),
                        callback_data=f'admin_faq_page:{page.id}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


@admin_required
@error_handler
async def process_edit_faq_content(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    content = message.text or ''

    if len(content) > 6000:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_CONTENT_TOO_LONG',
                '❌ Текст слишком длинный. Максимум 6000 символов.',
            )
        )
        return

    if not content.strip():
        await message.answer(
            texts.t(
                'ADMIN_FAQ_CONTENT_EMPTY',
                '❌ Текст не может быть пустым.',
            )
        )
        return

    is_valid, error_message = validate_html_tags(content)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_FAQ_HTML_ERROR',
                '❌ Ошибка в HTML: {error}',
            ).format(error=error_message)
        )
        return

    data = await state.get_data()
    page_id = data.get('faq_page_id')

    if not page_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_FAQ_UNEXPECTED_STATE', '⚠️ Состояние сброшено.'))
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await message.answer(
            texts.t('ADMIN_FAQ_PAGE_NOT_FOUND', '⚠️ Страница не найдена.'),
        )
        await state.clear()
        return

    await FaqService.update_page(db, page, content=content)
    await state.clear()

    await message.answer(
        texts.t('ADMIN_FAQ_CONTENT_UPDATED', '✅ Текст страницы обновлён.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('ADMIN_FAQ_BACK_TO_LIST', '⬅️ К настройкам FAQ'),
                        callback_data='admin_faq',
                    )
                ]
            ]
        ),
    )


@admin_required
@error_handler
async def toggle_faq_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    parts = (callback.data or '').split(':')
    try:
        page_id = int(parts[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await callback.answer(
            texts.t('ADMIN_FAQ_PAGE_NOT_FOUND', '⚠️ Страница не найдена.'),
            show_alert=True,
        )
        return

    updated_page = await FaqService.update_page(db, page, is_active=not page.is_active)

    alert_text = texts.t(
        'ADMIN_FAQ_PAGE_ENABLED_ALERT',
        '✅ Страница включена.',
    )
    if not updated_page.is_active:
        alert_text = texts.t(
            'ADMIN_FAQ_PAGE_DISABLED_ALERT',
            '🚫 Страница выключена.',
        )

    await callback.answer(alert_text, show_alert=True)
    await show_faq_page_details(callback, db_user, db)


@admin_required
@error_handler
async def delete_faq_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    parts = (callback.data or '').split(':')
    try:
        page_id = int(parts[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    page = await FaqService.get_page(
        db,
        page_id,
        db_user.language,
        fallback=False,
        include_inactive=True,
    )

    if not page:
        await callback.answer(
            texts.t('ADMIN_FAQ_PAGE_NOT_FOUND', '⚠️ Страница не найдена.'),
            show_alert=True,
        )
        return

    await FaqService.delete_page(db, page.id)

    remaining_pages = await FaqService.get_pages(
        db,
        db_user.language,
        include_inactive=True,
        fallback=False,
    )

    if remaining_pages:
        remaining_sorted = sorted(
            remaining_pages,
            key=lambda item: (item.display_order, item.id),
        )
        await FaqService.reorder_pages(db, db_user.language, remaining_sorted)

    await callback.answer(
        texts.t('ADMIN_FAQ_PAGE_DELETED', '🗑️ Страница удалена.'),
        show_alert=True,
    )

    await show_faq_management(callback, db_user, db)


@admin_required
@error_handler
async def move_faq_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    parts = (callback.data or '').split(':')
    try:
        page_id = int(parts[1])
        direction = parts[2]
    except (ValueError, IndexError):
        await callback.answer()
        return

    pages = await FaqService.get_pages(
        db,
        db_user.language,
        include_inactive=True,
        fallback=False,
    )

    if not pages:
        await callback.answer()
        return

    pages_sorted = sorted(pages, key=lambda item: (item.display_order, item.id))

    index = next((i for i, page in enumerate(pages_sorted) if page.id == page_id), None)

    if index is None:
        await callback.answer()
        return

    if direction == 'up' and index > 0:
        pages_sorted[index - 1], pages_sorted[index] = (
            pages_sorted[index],
            pages_sorted[index - 1],
        )
    elif direction == 'down' and index < len(pages_sorted) - 1:
        pages_sorted[index + 1], pages_sorted[index] = (
            pages_sorted[index],
            pages_sorted[index + 1],
        )
    else:
        await callback.answer()
        return

    await FaqService.reorder_pages(db, db_user.language, pages_sorted)

    await callback.answer(
        texts.t('ADMIN_FAQ_PAGE_REORDERED', '✅ Порядок обновлён.'),
        show_alert=True,
    )
    await show_faq_page_details(callback, db_user, db)


@admin_required
@error_handler
async def show_faq_html_help(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    help_text = get_html_help_text()

    buttons = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_FAQ_BACK_TO_LIST', '⬅️ К настройкам FAQ'),
                callback_data='admin_faq',
            )
        ]
    ]

    await callback.message.edit_text(
        help_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        show_faq_management,
        F.data == 'admin_faq',
    )
    dp.callback_query.register(
        toggle_faq,
        F.data == 'admin_faq_toggle',
    )
    dp.callback_query.register(
        cycle_faq_display_mode,
        F.data == 'admin_faq_display_mode',
    )
    dp.callback_query.register(
        start_create_faq_page,
        F.data == 'admin_faq_create',
    )
    dp.callback_query.register(
        cancel_faq_creation,
        F.data == 'admin_faq_cancel',
    )
    dp.callback_query.register(
        show_faq_page_details,
        F.data.startswith('admin_faq_page:'),
    )
    dp.callback_query.register(
        start_edit_faq_title,
        F.data.startswith('admin_faq_edit_title:'),
    )
    dp.callback_query.register(
        start_edit_faq_content,
        F.data.startswith('admin_faq_edit_content:'),
    )
    dp.callback_query.register(
        toggle_faq_page,
        F.data.startswith('admin_faq_toggle_page:'),
    )
    dp.callback_query.register(
        delete_faq_page,
        F.data.startswith('admin_faq_delete:'),
    )
    dp.callback_query.register(
        move_faq_page,
        F.data.startswith('admin_faq_move:'),
    )
    dp.callback_query.register(
        show_faq_html_help,
        F.data == 'admin_faq_help',
    )

    dp.message.register(
        process_new_faq_title,
        AdminStates.creating_faq_title,
    )
    dp.message.register(
        process_new_faq_content,
        AdminStates.creating_faq_content,
    )
    dp.message.register(
        process_edit_faq_title,
        AdminStates.editing_faq_title,
    )
    dp.message.register(
        process_edit_faq_content,
        AdminStates.editing_faq_content,
    )
