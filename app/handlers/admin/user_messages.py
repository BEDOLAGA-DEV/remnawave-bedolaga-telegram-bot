import logging

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


logger = logging.getLogger(__name__)


class UserMessageStates(StatesGroup):
    waiting_for_message_text = State()
    waiting_for_edit_text = State()


def get_user_messages_keyboard(language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_ADD_BUTTON'), callback_data='add_user_message')],
            [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_LIST_BUTTON'), callback_data='list_user_messages:0')],
            [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_STATS_BUTTON'), callback_data='user_messages_stats')],
            [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_BACK_TO_ADMIN_BUTTON'), callback_data='admin_panel')],
        ]
    )


def get_message_actions_keyboard(message_id: int, is_active: bool, language: str = 'ru'):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(language)
    status_text = (
        texts.t('ADMIN_USER_MSG_DEACTIVATE_BUTTON') if is_active else texts.t('ADMIN_USER_MSG_ACTIVATE_BUTTON')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MSG_EDIT_BUTTON'), callback_data=f'edit_user_message:{message_id}'
                )
            ],
            [InlineKeyboardButton(text=status_text, callback_data=f'toggle_user_message:{message_id}')],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MSG_DELETE_BUTTON'), callback_data=f'delete_user_message:{message_id}'
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_USER_MSG_BACK_TO_LIST_BUTTON'), callback_data='list_user_messages:0'
                )
            ],
        ]
    )


@admin_required
@error_handler
async def show_user_messages_panel(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t('ADMIN_USER_MSG_PANEL_TEXT')

    await callback.message.edit_text(text, reply_markup=get_user_messages_keyboard(db_user.language), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def add_user_message_start(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_USER_MSG_ADD_PROMPT').format(html_help=get_html_help_text()),
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
            texts.t('ADMIN_USER_MSG_ADD_CANCELLED'), reply_markup=get_user_messages_keyboard(db_user.language)
        )
        return

    message_text = message.text.strip()

    if len(message_text) > 4000:
        await message.answer(texts.t('ADMIN_USER_MSG_TOO_LONG'))
        return

    is_valid, error_msg = validate_html_tags(message_text)
    if not is_valid:
        await message.answer(
            texts.t('ADMIN_USER_MSG_HTML_ERROR').format(error=error_msg),
            parse_mode=None,
        )
        return

    try:
        new_message = await create_user_message(db=db, message_text=message_text, created_by=db_user.id, is_active=True)

        await state.clear()

        status_text = (
            texts.t('ADMIN_USER_MSG_STATUS_ACTIVE')
            if new_message.is_active
            else texts.t('ADMIN_USER_MSG_STATUS_INACTIVE')
        )

        await message.answer(
            texts.t('ADMIN_USER_MSG_CREATED_SUCCESS').format(
                id=new_message.id,
                status=status_text,
                created=new_message.created_at.strftime('%d.%m.%Y %H:%M'),
                preview=message_text,
            ),
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )

    except Exception as e:
        logger.error(f'Ошибка создания сообщения: {e}')
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MSG_CREATE_ERROR'),
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
            texts.t('ADMIN_USER_MSG_LIST_EMPTY'),
            reply_markup=get_user_messages_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    text = texts.t('ADMIN_USER_MSG_LIST_TITLE')

    for msg in messages:
        status_emoji = '🟢' if msg.is_active else '🔴'
        preview = msg.message_text[:100] + '...' if len(msg.message_text) > 100 else msg.message_text
        preview = preview.replace('<', '&lt;').replace('>', '&gt;')

        text += texts.t('ADMIN_USER_MSG_LIST_ITEM').format(
            status_emoji=status_emoji,
            id=msg.id,
            preview=preview,
            created=msg.created_at.strftime('%d.%m.%Y %H:%M'),
        )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
                text=texts.t('ADMIN_USER_MSG_PREV_BUTTON'), callback_data=f'list_user_messages:{page - 1}'
            )
        )

    nav_buttons.append(
        InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_ADD_SHORT_BUTTON'), callback_data='add_user_message')
    )

    if len(messages) == limit:
        nav_buttons.append(
            InlineKeyboardButton(
                text=texts.t('ADMIN_USER_MSG_NEXT_BUTTON'), callback_data=f'list_user_messages:{page + 1}'
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append(
        [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_BACK_BUTTON'), callback_data='user_messages_panel')]
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
        await callback.answer(texts.t('ADMIN_USER_MSG_INVALID_ID'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MSG_NOT_FOUND'), show_alert=True)
        return

    safe_content = sanitize_html(message.message_text)

    status_text = (
        texts.t('ADMIN_USER_MSG_STATUS_ACTIVE') if message.is_active else texts.t('ADMIN_USER_MSG_STATUS_INACTIVE')
    )

    text = texts.t('ADMIN_USER_MSG_VIEW_TEXT').format(
        id=message.id,
        status=status_text,
        created=message.created_at.strftime('%d.%m.%Y %H:%M'),
        updated=message.updated_at.strftime('%d.%m.%Y %H:%M'),
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
        await callback.answer(texts.t('ADMIN_USER_MSG_INVALID_ID'), show_alert=True)
        return

    message = await toggle_user_message_status(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MSG_NOT_FOUND'), show_alert=True)
        return

    status_text = (
        texts.t('ADMIN_USER_MSG_STATUS_ACTIVATED')
        if message.is_active
        else texts.t('ADMIN_USER_MSG_STATUS_DEACTIVATED')
    )
    await callback.answer(texts.t('ADMIN_USER_MSG_TOGGLED_STATUS').format(status=status_text))

    await view_user_message(callback, db_user, db)


@admin_required
@error_handler
async def delete_message_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтвердить удаление сообщения"""
    texts = get_texts(db_user.language)
    try:
        message_id = int(callback.data.split(':')[1])
    except (ValueError, IndexError):
        await callback.answer(texts.t('ADMIN_USER_MSG_INVALID_ID'), show_alert=True)
        return

    success = await delete_user_message(db, message_id)

    if success:
        await callback.answer(texts.t('ADMIN_USER_MSG_DELETED'))
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
        await callback.answer(texts.t('ADMIN_USER_MSG_DELETE_ERROR'), show_alert=True)


@admin_required
@error_handler
async def show_messages_stats(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    stats = await get_user_messages_stats(db)

    text = texts.t('ADMIN_USER_MSG_STATS_TEXT').format(
        total=stats['total_messages'],
        active=stats['active_messages'],
        inactive=stats['inactive_messages'],
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('ADMIN_USER_MSG_BACK_BUTTON'), callback_data='user_messages_panel')]
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
        await callback.answer(texts.t('ADMIN_USER_MSG_INVALID_ID'), show_alert=True)
        return

    message = await get_user_message_by_id(db, message_id)

    if not message:
        await callback.answer(texts.t('ADMIN_USER_MSG_NOT_FOUND'), show_alert=True)
        return

    await callback.message.edit_text(
        texts.t('ADMIN_USER_MSG_EDIT_PROMPT').format(
            id=message.id,
            current=sanitize_html(message.message_text),
        ),
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
            texts.t('ADMIN_USER_MSG_EDIT_CANCELLED'), reply_markup=get_user_messages_keyboard(db_user.language)
        )
        return

    data = await state.get_data()
    message_id = data.get('editing_message_id')

    if not message_id:
        await state.clear()
        await message.answer(texts.t('ADMIN_USER_MSG_EDIT_ID_MISSING'))
        return

    new_text = message.text.strip()

    if len(new_text) > 4000:
        await message.answer(texts.t('ADMIN_USER_MSG_TOO_LONG'))
        return

    is_valid, error_msg = validate_html_tags(new_text)
    if not is_valid:
        await message.answer(
            texts.t('ADMIN_USER_MSG_HTML_ERROR').format(error=error_msg),
            parse_mode=None,
        )
        return

    try:
        updated_message = await update_user_message(db=db, message_id=message_id, message_text=new_text)

        if updated_message:
            await state.clear()
            await message.answer(
                texts.t('ADMIN_USER_MSG_UPDATED_SUCCESS').format(
                    id=updated_message.id,
                    updated=updated_message.updated_at.strftime('%d.%m.%Y %H:%M'),
                    new_text=sanitize_html(new_text),
                ),
                reply_markup=get_user_messages_keyboard(db_user.language),
                parse_mode='HTML',
            )
        else:
            await state.clear()
            await message.answer(
                texts.t('ADMIN_USER_MSG_UPDATE_NOT_FOUND'),
                reply_markup=get_user_messages_keyboard(db_user.language),
            )

    except Exception as e:
        logger.error(f'Ошибка обновления сообщения: {e}')
        await state.clear()
        await message.answer(
            texts.t('ADMIN_USER_MSG_UPDATE_ERROR'),
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
