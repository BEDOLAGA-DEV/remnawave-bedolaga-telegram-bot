import html
from datetime import datetime

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.services.public_offer_service import PublicOfferService
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler
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
    offer = await PublicOfferService.get_offer(
        db,
        db_user.language,
        fallback=False,
    )

    normalized_language = PublicOfferService.normalize_language(db_user.language)
    has_content = bool(offer and offer.content and offer.content.strip())

    description = texts.t('ADMIN_PUBLIC_OFFER_DESCRIPTION', 'Публичная оферта отображается в разделе «Инфо».')

    status_text = texts.t('ADMIN_PUBLIC_OFFER_STATUS_DISABLED', '⚠️ Показ оферты выключен или текст отсутствует.')
    if offer and offer.is_enabled and has_content:
        status_text = texts.t('ADMIN_PUBLIC_OFFER_STATUS_ENABLED', '✅ Оферта активна и показывается пользователям.')
    elif offer and offer.is_enabled:
        status_text = texts.t('ADMIN_PUBLIC_OFFER_STATUS_ENABLED_EMPTY', '⚠️ Оферта включена, но текст пуст — пользователи её не увидят.')

    updated_at = _format_timestamp(getattr(offer, 'updated_at', None))
    updated_block = ''
    if updated_at:
        updated_block = texts.t('ADMIN_PUBLIC_OFFER_UPDATED_AT', 'Последнее обновление: {timestamp}').format(timestamp=updated_at)

    preview_block = texts.t('ADMIN_PUBLIC_OFFER_PREVIEW_EMPTY', 'Текст ещё не задан.')
    if has_content:
        preview_title = texts.t('ADMIN_PUBLIC_OFFER_PREVIEW_TITLE', '<b>Превью текста:</b>')
        preview_raw = offer.content.strip()
        preview_trimmed = preview_raw[:400]
        if len(preview_raw) > 400:
            preview_trimmed += '...'
        preview_block = f'{preview_title}\n<code>{html.escape(preview_trimmed)}</code>'

    language_block = texts.t('ADMIN_PUBLIC_OFFER_LANGUAGE', 'Язык: <code>{lang}</code>').format(lang=normalized_language)

    header = texts.t('ADMIN_PUBLIC_OFFER_HEADER', '📄 <b>Публичная оферта</b>')
    actions_prompt = texts.t('ADMIN_PUBLIC_OFFER_ACTION_PROMPT', 'Выберите действие:')

    message_parts = [
        header,
        description,
        language_block,
        status_text,
    ]

    if updated_block:
        message_parts.append(updated_block)

    message_parts.append(preview_block)
    message_parts.append(actions_prompt)

    overview_text = '\n\n'.join(part for part in message_parts if part)

    buttons: list[list[types.InlineKeyboardButton]] = []

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PUBLIC_OFFER_EDIT_BUTTON', '✏️ Изменить текст'),
                callback_data='admin_public_offer_edit',
            )
        ]
    )

    if has_content:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_VIEW_BUTTON', '👀 Просмотреть текущий текст'),
                    callback_data='admin_public_offer_view',
                )
            ]
        )

    toggle_text = texts.t('ADMIN_PUBLIC_OFFER_ENABLE_BUTTON', '✅ Включить показ')
    if offer and offer.is_enabled:
        toggle_text = texts.t('ADMIN_PUBLIC_OFFER_DISABLE_BUTTON', '🚫 Отключить показ')

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=toggle_text,
                callback_data='admin_public_offer_toggle',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PUBLIC_OFFER_HTML_HELP', 'ℹ️ HTML помощь'),
                callback_data='admin_public_offer_help',
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

    return overview_text, types.InlineKeyboardMarkup(inline_keyboard=buttons), offer


@admin_required
@error_handler
async def show_public_offer_management(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    overview_text, markup, _ = await _build_overview(db_user, db)

    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_public_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    updated_offer = await PublicOfferService.toggle_enabled(db, db_user.language)
    logger.info(
        'Админ %s переключил показ публичной оферты: %s',
        db_user.telegram_id,
        'enabled' if updated_offer.is_enabled else 'disabled',
    )
    status_message = (
        texts.t('ADMIN_PUBLIC_OFFER_ENABLED', '✅ Оферта включена')
        if updated_offer.is_enabled
        else texts.t('ADMIN_PUBLIC_OFFER_DISABLED', '🚫 Оферта отключена')
    )

    overview_text, markup, _ = await _build_overview(db_user, db)
    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer(status_message)


@admin_required
@error_handler
async def start_edit_public_offer(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    offer = await PublicOfferService.get_offer(
        db,
        db_user.language,
        fallback=False,
    )

    current_preview = ''
    if offer and offer.content:
        preview = offer.content.strip()[:400]
        if len(offer.content.strip()) > 400:
            preview += '...'
        current_preview = (
            texts.t('ADMIN_PUBLIC_OFFER_CURRENT_PREVIEW', 'Текущий текст (превью):')
            + f'\n<code>{html.escape(preview)}</code>\n\n'
        )

    prompt = texts.t('ADMIN_PUBLIC_OFFER_EDIT_PROMPT', 'Отправьте новый текст публичной оферты. Допускается HTML-разметка.')

    hint = texts.t('ADMIN_PUBLIC_OFFER_EDIT_HINT', 'Используйте /html_help для справки по тегам.')

    message_text = (
        f'📝 <b>{texts.t("ADMIN_PUBLIC_OFFER_EDIT_TITLE", 'Редактирование оферты')}</b>\n\n'
        f'{current_preview}{prompt}\n\n{hint}'
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_HTML_HELP', 'ℹ️ HTML помощь'),
                    callback_data='admin_public_offer_help',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_CANCEL', '❌ Отмена'),
                    callback_data='admin_public_offer_cancel',
                )
            ],
        ]
    )

    await callback.message.edit_text(message_text, reply_markup=keyboard)
    await state.set_state(AdminStates.editing_public_offer)
    await callback.answer()


@admin_required
@error_handler
async def cancel_edit_public_offer(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    await state.clear()
    overview_text, markup, _ = await _build_overview(db_user, db)
    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer(
        get_texts(db_user.language).t('ADMIN_PUBLIC_OFFER_EDIT_CANCELLED', 'Редактирование оферты отменено.')
    )


@admin_required
@error_handler
async def process_public_offer_edit(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    new_text = message.text or ''

    if len(new_text) > 4000:
        await message.answer(texts.t('ADMIN_PUBLIC_OFFER_TOO_LONG', '❌ Текст оферты слишком длинный. Максимум 4000 символов.'))
        return

    is_valid, error_message = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(texts.t('ADMIN_PUBLIC_OFFER_HTML_ERROR', '❌ Ошибка в HTML: {error}').format(error=error_message))
        return

    await PublicOfferService.save_offer(db, db_user.language, new_text)
    logger.info(
        'Админ обновил текст публичной оферты (символов)', telegram_id=db_user.telegram_id, new_text_count=len(new_text)
    )
    await state.clear()

    success_text = texts.t('ADMIN_PUBLIC_OFFER_SAVED', '✅ Публичная оферта обновлена.')

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_BACK_BUTTON', '⬅️ К настройкам оферты'),
                    callback_data='admin_public_offer',
                )
            ]
        ]
    )

    await message.answer(success_text, reply_markup=reply_markup)


@admin_required
@error_handler
async def view_public_offer(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    offer = await PublicOfferService.get_offer(
        db,
        db_user.language,
        fallback=False,
    )

    if not offer or not offer.content or not offer.content.strip():
        await callback.answer(
            texts.t('ADMIN_PUBLIC_OFFER_PREVIEW_EMPTY_ALERT', 'Текст оферты пока не задан.'),
            show_alert=True,
        )
        return

    content = offer.content.strip()
    max_length = 3800
    pages = PublicOfferService.split_content_into_pages(
        content,
        max_length=max_length,
    )

    if not pages:
        await callback.answer(
            texts.t('ADMIN_PUBLIC_OFFER_PREVIEW_EMPTY_ALERT', 'Текст оферты пока не задан.'),
            show_alert=True,
        )
        return

    preview = pages[0]
    truncated = len(pages) > 1

    header = texts.t('ADMIN_PUBLIC_OFFER_VIEW_TITLE', '👀 <b>Текущий текст оферты</b>')

    note = ''
    if truncated:
        note = texts.t('ADMIN_PUBLIC_OFFER_VIEW_TRUNCATED', '\n\n⚠️ Текст сокращён для отображения. Полную версию увидят пользователи в меню.')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_BACK_BUTTON', '⬅️ К настройкам оферты'),
                    callback_data='admin_public_offer',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_EDIT_BUTTON', '✏️ Изменить текст'),
                    callback_data='admin_public_offer_edit',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        f'{header}\n\n{preview}{note}',
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def show_public_offer_html_help(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    help_text = get_html_help_text()

    current_state = await state.get_state()

    buttons: list[list[types.InlineKeyboardButton]] = []

    if current_state == AdminStates.editing_public_offer.state:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PUBLIC_OFFER_RETURN_TO_EDIT', '⬅️ Назад к редактированию'),
                    callback_data='admin_public_offer_edit',
                )
            ]
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_PUBLIC_OFFER_BACK_BUTTON', '⬅️ К настройкам оферты'),
                callback_data='admin_public_offer',
            )
        ]
    )

    await callback.message.edit_text(
        help_text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        show_public_offer_management,
        F.data == 'admin_public_offer',
    )
    dp.callback_query.register(
        toggle_public_offer,
        F.data == 'admin_public_offer_toggle',
    )
    dp.callback_query.register(
        start_edit_public_offer,
        F.data == 'admin_public_offer_edit',
    )
    dp.callback_query.register(
        cancel_edit_public_offer,
        F.data == 'admin_public_offer_cancel',
    )
    dp.callback_query.register(
        view_public_offer,
        F.data == 'admin_public_offer_view',
    )
    dp.callback_query.register(
        show_public_offer_html_help,
        F.data == 'admin_public_offer_help',
    )

    dp.message.register(
        process_public_offer_edit,
        AdminStates.editing_public_offer,
    )
