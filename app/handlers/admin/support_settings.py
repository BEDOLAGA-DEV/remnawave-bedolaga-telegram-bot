import contextlib
import html
import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.states import SupportSettingsStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)


def _get_support_settings_keyboard(language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    mode = SupportSettingsService.get_system_mode()
    menu_enabled = SupportSettingsService.is_support_menu_enabled()
    admin_notif = SupportSettingsService.get_admin_ticket_notifications_enabled()
    user_notif = SupportSettingsService.get_user_ticket_notifications_enabled()
    sla_enabled = SupportSettingsService.get_sla_enabled()
    sla_minutes = SupportSettingsService.get_sla_minutes()

    rows: list[list[types.InlineKeyboardButton]] = []

    status_enabled = texts.t('ADMIN_SUPPORT_SETTINGS_STATUS_ENABLED', 'Включены')
    status_disabled = texts.t('ADMIN_SUPPORT_SETTINGS_STATUS_DISABLED', 'Отключены')

    def mode_button(label: str, active: bool) -> str:
        prefix = '🔘' if active else '⚪'
        return f'{prefix} {label}'

    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"✅" if menu_enabled else "🚫"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_MENU_LABEL", "Пункт «Техподдержка» в меню")}'
                ),
                callback_data='admin_support_toggle_menu',
            )
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=mode_button(texts.t('ADMIN_SUPPORT_SETTINGS_MODE_TICKETS', 'Тикеты'), mode == 'tickets'),
                callback_data='admin_support_mode_tickets',
            ),
            types.InlineKeyboardButton(
                text=mode_button(texts.t('ADMIN_SUPPORT_SETTINGS_MODE_CONTACT', 'Контакт'), mode == 'contact'),
                callback_data='admin_support_mode_contact',
            ),
            types.InlineKeyboardButton(
                text=mode_button(texts.t('ADMIN_SUPPORT_SETTINGS_MODE_BOTH', 'Оба'), mode == 'both'),
                callback_data='admin_support_mode_both',
            ),
        ]
    )

    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_EDIT_DESCRIPTION', '📝 Изменить описание'),
                callback_data='admin_support_edit_desc',
            )
        ]
    )

    # Notifications block
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"🔔" if admin_notif else "🔕"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_ADMIN_NOTIFICATIONS", "Админ-уведомления")}: '
                    f'{status_enabled if admin_notif else status_disabled}'
                ),
                callback_data='admin_support_toggle_admin_notifications',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"🔔" if user_notif else "🔕"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_USER_NOTIFICATIONS", "Пользовательские уведомления")}: '
                    f'{status_enabled if user_notif else status_disabled}'
                ),
                callback_data='admin_support_toggle_user_notifications',
            )
        ]
    )

    # SLA block
    rows.append(
        [
            types.InlineKeyboardButton(
                text=(
                    f'{"⏰" if sla_enabled else "⏹️"} '
                    f'{texts.t("ADMIN_SUPPORT_SETTINGS_SLA_LABEL", "SLA")}: '
                    f'{status_enabled if sla_enabled else status_disabled}'
                ),
                callback_data='admin_support_toggle_sla',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_SLA_TIME', '⏳ Время SLA: {minutes} мин').format(
                    minutes=sla_minutes
                ),
                callback_data='admin_support_set_sla_minutes',
            )
        ]
    )

    # Moderators
    moderators = SupportSettingsService.get_moderators()
    mod_count = len(moderators)
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_MODERATORS_COUNT', '🧑‍⚖️ Модераторы: {count}').format(
                    count=mod_count
                ),
                callback_data='admin_support_list_moderators',
            )
        ]
    )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_ADD_MODERATOR', '➕ Назначить модератора'),
                callback_data='admin_support_add_moderator',
            ),
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SETTINGS_REMOVE_MODERATOR', '➖ Удалить модератора'),
                callback_data='admin_support_remove_moderator',
            ),
        ]
    )

    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_support')])

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@admin_required
@error_handler
async def show_support_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    desc = SupportSettingsService.get_support_info_text(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_SUPPORT_SETTINGS_TITLE', '🛟 <b>Настройки поддержки</b>')
        + '\n\n'
        + texts.t(
            'ADMIN_SUPPORT_SETTINGS_DESCRIPTION',
            'Режим работы и видимость в меню. Ниже текущее описание меню поддержки:',
        )
        + '\n\n'
        + desc,
        reply_markup=_get_support_settings_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_support_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.is_support_menu_enabled()
    SupportSettingsService.set_support_menu_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_admin_notifications(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_admin_ticket_notifications_enabled()
    SupportSettingsService.set_admin_ticket_notifications_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_user_notifications(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_user_ticket_notifications_enabled()
    SupportSettingsService.set_user_ticket_notifications_enabled(not current)
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def toggle_sla(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    current = SupportSettingsService.get_sla_enabled()
    SupportSettingsService.set_sla_enabled(not current)
    await show_support_settings(callback, db_user, db)


class SupportAdvancedStates(StatesGroup):
    waiting_for_sla_minutes = State()
    waiting_for_moderator_id = State()


@admin_required
@error_handler
async def start_set_sla_minutes(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_SLA_SETUP_PROMPT',
            '⏳ <b>Настройка SLA</b>\n\nВведите количество минут ожидания ответа (целое число > 0):',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_sla_minutes)
    await callback.answer()


@admin_required
@error_handler
async def handle_sla_minutes(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    text = (message.text or '').strip()
    try:
        minutes = int(text)
        if minutes <= 0 or minutes > 1440:
            raise ValueError
    except Exception:
        await message.answer(texts.t('ADMIN_SUPPORT_SLA_INVALID', '❌ Введите корректное число минут (1-1440)'))
        return
    SupportSettingsService.set_sla_minutes(minutes)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(texts.t('ADMIN_SUPPORT_SLA_SAVED', '✅ Значение SLA сохранено'), reply_markup=markup)


@admin_required
@error_handler
async def start_add_moderator(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_ASSIGN_MODERATOR_PROMPT',
            '🧑‍⚖️ <b>Назначение модератора</b>\n\nОтправьте Telegram ID пользователя (число)',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_moderator_id)
    await callback.answer()


@admin_required
@error_handler
async def start_remove_moderator(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_SUPPORT_REMOVE_MODERATOR_PROMPT',
            '🧑‍⚖️ <b>Удаление модератора</b>\n\nОтправьте Telegram ID пользователя (число)',
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
        ),
    )
    await state.set_state(SupportAdvancedStates.waiting_for_moderator_id)
    # We'll reuse the same state; next message will decide action via flag
    await state.update_data(action='remove_moderator')
    await callback.answer()


@admin_required
@error_handler
async def handle_moderator_id(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    data = await state.get_data()
    action = data.get('action', 'add')
    text = (message.text or '').strip()
    try:
        tid = int(text)
    except Exception:
        await message.answer(texts.t('ADMIN_SUPPORT_INVALID_TELEGRAM_ID', '❌ Введите корректный Telegram ID (число)'))
        return
    if action == 'remove_moderator':
        ok = SupportSettingsService.remove_moderator(tid)
        msg = (
            texts.t('ADMIN_SUPPORT_MODERATOR_REMOVED_SUCCESS', '✅ Модератор {tid} удалён').format(tid=tid)
            if ok
            else texts.t('ADMIN_SUPPORT_MODERATOR_REMOVED_FAIL', '❌ Не удалось удалить модератора')
        )
    else:
        ok = SupportSettingsService.add_moderator(tid)
        msg = (
            texts.t('ADMIN_SUPPORT_MODERATOR_ADDED_SUCCESS', '✅ Пользователь {tid} назначен модератором').format(
                tid=tid
            )
            if ok
            else texts.t('ADMIN_SUPPORT_MODERATOR_ADDED_FAIL', '❌ Не удалось назначить модератора')
        )
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(msg, reply_markup=markup)


@admin_required
@error_handler
async def list_moderators(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    moderators = SupportSettingsService.get_moderators()
    if not moderators:
        await callback.answer(texts.t('ADMIN_SUPPORT_MODERATORS_EMPTY', 'Список пуст'), show_alert=True)
        return
    text = (
        texts.t('ADMIN_SUPPORT_MODERATORS_TITLE', '🧑‍⚖️ <b>Модераторы</b>')
        + '\n\n'
        + '\n'.join([f'• <code>{tid}</code>' for tid in moderators])
    )
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')]]
    )
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=markup)
    await callback.answer()


@admin_required
@error_handler
async def set_mode_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('tickets')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_mode_contact(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('contact')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def set_mode_both(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    SupportSettingsService.set_system_mode('both')
    await show_support_settings(callback, db_user, db)


@admin_required
@error_handler
async def start_edit_desc(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    current_desc_html = SupportSettingsService.get_support_info_text(db_user.language)
    # plain text for display-only code block
    current_desc_plain = re.sub(r'<[^>]+>', '', current_desc_html)

    kb_rows: list[list[types.InlineKeyboardButton]] = []
    kb_rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('ADMIN_SUPPORT_SEND_DESCRIPTION', '📨 Прислать текст'),
                callback_data='admin_support_send_desc',
            )
        ]
    )
    # Подготовим блок контакта (отдельным инлайном)
    from app.config import settings

    support_contact_display = settings.get_support_contact_display()
    kb_rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='admin_support_settings')])

    text_parts = [
        texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_TITLE', '📝 <b>Редактирование описания поддержки</b>'),
        '',
        texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CURRENT', 'Текущее описание:'),
        '',
        f'<code>{html.escape(current_desc_plain)}</code>',
    ]
    if support_contact_display:
        text_parts += [
            '',
            texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CONTACT_TITLE', '<b>Контакт для режима «Контакт»</b>'),
            f'<code>{html.escape(support_contact_display)}</code>',
            '',
            texts.t('ADMIN_SUPPORT_EDIT_DESCRIPTION_CONTACT_HINT', 'Добавьте в описание при необходимости.'),
        ]
    await callback.message.edit_text(
        '\n'.join(text_parts), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode='HTML'
    )
    await state.set_state(SupportSettingsStates.waiting_for_desc)
    await callback.answer()


@admin_required
@error_handler
async def handle_new_desc(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    new_text = message.html_text or message.text
    SupportSettingsService.set_support_info_text(db_user.language, new_text)
    await state.clear()
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    await message.answer(texts.t('ADMIN_SUPPORT_DESCRIPTION_UPDATED', '✅ Описание обновлено.'), reply_markup=markup)


@admin_required
@error_handler
async def send_desc_copy(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # send plain text for easy copying
    texts = get_texts(db_user.language)
    current_desc_html = SupportSettingsService.get_support_info_text(db_user.language)
    current_desc_plain = re.sub(r'<[^>]+>', '', current_desc_html)
    # attach delete button to the sent message
    markup = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DELETE_MESSAGE', '🗑 Удалить'), callback_data='admin_support_delete_msg'
                )
            ]
        ]
    )
    if len(current_desc_plain) <= 4000:
        await callback.message.answer(current_desc_plain, reply_markup=markup)
    else:
        # split long messages (attach delete only to the last chunk)
        chunk = 0
        while chunk < len(current_desc_plain):
            next_chunk = current_desc_plain[chunk : chunk + 4000]
            is_last = (chunk + 4000) >= len(current_desc_plain)
            await callback.message.answer(next_chunk, reply_markup=(markup if is_last else None))
            chunk += 4000
    await callback.answer(texts.t('ADMIN_SUPPORT_DESCRIPTION_SENT', 'Текст отправлен ниже'))


@error_handler
async def delete_sent_message(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Allow admins and moderators to delete informational notifications
    try:
        may_delete = settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(
            callback.from_user.id
        )
    except Exception:
        may_delete = False
    texts = get_texts(db_user.language if db_user else 'ru')
    if not may_delete:
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    try:
        await callback.message.delete()
    finally:
        with contextlib.suppress(Exception):
            await callback.answer(texts.t('ADMIN_SUPPORT_MESSAGE_DELETED', 'Сообщение удалено'))


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_settings, F.data == 'admin_support_settings')
    dp.callback_query.register(toggle_support_menu, F.data == 'admin_support_toggle_menu')
    dp.callback_query.register(set_mode_tickets, F.data == 'admin_support_mode_tickets')
    dp.callback_query.register(set_mode_contact, F.data == 'admin_support_mode_contact')
    dp.callback_query.register(set_mode_both, F.data == 'admin_support_mode_both')
    dp.callback_query.register(start_edit_desc, F.data == 'admin_support_edit_desc')
    dp.callback_query.register(send_desc_copy, F.data == 'admin_support_send_desc')
    dp.callback_query.register(delete_sent_message, F.data == 'admin_support_delete_msg')
    dp.callback_query.register(toggle_admin_notifications, F.data == 'admin_support_toggle_admin_notifications')
    dp.callback_query.register(toggle_user_notifications, F.data == 'admin_support_toggle_user_notifications')
    dp.callback_query.register(toggle_sla, F.data == 'admin_support_toggle_sla')
    dp.callback_query.register(start_set_sla_minutes, F.data == 'admin_support_set_sla_minutes')
    dp.callback_query.register(start_add_moderator, F.data == 'admin_support_add_moderator')
    dp.callback_query.register(start_remove_moderator, F.data == 'admin_support_remove_moderator')
    dp.callback_query.register(list_moderators, F.data == 'admin_support_list_moderators')
    dp.message.register(handle_new_desc, SupportSettingsStates.waiting_for_desc)
    dp.message.register(handle_sla_minutes, SupportAdvancedStates.waiting_for_sla_minutes)
    dp.message.register(handle_moderator_id, SupportAdvancedStates.waiting_for_moderator_id)
