import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user_message import (
    create_user_message,
    delete_user_message,
    get_all_user_messages,
    get_user_message_by_id,
    get_user_messages_stats,
    toggle_user_message_status,
    update_user_message,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.validators import (
    get_html_help_text,
    sanitize_html,
    validate_html_tags,
)


logger = structlog.get_logger(__name__)


class UserMessageStates(StatesGroup):
    waiting_for_message_text = State()
    waiting_for_edit_text = State()


def get_user_messages_keyboard(language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_ADD', '📝 Добавить сообщение'),
                    callback_data='add_user_message',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_LIST', '📋 Список сообщений'),
                    callback_data='list_user_messages:0',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_STATS', '📊 Статистика'),
                    callback_data='user_messages_stats',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_BACK_TO_ADMIN', '🔙 Назад в админку'),
                    callback_data='admin_panel',
                )
            ],
        ]
    )


def get_message_actions_keyboard(message_id: int, is_active: bool, language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)
    status_text = (
        texts.t('ADMIN_USER_MESSAGES_BUTTON_DEACTIVATE', '🔴 Деактивировать')
        if is_active
        else texts.t('ADMIN_USER_MESSAGES_BUTTON_ACTIVATE', '🟢 Активировать')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_EDIT', '✏️ Редактировать'),
                    callback_data=f'edit_user_message:{message_id}',
                )
            ],
            [InlineKeyboardButton(text=status_text, callback_data=f'toggle_user_message:{message_id}')],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_DELETE', '🗑️ Удалить'),
                    callback_data=f'delete_user_message:{message_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_BACK_TO_LIST', '🔙 К списку'),
                    callback_data='list_user_messages:0',
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_user_messages_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        'ADMIN_USER_MESSAGES_PANEL_TEXT',
        '📢 <b>Управление сообщениями в главном меню</b>\n\n'
        'Здесь вы можете добавлять сообщения, которые будут показываться пользователям '
        'в главном меню между информацией о подписке и кнопками действий.\n\n'
        '• Сообщения поддерживают HTML теги\n'
        '• Можно создать несколько сообщений\n'
        '• Активные сообщения показываются случайно\n'
        '• Неактивные сообщения не показываются',
    )

    await callback.message.edit_text(text, reply_markup=get_user_messages_keyboard(db_user.language), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def add_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_USER_MESSAGES_ADD_PROMPT',
            '📝 <b>Добавление нового сообщения</b>\n\n'
            'Введите текст сообщения, которое будет показываться в главном меню.\n\n'
            '{html_help}\n\n'
            'Отправьте /cancel для отмены.',
        ).format(html_help=get_html_help_text()),
        parse_mode='HTML',
    )

    await state.set_state(UserMessageStates.waiting_for_message_text)
    await callback.answer()


@admin_required
@error_handler
async def process_new_message_text(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    if message.text == '/cancel':
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MESSAGES_ADD_CANCELED', '❌ Добавление сообщения отменено.'),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )
        return

    message_text = message.text.strip()

    if len(message_text) > 4000:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_TEXT_TOO_LONG',
                '❌ Сообщение слишком длинное. Максимум 4000 символов.\n'
                'Попробуйте еще раз или отправьте /cancel для отмены.',
            )
        )
        return

    is_valid, error_msg = validate_html_tags(message_text)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_HTML_ERROR',
                '❌ Ошибка в HTML разметке: {error}\n\n'
                'Исправьте ошибку и попробуйте еще раз, или отправьте /cancel для отмены.',
            ).format(error=error_msg),
            parse_mode=None,
        )
        return

    try:
        new_message = await create_user_message(db=db, message_text=message_text, created_by=db_user.id, is_active=True)

        await state.clear()

        status_text = (
            texts.t('ADMIN_USER_MESSAGES_STATUS_ACTIVE', '🟢 Активно')
            if new_message.is_active
            else texts.t('ADMIN_USER_MESSAGES_STATUS_INACTIVE', '🔴 Неактивно')
        )
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_ADD_SUCCESS',
                '✅ <b>Сообщение добавлено!</b>\n\n'
                '<b>ID:</b> {message_id}\n'
                '<b>Статус:</b> {status}\n'
                '<b>Создано:</b> {created_at}\n\n'
                '<b>Предварительный просмотр:</b>\n'
                '<blockquote>{preview}</blockquote>',
            ).format(
                message_id=new_message.id,
                status=status_text,
                created_at=new_message.created_at.strftime('%d.%m.%Y %H:%M'),
                preview=message_text,
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error('Ошибка создания сообщения', error=e)
        await state.clear()
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_ADD_ERROR',
                '❌ Произошла ошибка при создании сообщения. Попробуйте еще раз.',
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )


@admin_required
@error_handler
async def list_user_messages(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    page = 0
    if ':' in callback.data:
        try:
            page = int(callback.data.split(':')[1])
        except (ValueError, IndexError):
            page = 0

    limit = 5
    offset = page * limit

    messages = await get_all_user_messages(db, offset=offset, limit=limit)

    if not messages:
        await callback.message.edit_text(
            texts.t(
                'ADMIN_USER_MESSAGES_LIST_EMPTY',
                '📋 <b>Список сообщений</b>\n\nСообщений пока нет. Добавьте первое сообщение!',
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USER_MESSAGES_LIST_HEADER', '📋 <b>Список сообщений</b>\n\n')

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        preview = msg.message_text[:100] + '...' if len(msg.message_text) > 100 else msg.message_text
        preview = preview.replace('<', '&lt;').replace('>', '&gt;')

        text += texts.t(
            'ADMIN_USER_MESSAGES_LIST_ITEM',
            '{status_emoji} <b>ID {message_id}</b>\n{preview}\n📅 {created_at}\n\n',
        ).format(
            status_emoji=status_emoji,
            message_id=msg.id,
            preview=preview,
            created_at=msg.created_at.strftime('%d.%m.%Y %H:%M'),
        )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = []

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'ADMIN_USER_MESSAGES_LIST_BUTTON_ITEM',
                        '{status_emoji} ID {message_id}',
                    ).format(status_emoji=status_emoji, message_id=msg.id),
                    callback_data=f'view_user_message:{msg.id}',
                )
            ]
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MESSAGES_LIST_BUTTON_PREV', '⬅️ Назад'),
                callback_data=f'list_user_messages:{page - 1}',
            )
        )

    nav_buttons.append(
        InlineKeyboardButton(
            text=texts.t('ADMIN_USER_MESSAGES_LIST_BUTTON_ADD', '➕ Добавить'),
            callback_data='add_user_message',
        )
    )

    if len(messages) == limit:
        nav_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MESSAGES_LIST_BUTTON_NEXT', 'Вперед ➡️'),
                callback_data=f'list_user_messages:{page + 1}',
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MESSAGES_BUTTON_BACK', '🔙 Назад'),
                callback_data='user_messages_panel',
            )
        ]
    )

    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def view_user_message(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_INVALID_ID_ALERT', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_NOT_FOUND_ALERT', '❌ Сообщение не найдено'), show_alert=True)
        return

    safe_content = sanitize_html(message.message_text)

    status_text = (
        texts.t('ADMIN_USER_MESSAGES_STATUS_ACTIVE', '🟢 Активно')
        if message.is_active
        else texts.t('ADMIN_USER_MESSAGES_STATUS_INACTIVE', '🔴 Неактивно')
    )

    text = texts.t(
        'ADMIN_USER_MESSAGES_VIEW_TEXT',
        '📋 <b>Сообщение ID {message_id}</b>\n\n'
        '<b>Статус:</b> {status}\n'
        '<b>Создано:</b> {created_at}\n'
        '<b>Обновлено:</b> {updated_at}\n\n'
        '<b>Содержимое:</b>\n'
        '<blockquote>{content}</blockquote>',
    ).format(
        message_id=message.id,
        status=status_text,
        created_at=message.created_at.strftime('%d.%m.%Y %H:%M'),
        updated_at=message.updated_at.strftime('%d.%m.%Y %H:%M'),
        content=safe_content,
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_message_actions_keyboard(message_id, message.is_active, db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def toggle_message_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_INVALID_ID_ALERT', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await toggle_user_message_status(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_NOT_FOUND_ALERT', '❌ Сообщение не найдено'), show_alert=True)
        return

    status_text = (
        texts.t('ADMIN_USER_MESSAGES_STATUS_ACTIVATED_WORD', 'активировано')
        if message.is_active
        else texts.t('ADMIN_USER_MESSAGES_STATUS_DEACTIVATED_WORD', 'деактивировано')
    )
    await callback.answer(
        texts.t('ADMIN_USER_MESSAGES_TOGGLE_STATUS_TOAST', '✅ Сообщение {status}').format(status=status_text)
    )

    await view_user_message(callback, db_user, db)


@admin_required
@error_handler
async def delete_message_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтвердить удаление сообщения"""
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_INVALID_ID_ALERT', '❌ Неверный ID сообщения'), show_alert=True)
        return

    success = await delete_user_message(db, message_id)

    if success:
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_DELETE_SUCCESS_TOAST', '✅ Сообщение удалено'))
        await list_user_messages(
            types.CallbackQuery(
                id=callback.id,
                from_user=callback.from_user,
                chat_instance=callback.chat_instance,
                data='list_user_messages:0',
                message=callback.message,
            ),
            db_user,
            db,
        )
    else:
        await callback.answer(
            texts.t('ADMIN_USER_MESSAGES_DELETE_ERROR_ALERT', '❌ Ошибка удаления сообщения'),
            show_alert=True,
        )


@admin_required
@error_handler
async def show_messages_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_user_messages_stats(db)

    text = texts.t(
        'ADMIN_USER_MESSAGES_STATS_TEXT',
        '📊 <b>Статистика сообщений</b>\n\n'
        '📝 Всего сообщений: <b>{total_messages}</b>\n'
        '🟢 Активных: <b>{active_messages}</b>\n'
        '🔴 Неактивных: <b>{inactive_messages}</b>\n\n'
        'Активные сообщения показываются пользователям случайным образом '
        'в главном меню между информацией о подписке и кнопками действий.',
    ).format(
        total_messages=stats['total_messages'],
        active_messages=stats['active_messages'],
        inactive_messages=stats['inactive_messages'],
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGES_BUTTON_BACK', '🔙 Назад'),
                    callback_data='user_messages_panel',
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def edit_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_INVALID_ID_ALERT', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGES_NOT_FOUND_ALERT', '❌ Сообщение не найдено'), show_alert=True)
        return

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USER_MESSAGES_EDIT_PROMPT',
            '✏️ <b>Редактирование сообщения ID {message_id}</b>\n\n'
            '<b>Текущий текст:</b>\n'
            '<blockquote>{current_text}</blockquote>\n\n'
            'Введите новый текст сообщения или отправьте /cancel для отмены:',
        ).format(message_id=message.id, current_text=sanitize_html(message.message_text)),
        parse_mode='HTML',
    )

    await state.set_data({'editing_message_id': message_id})
    await state.set_state(UserMessageStates.waiting_for_edit_text)
    await callback.answer()


@admin_required
@error_handler
async def process_edit_message_text(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    if message.text == '/cancel':
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MESSAGES_EDIT_CANCELED', '❌ Редактирование отменено.'),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )
        return

    data = await state.get_data()
    message_id = data.get('editing_message_id')

    if not message_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_USER_MESSAGES_EDIT_ID_MISSING', '❌ Ошибка: ID сообщения не найден'))
        return

    new_text = message.text.strip()

    if len(new_text) > 4000:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_TEXT_TOO_LONG',
                '❌ Сообщение слишком длинное. Максимум 4000 символов.\n'
                'Попробуйте еще раз или отправьте /cancel для отмены.',
            )
        )
        return

    is_valid, error_msg = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_HTML_ERROR',
                '❌ Ошибка в HTML разметке: {error}\n\n'
                'Исправьте ошибку и попробуйте еще раз, или отправьте /cancel для отмены.',
            ).format(error=error_msg),
            parse_mode=None,
        )
        return

    try:
        updated_message = await update_user_message(db=db, message_id=message_id, message_text=new_text)

        if updated_message:
            await state.clear()
            await message.answer(
                texts.t(
                    'ADMIN_USER_MESSAGES_EDIT_SUCCESS',
                    '✅ <b>Сообщение обновлено!</b>\n\n'
                    '<b>ID:</b> {message_id}\n'
                    '<b>Обновлено:</b> {updated_at}\n\n'
                    '<b>Новый текст:</b>\n'
                    '<blockquote>{new_text}</blockquote>',
                ).format(
                    message_id=updated_message.id,
                    updated_at=updated_message.updated_at.strftime('%d.%m.%Y %H:%M'),
                    new_text=sanitize_html(new_text),
                ),
                reply_markup=get_user_messages_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await state.clear()
            await message.answer(
                texts.t(
                    'ADMIN_USER_MESSAGES_EDIT_NOT_FOUND_OR_ERROR',
                    '❌ Сообщение не найдено или ошибка обновления.',
                ),
                reply_markup=get_user_messages_keyboard(db_user.language),
            )

    except Exception as e:
        logger.error('Ошибка обновления сообщения', error=e)
        await state.clear()
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGES_EDIT_ERROR',
                '❌ Произошла ошибка при обновлении сообщения.',
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_user_messages_panel, F.data == 'user_messages_panel')

    dp.callback_query.register(add_user_message_start, F.data == 'add_user_message')

    dp.message.register(process_new_message_text, StateFilter(UserMessageStates.waiting_for_message_text))

    dp.callback_query.register(edit_user_message_start, F.data.startswith('edit_user_message:'))

    dp.message.register(process_edit_message_text, StateFilter(UserMessageStates.waiting_for_edit_text))

    dp.callback_query.register(list_user_messages, F.data.startswith('list_user_messages'))

    dp.callback_query.register(view_user_message, F.data.startswith('view_user_message:'))

    dp.callback_query.register(toggle_message_status, F.data.startswith('toggle_user_message:'))

    dp.callback_query.register(delete_message_confirm, F.data.startswith('delete_user_message:'))

    dp.callback_query.register(show_messages_stats, F.data == 'user_messages_stats')
