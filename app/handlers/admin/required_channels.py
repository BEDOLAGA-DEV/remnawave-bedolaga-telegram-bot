"""Admin handler for managing required channel subscriptions."""

import html

import structlog
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    get_channel_by_id,
    toggle_channel,
    validate_channel_id,
)
from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
from app.services.channel_subscription_service import channel_subscription_service
from app.services.system_settings_service import normalize_settings_language
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_required_channels')


class AddChannelStates(StatesGroup):
    waiting_channel_id = State()
    waiting_channel_link = State()
    waiting_channel_title = State()


# -- List channels ----------------------------------------------------------------


def _channel_language(event, db_user=None) -> str:
    return normalize_settings_language(
        getattr(db_user, 'language', None),
        getattr(getattr(event, 'from_user', None), 'language_code', None),
    )


def _channels_keyboard(channels: list, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []
    for ch in channels:
        status = '✅' if ch.is_active else '❌'
        title = ch.title or ch.channel_id
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{status} {title}',
                    callback_data=f'reqch:view:{ch.id}',
                )
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNELS_KEYBOARD_000', '➕ Добавить канал'),
                callback_data='reqch:add',
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNELS_KEYBOARD_001', '◀️ Назад'),
                callback_data='admin_submenu_settings',
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _channel_detail_keyboard(channel_id: int, is_active: bool, language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = (
        texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNEL_DETAIL_KEYBOARD_002', '❌ Отключить')
        if is_active
        else texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNEL_DETAIL_KEYBOARD_003', '✅ Включить')
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f'reqch:toggle:{channel_id}')],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNEL_DETAIL_KEYBOARD_004', '🗑 Удалить'),
                    callback_data=f'reqch:delete:{channel_id}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_REQUIRED_CHANNELS_UI_CHANNEL_DETAIL_KEYBOARD_005', '◀️ К списку'),
                    callback_data='reqch:list',
                )
            ],
        ]
    )


@router.callback_query(F.data == 'reqch:list')
@admin_required
async def show_channels_list(callback: CallbackQuery, **kwargs) -> None:
    language = _channel_language(callback, kwargs.get('db_user'))
    texts = get_texts(language)
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    if not channels:
        text = texts.t(
            'ADMIN_REQUIRED_CHANNELS_UI_SHOW_CHANNELS_LIST_006',
            '<b>📢 Обязательные каналы</b>\n\nКаналы не настроены. Нажмите «Добавить» чтобы создать.',
        )
    else:
        lines = [texts.t('ADMIN_REQUIRED_CHANNELS_UI_SHOW_CHANNELS_LIST_007', '<b>📢 Обязательные каналы</b>\n')]
        for ch in channels:
            status = '✅' if ch.is_active else '❌'
            title = ch.title or ch.channel_id
            lines.append(f'{status} <code>{html.escape(ch.channel_id)}</code> — {html.escape(title)}')
        text = '\n'.join(lines)

    await callback.message.edit_text(text, reply_markup=_channels_keyboard(channels, language=language))
    await callback.answer()


@router.callback_query(F.data.startswith('reqch:view:'))
@admin_required
async def view_channel(callback: CallbackQuery, **kwargs) -> None:
    language = _channel_language(callback, kwargs.get('db_user'))
    texts = get_texts(language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_008', 'Неверный ID канала'), show_alert=True
        )
        return
    async with AsyncSessionLocal() as db:
        ch = await get_channel_by_id(db, channel_db_id)

    if not ch:
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_009', 'Канал не найден'), show_alert=True
        )
        return

    status = (
        texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_010', '✅ Активен')
        if ch.is_active
        else texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_011', '❌ Отключён')
    )
    text = texts.t(
        'ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_012',
        '<b>{value1}</b>\n\n<b>ID:</b> <code>{value2}</code>\n<b>Ссылка:</b> {value3}\n<b>Статус:</b> {value4}\n<b>Порядок:</b> {value5}',
    ).format(
        value1=html.escape(ch.title or texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_013', 'Без названия')),
        value2=html.escape(ch.channel_id),
        value3=html.escape(ch.channel_link or '—'),
        value4=status,
        value5=ch.sort_order,
    )

    await callback.message.edit_text(
        text, reply_markup=_channel_detail_keyboard(ch.id, ch.is_active, language=language)
    )
    await callback.answer()


# -- Toggle / Delete ---------------------------------------------------------------


@router.callback_query(F.data.startswith('reqch:toggle:'))
@admin_required
async def toggle_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    language = _channel_language(callback, kwargs.get('db_user'))
    texts = get_texts(language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_008', 'Неверный ID канала'), show_alert=True
        )
        return
    async with AsyncSessionLocal() as db:
        ch = await toggle_channel(db, channel_db_id)

    if ch:
        await channel_subscription_service.invalidate_channels_cache()
        status = (
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_TOGGLE_CHANNEL_HANDLER_015', 'включён')
            if ch.is_active
            else texts.t('ADMIN_REQUIRED_CHANNELS_UI_TOGGLE_CHANNEL_HANDLER_016', 'отключён')
        )
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_TOGGLE_CHANNEL_HANDLER_017', 'Канал {value1}').format(value1=status),
            show_alert=True,
        )

    # Refresh list
    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        texts.t('ADMIN_REQUIRED_CHANNELS_UI_TOGGLE_CHANNEL_HANDLER_018', '<b>📢 Обязательные каналы</b>'),
        reply_markup=_channels_keyboard(channels, language=language),
    )


@router.callback_query(F.data.startswith('reqch:delete:'))
@admin_required
async def delete_channel_handler(callback: CallbackQuery, **kwargs) -> None:
    language = _channel_language(callback, kwargs.get('db_user'))
    texts = get_texts(language)
    try:
        channel_db_id = int(callback.data.split(':')[2])
    except (ValueError, IndexError):
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_VIEW_CHANNEL_008', 'Неверный ID канала'), show_alert=True
        )
        return
    async with AsyncSessionLocal() as db:
        ok = await delete_channel(db, channel_db_id)

    if ok:
        await channel_subscription_service.invalidate_channels_cache()
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_DELETE_CHANNEL_HANDLER_020', 'Канал удалён'), show_alert=True
        )
    else:
        await callback.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_DELETE_CHANNEL_HANDLER_021', 'Ошибка удаления'), show_alert=True
        )

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)
    await callback.message.edit_text(
        texts.t('ADMIN_REQUIRED_CHANNELS_UI_TOGGLE_CHANNEL_HANDLER_018', '<b>📢 Обязательные каналы</b>'),
        reply_markup=_channels_keyboard(channels, language=language),
    )


# -- Add channel flow --------------------------------------------------------------


@router.callback_query(F.data == 'reqch:add')
@admin_required
async def start_add_channel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    language = _channel_language(callback, kwargs.get('db_user'))
    texts = get_texts(language)
    await state.set_state(AddChannelStates.waiting_channel_id)
    await callback.message.edit_text(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_UI_START_ADD_CHANNEL_023',
            '<b>➕ Добавить канал</b>\n\nОтправьте числовой ID канала (например <code>1234567890</code>).\nПрефикс <code>-100</code> добавляется автоматически.',
        )
    )
    await callback.answer()


@router.message(AddChannelStates.waiting_channel_id)
@admin_required
async def process_channel_id(message: Message, state: FSMContext, **kwargs) -> None:
    language = _channel_language(message, kwargs.get('db_user'))
    texts = get_texts(language)
    if not message.text:
        await message.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_ID_024', 'Отправьте текстовое сообщение.')
        )
        return
    channel_id = message.text.strip()

    # Validate and normalize channel_id (auto-prefixes -100 for bare digits)
    try:
        channel_id = validate_channel_id(channel_id)
    except ValueError:
        await message.answer(
            texts.t(
                'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_ID_025', 'Неверный формат. {value1}\n\nПопробуйте ещё раз:'
            ).format(
                value1=texts.t(
                    'ADMIN_ACCESS_CHANNEL_ID_HINT',
                    'Введите числовой ID канала. Префикс -100 добавляется автоматически.',
                )
            )
        )
        return

    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannelStates.waiting_channel_link)
    await message.answer(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_ID_026',
            'Канал: <code>{value1}</code>\n\nТеперь отправьте ссылку на канал (например <code>https://t.me/mychannel</code>)\nИли отправьте <code>-</code> чтобы пропустить:',
        ).format(value1=channel_id)
    )


@router.message(AddChannelStates.waiting_channel_link)
@admin_required
async def process_channel_link(message: Message, state: FSMContext, **kwargs) -> None:
    language = _channel_language(message, kwargs.get('db_user'))
    texts = get_texts(language)
    if not message.text:
        await message.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_ID_024', 'Отправьте текстовое сообщение.')
        )
        return
    link = message.text.strip()
    if link == '-':
        link = None

    if link is not None:
        # Validate and normalize channel link
        if not link.startswith(('https://t.me/', 'http://t.me/', '@')):
            await message.answer(
                texts.t(
                    'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_LINK_028',
                    'Ссылка должна быть URL вида t.me или @username. Попробуйте ещё раз:',
                )
            )
            return
        if link.startswith('@'):
            link = f'https://t.me/{link[1:]}'
        if link.startswith('http://'):
            link = link.replace('http://', 'https://', 1)

    await state.update_data(channel_link=link)
    await state.set_state(AddChannelStates.waiting_channel_title)
    await message.answer(
        texts.t(
            'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_LINK_029',
            'Отправьте название канала (например <code>Новости проекта</code>)\nИли отправьте <code>-</code> чтобы пропустить:',
        )
    )


@router.message(AddChannelStates.waiting_channel_title)
@admin_required
async def process_channel_title(message: Message, state: FSMContext, **kwargs) -> None:
    language = _channel_language(message, kwargs.get('db_user'))
    texts = get_texts(language)
    if not message.text:
        await message.answer(
            texts.t('ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_ID_024', 'Отправьте текстовое сообщение.')
        )
        return
    title = message.text.strip()
    if title == '-':
        title = None

    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        try:
            ch = await add_channel(
                db,
                channel_id=data['channel_id'],
                channel_link=data.get('channel_link'),
                title=title,
            )
            await channel_subscription_service.invalidate_channels_cache()

            text = texts.t(
                'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_TITLE_031',
                '✅ Канал добавлен!\n\n<b>ID:</b> <code>{value1}</code>\n<b>Ссылка:</b> {value2}\n<b>Название:</b> {value3}',
            ).format(
                value1=html.escape(ch.channel_id),
                value2=html.escape(ch.channel_link or '—'),
                value3=html.escape(ch.title or '—'),
            )
        except Exception as e:
            text = texts.t(
                'ADMIN_REQUIRED_CHANNELS_UI_PROCESS_CHANNEL_TITLE_032',
                '❌ Ошибка добавления канала. Попробуйте ещё раз.',
            )
            logger.error('Error adding channel', error=e)

    async with AsyncSessionLocal() as db:
        channels = await get_all_channels(db)

    await message.answer(text, reply_markup=_channels_keyboard(channels, language=language))


def register_handlers(dp_router: Router) -> None:
    dp_router.include_router(router)
