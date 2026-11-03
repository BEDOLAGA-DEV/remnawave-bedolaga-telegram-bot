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
from app.utils.timezone import format_local_datetime
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
    texts = get_texts(language)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_032', '📝 Добавить сообщение'),
                    callback_data='add_user_message',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_033', '📋 Список сообщений'),
                    callback_data='list_user_messages:0',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_034', '📊 Статистика'),
                    callback_data='user_messages_stats',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_035', '🔙 Назад в админку'),
                    callback_data='admin_panel',
                )
            ],
        ]
    )


def get_message_actions_keyboard(message_id: int, is_active: bool, language: str = 'ru'):
    texts = get_texts(language)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    status_text = (
        texts.t('ADMIN_USER_MESSAGE_004', '🔴 Деактивировать')
        if is_active
        else texts.t('ADMIN_USER_MESSAGE_005', '🟢 Активировать')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_036', '✏️ Редактировать'),
                    callback_data=f'edit_user_message:{message_id}',
                )
            ],
            [InlineKeyboardButton(text=status_text, callback_data=f'toggle_user_message:{message_id}')],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_037', '🗑️ Удалить'),
                    callback_data=f'delete_user_message:{message_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_038', '🔙 К списку'),
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
        'ADMIN_USER_MESSAGE_000',
        '📢 <b>Управление сообщениями в главном меню</b>\n\nЗдесь вы можете добавлять сообщения, которые будут показываться пользователям в главном меню между информацией о подписке и кнопками действий.\n\n• Сообщения поддерживают HTML теги\n• Можно создать несколько сообщений\n• Активные сообщения показываются случайно\n• Неактивные сообщения не показываются',
    )

    await callback.message.edit_text(text, reply_markup=get_user_messages_keyboard(db_user.language), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def add_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    await callback.message.edit_text(
        get_texts(db_user.language)
        .t(
            'ADMIN_USER_MESSAGE_010',
            '📝 <b>Добавление нового сообщения</b>\n\nВведите текст сообщения, которое будет показываться в главном меню.\n\n{value1}\n\nОтправьте /cancel для отмены.',
        )
        .format(value1=get_html_help_text(db_user.language)),
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
            texts.t('ADMIN_USER_MESSAGE_013', '❌ Добавление сообщения отменено.'),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )
        return

    message_text = message.text.strip()

    if len(message_text) > 4000:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGE_014',
                '❌ Сообщение слишком длинное. Максимум 4000 символов.\nПопробуйте еще раз или отправьте /cancel для отмены.',
            )
        )
        return

    is_valid, error_msg = validate_html_tags(message_text, db_user.language)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGE_015',
                '❌ Ошибка в HTML разметке: {value1}\n\nИсправьте ошибку и попробуйте еще раз, или отправьте /cancel для отмены.',
            ).format(value1=error_msg),
            parse_mode=None,
        )
        return

    try:
        new_message = await create_user_message(db=db, message_text=message_text, created_by=db_user.id, is_active=True)

        await state.clear()

        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGE_016',
                '✅ <b>Сообщение добавлено!</b>\n\n<b>ID:</b> {value1}\n<b>Статус:</b> {value2}\n<b>Создано:</b> {value3}\n\n<b>Предварительный просмотр:</b>\n<blockquote>{value4}</blockquote>',
            ).format(
                value1=new_message.id,
                value2=texts.t('ADMIN_USER_MESSAGE_006', '🟢 Активно')
                if new_message.is_active
                else texts.t('ADMIN_USER_MESSAGE_007', '🔴 Неактивно'),
                value3=format_local_datetime(new_message.created_at, '%d.%m.%Y %H:%M'),
                value4=message_text,
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error('Ошибка создания сообщения', error=e)
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MESSAGE_024', '❌ Произошла ошибка при создании сообщения. Попробуйте еще раз.'),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )


async def _render_user_messages_list(message: types.Message, db: AsyncSession, language: str, page: int = 0) -> None:
    """Рендерит (edit) список сообщений в `message`. НЕ отвечает на callback —
    это делает вызывающий хендлер.

    Раньше `delete_message_confirm` пере-вызывал `list_user_messages` через
    вручную собранный CallbackQuery, который не привязан к боту → `callback.answer()`
    внутри падал (`method is not mounted to a bot instance`) + попытка второго ответа
    на тот же callback.id. Рендер вынесен сюда и вызывается напрямую.
    """
    texts = get_texts(language)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    limit = 5
    offset = page * limit

    messages = await get_all_user_messages(db, offset=offset, limit=limit)

    if not messages:
        await message.edit_text(
            texts.t(
                'ADMIN_USER_MESSAGE_017', '📋 <b>Список сообщений</b>\n\nСообщений пока нет. Добавьте первое сообщение!'
            ),
            reply_markup=get_user_messages_keyboard(language),
            parse_mode='HTML',
        )
        return

    text = texts.t('ADMIN_USER_MESSAGE_001', '📋 <b>Список сообщений</b>\n\n')

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        preview = msg.message_text[:100] + '...' if len(msg.message_text) > 100 else msg.message_text
        preview = preview.replace('<', '&lt;').replace('>', '&gt;')

        text += f'{status_emoji} <b>ID {msg.id}</b>\n{preview}\n📅 {format_local_datetime(msg.created_at, "%d.%m.%Y %H:%M")}\n\n'

    keyboard = []

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        keyboard.append(
            [InlineKeyboardButton(text=f'{status_emoji} ID {msg.id}', callback_data=f'view_user_message:{msg.id}')]
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MESSAGE_025', '⬅️ Назад'),
                callback_data=f'list_user_messages:{page - 1}',
            )
        )

    nav_buttons.append(
        InlineKeyboardButton(text=texts.t('ADMIN_USER_MESSAGE_018', '➕ Добавить'), callback_data='add_user_message')
    )

    if len(messages) == limit:
        nav_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MESSAGE_026', 'Вперед ➡️'),
                callback_data=f'list_user_messages:{page + 1}',
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append(
        [InlineKeyboardButton(text=texts.t('ADMIN_USER_MESSAGE_027', '🔙 Назад'), callback_data='user_messages_panel')]
    )

    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML')


@admin_required
@error_handler
async def list_user_messages(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = 0
    if ':' in callback.data:
        try:
            page = int(callback.data.split(':')[1])
        except (ValueError, IndexError):
            page = 0

    await _render_user_messages_list(callback.message, db, db_user.language, page)
    await callback.answer()


@admin_required
@error_handler
async def view_user_message(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_028', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_019', '❌ Сообщение не найдено'), show_alert=True)
        return

    safe_content = sanitize_html(message.message_text)

    status_text = (
        texts.t('ADMIN_USER_MESSAGE_006', '🟢 Активно')
        if message.is_active
        else texts.t('ADMIN_USER_MESSAGE_007', '🔴 Неактивно')
    )

    text = texts.t(
        'ADMIN_USER_MESSAGE_002',
        '📋 <b>Сообщение ID {value1}</b>\n\n<b>Статус:</b> {value2}\n<b>Создано:</b> {value3}\n<b>Обновлено:</b> {value4}\n\n<b>Содержимое:</b>\n<blockquote>{value5}</blockquote>',
    ).format(
        value1=message.id,
        value2=status_text,
        value3=format_local_datetime(message.created_at, '%d.%m.%Y %H:%M'),
        value4=format_local_datetime(message.updated_at, '%d.%m.%Y %H:%M'),
        value5=safe_content,
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
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_028', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await toggle_user_message_status(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_019', '❌ Сообщение не найдено'), show_alert=True)
        return

    status_text = (
        texts.t('ADMIN_USER_MESSAGE_008', 'активировано')
        if message.is_active
        else texts.t('ADMIN_USER_MESSAGE_009', 'деактивировано')
    )
    await callback.answer(texts.t('ADMIN_USER_MESSAGE_011', '✅ Сообщение {value1}').format(value1=status_text))

    await view_user_message(callback, db_user, db)


@admin_required
@error_handler
async def delete_message_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтвердить удаление сообщения"""
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_028', '❌ Неверный ID сообщения'), show_alert=True)
        return

    success = await delete_user_message(db, message_id)

    if success:
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_020', '✅ Сообщение удалено'))
        # Рендерим список напрямую (callback уже отвечен выше) — без фейкового
        # CallbackQuery, который терял привязку к боту.
        await _render_user_messages_list(callback.message, db, db_user.language, 0)
    else:
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_021', '❌ Ошибка удаления сообщения'), show_alert=True)


@admin_required
@error_handler
async def show_messages_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_user_messages_stats(db)

    text = texts.t(
        'ADMIN_USER_MESSAGE_003',
        '📊 <b>Статистика сообщений</b>\n\n📝 Всего сообщений: <b>{value1}</b>\n🟢 Активных: <b>{value2}</b>\n🔴 Неактивных: <b>{value3}</b>\n\nАктивные сообщения показываются пользователям случайным образом в главном меню между информацией о подписке и кнопками действий.',
    ).format(value1=stats['total_messages'], value2=stats['active_messages'], value3=stats['inactive_messages'])

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MESSAGE_027', '🔙 Назад'),
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
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_028', '❌ Неверный ID сообщения'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MESSAGE_019', '❌ Сообщение не найдено'), show_alert=True)
        return

    await callback.message.edit_text(
        texts.t(
            'ADMIN_USER_MESSAGE_012',
            '✏️ <b>Редактирование сообщения ID {value1}</b>\n\n<b>Текущий текст:</b>\n<blockquote>{value2}</blockquote>\n\nВведите новый текст сообщения или отправьте /cancel для отмены:',
        ).format(value1=message.id, value2=sanitize_html(message.message_text)),
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
            texts.t('ADMIN_USER_MESSAGE_022', '❌ Редактирование отменено.'),
            reply_markup=get_user_messages_keyboard(db_user.language),
        )
        return

    data = await state.get_data()
    message_id = data.get('editing_message_id')

    if not message_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_USER_MESSAGE_023', '❌ Ошибка: ID сообщения не найден'))
        return

    new_text = message.text.strip()

    if len(new_text) > 4000:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGE_014',
                '❌ Сообщение слишком длинное. Максимум 4000 символов.\nПопробуйте еще раз или отправьте /cancel для отмены.',
            )
        )
        return

    is_valid, error_msg = validate_html_tags(new_text, db_user.language)
    if not is_valid:
        await message.answer(
            texts.t(
                'ADMIN_USER_MESSAGE_015',
                '❌ Ошибка в HTML разметке: {value1}\n\nИсправьте ошибку и попробуйте еще раз, или отправьте /cancel для отмены.',
            ).format(value1=error_msg),
            parse_mode=None,
        )
        return

    try:
        updated_message = await update_user_message(db=db, message_id=message_id, message_text=new_text)

        if updated_message:
            await state.clear()
            await message.answer(
                texts.t(
                    'ADMIN_USER_MESSAGE_029',
                    '✅ <b>Сообщение обновлено!</b>\n\n<b>ID:</b> {value1}\n<b>Обновлено:</b> {value2}\n\n<b>Новый текст:</b>\n<blockquote>{value3}</blockquote>',
                ).format(
                    value1=updated_message.id,
                    value2=format_local_datetime(updated_message.updated_at, '%d.%m.%Y %H:%M'),
                    value3=sanitize_html(new_text),
                ),
                reply_markup=get_user_messages_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await state.clear()
            await message.answer(
                texts.t('ADMIN_USER_MESSAGE_030', '❌ Сообщение не найдено или ошибка обновления.'),
                reply_markup=get_user_messages_keyboard(db_user.language),
            )

    except Exception as e:
        logger.error('Ошибка обновления сообщения', error=e)
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MESSAGE_031', '❌ Произошла ошибка при обновлении сообщения.'),
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
