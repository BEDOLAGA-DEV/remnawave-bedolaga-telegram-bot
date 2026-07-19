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
from app.services.privacy_policy_service import PrivacyPolicyService
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
    policy = await PrivacyPolicyService.get_policy(
        db,
        db_user.language,
        fallback=False,
    )

    normalized_language = PrivacyPolicyService.normalize_language(db_user.language)
    has_content = bool(policy and policy.content and policy.content.strip())

    description = texts.t(
        'ADMIN_PRIVACY_POLICY_DESCRIPTION',
        'Политика конфиденциальности отображается в разделе «Инфо».',
    )

    status_text = texts.t(
        'ADMIN_PRIVACY_POLICY_STATUS_DISABLED',
        '⚠️ Показ политики выключен или текст отсутствует.',
    )
    if policy and policy.is_enabled and has_content:
        status_text = texts.t(
            'ADMIN_PRIVACY_POLICY_STATUS_ENABLED',
            '✅ Политика активна и показывается пользователям.',
        )
    elif policy and policy.is_enabled:
        status_text = texts.t(
            'ADMIN_PRIVACY_POLICY_STATUS_ENABLED_EMPTY',
            '⚠️ Политика включена, но текст пуст — пользователи её не увидят.',
        )

    updated_at = _format_timestamp(getattr(policy, 'updated_at', None))
    updated_block = ''
    if updated_at:
        updated_block = texts.t(
            'ADMIN_PRIVACY_POLICY_UPDATED_AT',
            'Последнее обновление: {timestamp}',
        ).format(timestamp=updated_at)

    preview_block = texts.t(
        'ADMIN_PRIVACY_POLICY_PREVIEW_EMPTY',
        'Текст ещё не задан.',
    )
    if has_content:
        preview_title = texts.t(
            'ADMIN_PRIVACY_POLICY_PREVIEW_TITLE',
            '<b>Превью текста:</b>',
        )
        preview_raw = policy.content.strip()
        preview_trimmed = preview_raw[:400]
        if len(preview_raw) > 400:
            preview_trimmed += '...'
        preview_block = f'{preview_title}\n<code>{html.escape(preview_trimmed)}</code>'

    language_block = texts.t(
        'ADMIN_PRIVACY_POLICY_LANGUAGE',
        'Язык: <code>{lang}</code>',
    ).format(lang=normalized_language)

    header = texts.t(
        'ADMIN_PRIVACY_POLICY_HEADER',
        '🛡️ <b>Политика конфиденциальности</b>',
    )
    actions_prompt = texts.t(
        'ADMIN_PRIVACY_POLICY_ACTION_PROMPT',
        'Выберите действие:',
    )

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
                text=texts.t(
                    'ADMIN_PRIVACY_POLICY_EDIT_BUTTON',
                    '✏️ Изменить текст',
                ),
                callback_data='admin_privacy_policy_edit',
            )
        ]
    )

    if has_content:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_VIEW_BUTTON',
                        '👀 Просмотреть текущий текст',
                    ),
                    callback_data='admin_privacy_policy_view',
                )
            ]
        )

    toggle_text = texts.t(
        'ADMIN_PRIVACY_POLICY_ENABLE_BUTTON',
        '✅ Включить показ',
    )
    if policy and policy.is_enabled:
        toggle_text = texts.t(
            'ADMIN_PRIVACY_POLICY_DISABLE_BUTTON',
            '🚫 Отключить показ',
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=toggle_text,
                callback_data='admin_privacy_policy_toggle',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRIVACY_POLICY_DISPLAY_MODE_BUTTON',
                    '👁 Отображение: {mode}',
                ).format(mode=display_mode_label(settings.PRIVACY_POLICY_DISPLAY_MODE)),
                callback_data='admin_privacy_policy_display_mode',
            )
        ]
    )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRIVACY_POLICY_HTML_HELP',
                    'ℹ️ HTML помощь',
                ),
                callback_data='admin_privacy_policy_help',
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

    return overview_text, types.InlineKeyboardMarkup(inline_keyboard=buttons), policy


@admin_required
@error_handler
async def show_privacy_policy_management(
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
async def toggle_privacy_policy(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    updated_policy = await PrivacyPolicyService.toggle_enabled(db, db_user.language)
    logger.info(
        'Админ %s переключил показ политики конфиденциальности: %s',
        db_user.telegram_id,
        'enabled' if updated_policy.is_enabled else 'disabled',
    )
    status_message = (
        texts.t('ADMIN_PRIVACY_POLICY_ENABLED', '✅ Политика включена')
        if updated_policy.is_enabled
        else texts.t('ADMIN_PRIVACY_POLICY_DISABLED', '🚫 Политика отключена')
    )

    overview_text, markup, _ = await _build_overview(db_user, db)
    await callback.message.edit_text(
        overview_text,
        reply_markup=markup,
    )
    await callback.answer(status_message)


@admin_required
@error_handler
async def cycle_privacy_policy_display_mode(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    new_mode = await cycle_display_mode_setting(callback, db, 'PRIVACY_POLICY_DISPLAY_MODE')
    if new_mode is None:
        return

    overview_text, markup, _ = await _build_overview(db_user, db)
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
async def start_edit_privacy_policy(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)

    policy = await PrivacyPolicyService.get_policy(
        db,
        db_user.language,
        fallback=False,
    )

    current_preview = ''
    if policy and policy.content:
        preview = policy.content.strip()[:400]
        if len(policy.content.strip()) > 400:
            preview += '...'
        current_preview = (
            texts.t(
                'ADMIN_PRIVACY_POLICY_CURRENT_PREVIEW',
                'Текущий текст (превью):',
            )
            + f'\n<code>{html.escape(preview)}</code>\n\n'
        )

    prompt = texts.t(
        'ADMIN_PRIVACY_POLICY_EDIT_PROMPT',
        'Отправьте новый текст политики конфиденциальности. Допускается HTML-разметка.',
    )

    hint = texts.t(
        'ADMIN_PRIVACY_POLICY_EDIT_HINT',
        'Используйте /html_help для справки по тегам.',
    )

    message_text = (
        f'📝 <b>{texts.t("ADMIN_PRIVACY_POLICY_EDIT_TITLE", "Редактирование политики")}</b>\n\n'
        f'{current_preview}{prompt}\n\n{hint}'
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_HTML_HELP',
                        'ℹ️ HTML помощь',
                    ),
                    callback_data='admin_privacy_policy_help',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('ADMIN_PRIVACY_POLICY_CANCEL', '❌ Отмена'),
                    callback_data='admin_privacy_policy_cancel',
                )
            ],
        ]
    )

    await callback.message.edit_text(message_text, reply_markup=keyboard)
    await state.set_state(AdminStates.editing_privacy_policy)
    await callback.answer()


@admin_required
@error_handler
async def cancel_edit_privacy_policy(
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
    await callback.answer()


@admin_required
@error_handler
async def process_privacy_policy_edit(
    message: types.Message,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    new_text = message.text or ''

    if len(new_text) > 4000:
        await message.answer(
            texts.t(
                'ADMIN_PRIVACY_POLICY_TOO_LONG',
                '❌ Текст политики слишком длинный. Максимум 4000 символов.',
            )
        )
        return

    is_valid, error_message = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_PRIVACY_POLICY_HTML_ERROR',
                '❌ Ошибка в HTML: {error}',
            ).format(error=error_message)
        )
        return

    await PrivacyPolicyService.save_policy(db, db_user.language, new_text)
    logger.info(
        'Админ обновил текст политики конфиденциальности (символов)',
        telegram_id=db_user.telegram_id,
        new_text_count=len(new_text),
    )
    await state.clear()

    success_text = texts.t(
        'ADMIN_PRIVACY_POLICY_SAVED',
        '✅ Политика конфиденциальности обновлена.',
    )

    reply_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_BACK_BUTTON',
                        '⬅️ К настройкам политики',
                    ),
                    callback_data='admin_privacy_policy',
                )
            ]
        ]
    )

    await message.answer(success_text, reply_markup=reply_markup)


@admin_required
@error_handler
async def view_privacy_policy(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    policy = await PrivacyPolicyService.get_policy(
        db,
        db_user.language,
        fallback=False,
    )

    if not policy or not policy.content or not policy.content.strip():
        await callback.answer(
            texts.t(
                'ADMIN_PRIVACY_POLICY_PREVIEW_EMPTY_ALERT',
                'Текст политики пока не задан.',
            ),
            show_alert=True,
        )
        return

    content = policy.content.strip()
    truncated = False
    max_length = 3800
    if len(content) > max_length:
        content = content[: max_length - 3] + '...'
        truncated = True

    header = texts.t(
        'ADMIN_PRIVACY_POLICY_VIEW_TITLE',
        '👀 <b>Текущий текст политики</b>',
    )

    note = ''
    if truncated:
        note = texts.t(
            'ADMIN_PRIVACY_POLICY_VIEW_TRUNCATED',
            '\n\n⚠️ Текст сокращён для отображения. Полную версию увидят пользователи в меню.',
        )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_BACK_BUTTON',
                        '⬅️ К настройкам политики',
                    ),
                    callback_data='admin_privacy_policy',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_EDIT_BUTTON',
                        '✏️ Изменить текст',
                    ),
                    callback_data='admin_privacy_policy_edit',
                )
            ],
        ]
    )

    await callback.message.edit_text(
        f'{header}\n\n{content}{note}',
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_required
@error_handler
async def show_privacy_policy_html_help(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
):
    texts = get_texts(db_user.language)
    help_text = get_html_help_text()

    current_state = await state.get_state()

    buttons: list[list[types.InlineKeyboardButton]] = []

    if current_state == AdminStates.editing_privacy_policy.state:
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_PRIVACY_POLICY_RETURN_TO_EDIT',
                        '⬅️ Назад к редактированию',
                    ),
                    callback_data='admin_privacy_policy_edit',
                )
            ]
        )

    buttons.append(
        [
            types.InlineKeyboardButton(
                text=texts.t(
                    'ADMIN_PRIVACY_POLICY_BACK_BUTTON',
                    '⬅️ К настройкам политики',
                ),
                callback_data='admin_privacy_policy',
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
        show_privacy_policy_management,
        F.data == 'admin_privacy_policy',
    )
    dp.callback_query.register(
        toggle_privacy_policy,
        F.data == 'admin_privacy_policy_toggle',
    )
    dp.callback_query.register(
        cycle_privacy_policy_display_mode,
        F.data == 'admin_privacy_policy_display_mode',
    )
    dp.callback_query.register(
        start_edit_privacy_policy,
        F.data == 'admin_privacy_policy_edit',
    )
    dp.callback_query.register(
        cancel_edit_privacy_policy,
        F.data == 'admin_privacy_policy_cancel',
    )
    dp.callback_query.register(
        view_privacy_policy,
        F.data == 'admin_privacy_policy_view',
    )
    dp.callback_query.register(
        show_privacy_policy_html_help,
        F.data == 'admin_privacy_policy_help',
    )

    dp.message.register(
        process_privacy_policy_edit,
        AdminStates.editing_privacy_policy,
    )
