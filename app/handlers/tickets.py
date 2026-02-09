import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.user import get_user_by_id
from app.database.models import Ticket, TicketStatus, User
from app.keyboards.inline import (
    get_my_tickets_keyboard,
    get_ticket_cancel_keyboard,
    get_ticket_reply_cancel_keyboard,
    get_ticket_view_keyboard,
)
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.utils.cache import RateLimitCache, cache, cache_key
from app.utils.photo_message import edit_or_answer_photo
from app.utils.timezone import format_local_datetime


logger = logging.getLogger(__name__)


class TicketStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_message = State()
    waiting_for_reply = State()


async def show_ticket_priority_selection(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    """Начать создание тикета без выбора приоритета: сразу просим заголовок"""
    texts = get_texts(db_user.language)

    # Глобальный блок и наличие активного тикета
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        if blocked_until.year > 9999 - 1:
            await callback.answer(
                texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.'), show_alert=True
            )
        else:
            await callback.answer(
                texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                    time=blocked_until.strftime('%d.%m.%Y %H:%M')
                ),
                show_alert=True,
            )
        return
    if await TicketCRUD.user_has_active_ticket(db, db_user.id):
        await callback.answer(
            texts.t('TICKET_ALREADY_OPEN', 'У вас уже есть незакрытый тикет. Сначала закройте его.'), show_alert=True
        )
        return

    await callback.message.edit_text(
        texts.t('TICKET_TITLE_INPUT', 'Введите заголовок тикета:'),
        reply_markup=get_ticket_cancel_keyboard(db_user.language),
    )
    # Запоминаем исходное сообщение бота, чтобы далее редактировать его, а не слать новые
    await state.update_data(prompt_chat_id=callback.message.chat.id, prompt_message_id=callback.message.message_id)
    await state.set_state(TicketStates.waiting_for_title)
    await callback.answer()


async def handle_ticket_title_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_title:
        return

    """Обработать ввод заголовка тикета"""
    if not message.text:
        asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
        return
    title = message.text.strip()

    data_prompt = await state.get_data()
    prompt_chat_id = data_prompt.get('prompt_chat_id')
    prompt_message_id = data_prompt.get('prompt_message_id')
    # Удалим сообщение пользователя через 2 секунды, чтобы не засорять чат
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
    if len(title) < 5:
        texts = get_texts(db_user.language)
        if prompt_chat_id and prompt_message_id:
            text_val = texts.t(
                'TICKET_TITLE_TOO_SHORT', 'Заголовок должен содержать минимум 5 символов. Попробуйте еще раз:'
            )
            if settings.ENABLE_LOGO_MODE:
                await message.bot.edit_message_caption(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    caption=text_val,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                    parse_mode=None,
                )
            else:
                await message.bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=text_val,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                )
        else:
            await message.answer(
                texts.t('TICKET_TITLE_TOO_SHORT', 'Заголовок должен содержать минимум 5 символов. Попробуйте еще раз:')
            )
        return

    if len(title) > 255:
        texts = get_texts(db_user.language)
        if prompt_chat_id and prompt_message_id:
            text_val = texts.t(
                'TICKET_TITLE_TOO_LONG', 'Заголовок слишком длинный. Максимум 255 символов. Попробуйте еще раз:'
            )
            if settings.ENABLE_LOGO_MODE:
                await message.bot.edit_message_caption(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    caption=text_val,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                    parse_mode=None,
                )
            else:
                await message.bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=text_val,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                )
        else:
            await message.answer(
                texts.t(
                    'TICKET_TITLE_TOO_LONG', 'Заголовок слишком длинный. Максимум 255 символов. Попробуйте еще раз:'
                )
            )
        return

    # Глобальный блок
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        texts = get_texts(db_user.language)
        if blocked_until.year > 9999 - 1:
            await message.answer(texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.'))
        else:
            await message.answer(
                texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                    time=blocked_until.strftime('%d.%m.%Y %H:%M')
                )
            )
        await state.clear()
        return

    await state.update_data(title=title)

    texts = get_texts(db_user.language)

    if prompt_chat_id and prompt_message_id:
        text_val = texts.t('TICKET_MESSAGE_INPUT', 'Опишите проблему (до 500 символов) или отправьте фото с подписью:')
        if settings.ENABLE_LOGO_MODE:
            await message.bot.edit_message_caption(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
                caption=text_val,
                reply_markup=get_ticket_cancel_keyboard(db_user.language),
                parse_mode=None,
            )
        else:
            await message.bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_message_id,
                text=text_val,
                reply_markup=get_ticket_cancel_keyboard(db_user.language),
            )
    else:
        await message.answer(
            texts.t('TICKET_MESSAGE_INPUT', 'Опишите проблему (до 500 символов) или отправьте фото с подписью:'),
            reply_markup=get_ticket_cancel_keyboard(db_user.language),
        )

    await state.set_state(TicketStates.waiting_for_message)


async def handle_ticket_message_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_message:
        return

    # Защита от спама: принимаем только первое сообщение в коротком окне
    try:
        # Глобальный мягкий супрессор на 6 секунд после создания тикета
        try:
            from_cache = await cache.get(cache_key('suppress_user_input', db_user.id))
            if from_cache:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
                return
        except Exception:
            pass
        limited = await RateLimitCache.is_rate_limited(db_user.id, 'ticket_create_message', limit=1, window=2)
        if limited:
            # Удаляем лишние части длинного сообщения
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception:
                pass
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('rl_ts_create')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception:
                pass
            return
        await state.update_data(rl_ts_create=now_ts)
    except Exception:
        pass

    """Обработать ввод сообщения тикета и создать тикет"""
    # Поддержка фото: если прислали фото с подписью — берём caption, сохраняем file_id
    message_text = (message.text or message.caption or '').strip()
    # Ограничим длину текста описания тикета, чтобы избежать проблем с caption/рендером
    if len(message_text) > 500:
        message_text = message_text[:500]
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption
    # Глобальный блок
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        texts = get_texts(db_user.language)
        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        text_msg = (
            texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.')
            if blocked_until.year > 9999 - 1
            else texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                time=blocked_until.strftime('%d.%m.%Y %H:%M')
            )
        )
        if prompt_chat_id and prompt_message_id:
            if settings.ENABLE_LOGO_MODE:
                await message.bot.edit_message_caption(
                    chat_id=prompt_chat_id, message_id=prompt_message_id, caption=text_msg, parse_mode=None
                )
            else:
                await message.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id, text=text_msg)
        else:
            await message.answer(text_msg)
        await state.clear()
        return

    # Удалим сообщение пользователя через 2 секунды
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
    # Валидируем: допускаем пустой текст, если есть фото
    if (not message_text or len(message_text) < 10) and not message.photo:
        texts = get_texts(db_user.language)
        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        err_text = texts.t(
            'TICKET_MESSAGE_TOO_SHORT', 'Сообщение слишком короткое. Опишите проблему подробнее или отправьте фото:'
        )
        if prompt_chat_id and prompt_message_id:
            if settings.ENABLE_LOGO_MODE:
                await message.bot.edit_message_caption(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    caption=err_text,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                    parse_mode=None,
                )
            else:
                await message.bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=err_text,
                    reply_markup=get_ticket_cancel_keyboard(db_user.language),
                )
        else:
            await message.answer(err_text)
        return

    data = await state.get_data()
    title = data.get('title')
    priority = 'normal'

    try:
        ticket = await TicketCRUD.create_ticket(
            db,
            db_user.id,
            title,
            message_text,
            priority,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )
        # Включим временное подавление лишних сообщений пользователя (на случай разбиения длинного текста)
        try:
            await cache.set(cache_key('suppress_user_input', db_user.id), True, 6)
        except Exception:
            pass

        texts = get_texts(db_user.language)
        # Ограничим длину подтверждения чтобы не упереться в лимиты
        safe_title = title if len(title) <= 200 else (title[:197] + '...')
        creation_text = (
            f'✅ <b>Тикет #{ticket.id} создан</b>\n\n'
            f'📝 Заголовок: {safe_title}\n'
            f'📊 Статус: {ticket.status_emoji} '
            f'{texts.t("TICKET_STATUS_OPEN", "Открыт")}\n'
            f'📅 Создан: {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n'
            + ('📎 Вложение: фото\n' if media_type == 'photo' else '')
        )

        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'), callback_data=f'view_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_MENU', '🏠 В главное меню'), callback_data='back_to_menu'
                    )
                ],
            ]
        )
        if prompt_chat_id and prompt_message_id:
            if settings.ENABLE_LOGO_MODE:
                await message.bot.edit_message_caption(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    caption=creation_text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            else:
                await message.bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=creation_text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
        else:
            await message.answer(creation_text, reply_markup=keyboard, parse_mode='HTML')

        await state.clear()

        # Уведомить админов
        await notify_admins_about_new_ticket(ticket, db)

    except Exception as e:
        logger.error(f'Error creating ticket: {e}')
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_CREATE_ERROR', '❌ Произошла ошибка при создании тикета. Попробуйте позже.')
        )


async def show_my_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    # Определяем текущую страницу
    current_page = 1
    if callback.data.startswith('my_tickets_page_'):
        try:
            current_page = int(callback.data.replace('my_tickets_page_', ''))
        except ValueError:
            current_page = 1

    # Пагинация открытых тикетов из БД
    per_page = 10
    total_open = await TicketCRUD.count_user_tickets_by_statuses(
        db, db_user.id, [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value, TicketStatus.PENDING.value]
    )
    total_pages = max(1, (total_open + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    offset = (current_page - 1) * per_page
    open_tickets = await TicketCRUD.get_user_tickets_by_statuses(
        db,
        db_user.id,
        [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value, TicketStatus.PENDING.value],
        limit=per_page,
        offset=offset,
    )

    # Проверка на отсутствие тикетов совсем (ни открытых, ни закрытых)
    has_closed_any = await TicketCRUD.count_user_tickets_by_statuses(db, db_user.id, [TicketStatus.CLOSED.value]) > 0
    if not open_tickets and not has_closed_any:
        await callback.message.edit_text(
            texts.t('NO_TICKETS', 'У вас пока нет тикетов.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CREATE_TICKET_BUTTON', '🎫 Создать тикет'), callback_data='create_ticket'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_CLOSED_TICKETS', '🟢 Закрытые тикеты'), callback_data='my_tickets_closed'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_support')],
                ]
            ),
        )
        await callback.answer()
        return

    # Открытые с пагинацией (DB)
    open_data = [{'id': t.id, 'title': t.title, 'status_emoji': t.status_emoji} for t in open_tickets]
    keyboard = get_my_tickets_keyboard(
        open_data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        page_prefix='my_tickets_page_',
    )
    # Добавим кнопку перехода к закрытым
    keyboard.inline_keyboard.insert(
        0,
        [
            types.InlineKeyboardButton(
                text=texts.t('VIEW_CLOSED_TICKETS', '🟢 Закрытые тикеты'), callback_data='my_tickets_closed'
            )
        ],
    )
    # Всегда используем фото-рендер с логотипом (утилита сама сделает фоллбек при необходимости)
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('MY_TICKETS_TITLE', '📋 Ваши тикеты:'),
        keyboard=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def show_my_tickets_closed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # Пагинация закрытых
    current_page = 1
    data_str = callback.data
    if data_str.startswith('my_tickets_closed_page_'):
        try:
            current_page = int(data_str.replace('my_tickets_closed_page_', ''))
        except ValueError:
            current_page = 1

    per_page = 10
    total_closed = await TicketCRUD.count_user_tickets_by_statuses(db, db_user.id, [TicketStatus.CLOSED.value])
    if total_closed == 0:
        await callback.message.edit_text(
            texts.t('NO_CLOSED_TICKETS', 'Закрытых тикетов пока нет.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_OPEN_TICKETS', '🔴 Открытые тикеты'), callback_data='my_tickets'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_support')],
                ]
            ),
        )
        await callback.answer()
        return
    total_pages = max(1, (total_closed + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    offset = (current_page - 1) * per_page
    tickets = await TicketCRUD.get_user_tickets_by_statuses(
        db, db_user.id, [TicketStatus.CLOSED.value], limit=per_page, offset=offset
    )
    data = [{'id': t.id, 'title': t.title, 'status_emoji': t.status_emoji} for t in tickets]
    kb = get_my_tickets_keyboard(
        data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        page_prefix='my_tickets_closed_page_',
    )
    kb.inline_keyboard.insert(
        0,
        [
            types.InlineKeyboardButton(
                text=texts.t('BACK_TO_OPEN_TICKETS', '🔴 Открытые тикеты'), callback_data='my_tickets'
            )
        ],
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('CLOSED_TICKETS_TITLE', '🟢 Закрытые тикеты:'),
        keyboard=kb,
        parse_mode='HTML',
    )
    await callback.answer()


def _split_long_block(block: str, max_len: int) -> list[str]:
    """Разбивает слишком длинный блок на части."""
    if len(block) <= max_len:
        return [block]

    parts = []
    remaining = block
    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break
        # Ищем место для разрыва (перенос строки или пробел)
        cut_at = max_len
        newline_pos = remaining.rfind('\n', 0, max_len)
        space_pos = remaining.rfind(' ', 0, max_len)

        if newline_pos > max_len // 2:
            cut_at = newline_pos + 1
        elif space_pos > max_len // 2:
            cut_at = space_pos + 1

        parts.append(remaining[:cut_at])
        remaining = remaining[cut_at:]

    return parts


def _split_text_into_pages(header: str, message_blocks: list[str], max_len: int = 3500) -> list[str]:
    """Разбивает текст на страницы с учётом лимита Telegram."""
    pages: list[str] = []
    current = header
    header_len = len(header)
    block_max_len = max_len - header_len - 50  # запас для безопасности

    for block in message_blocks:
        # Если блок сам по себе слишком длинный — разбиваем его
        if len(block) > block_max_len:
            block_parts = _split_long_block(block, block_max_len)
            for part in block_parts:
                if len(current) + len(part) > max_len:
                    if current.strip() and current != header:
                        pages.append(current)
                    current = header + part
                else:
                    current += part
        elif len(current) + len(block) > max_len:
            if current.strip() and current != header:
                pages.append(current)
            current = header + block
        else:
            current += block

    if current.strip():
        pages.append(current)

    return pages if pages else [header]


async def view_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показать детали тикета с пагинацией"""
    data_str = callback.data
    page = 1
    ticket_id = None
    if data_str.startswith('ticket_view_page_'):
        # format: ticket_view_page_{ticket_id}_{page}
        try:
            _, _, _, tid, p = data_str.split('_')
            ticket_id = int(tid)
            page = max(1, int(p))
        except Exception:
            pass
    if ticket_id is None:
        ticket_id = int(data_str.replace('view_ticket_', ''))

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)

    if not ticket or ticket.user_id != db_user.id:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Формируем текст тикета
    status_text = {
        TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'Открыт'),
        TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'Отвечен'),
        TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'Закрыт'),
        TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'В ожидании'),
    }.get(ticket.status, ticket.status)

    header = (
        f'🎫 Тикет #{ticket.id}\n\n'
        f'📝 Заголовок: {ticket.title}\n'
        f'📊 Статус: {ticket.status_emoji} {status_text}\n'
        f'📅 Создан: {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n\n'
    )
    message_blocks: list[str] = []
    if ticket.messages:
        message_blocks.append(f'💬 Сообщения ({len(ticket.messages)}):\n\n')
        for msg in ticket.messages:
            sender = '👤 Вы' if msg.is_user_message else '🛠️ Поддержка'
            block = f'{sender} ({format_local_datetime(msg.created_at, "%d.%m %H:%M")}):\n{msg.message_text}\n\n'
            if getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) == 'photo':
                block += '📎 Вложение: фото\n\n'
            message_blocks.append(block)
    pages = _split_text_into_pages(header, message_blocks, max_len=3500)
    total_pages = len(pages)
    page = min(page, total_pages)

    keyboard = get_ticket_view_keyboard(
        ticket_id,
        ticket.is_closed,
        db_user.language,
    )
    # Если есть вложения фото — добавим кнопку для просмотра
    has_photos = any(
        getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo' for m in ticket.messages or []
    )
    if has_photos:
        try:
            keyboard.inline_keyboard.insert(
                0,
                [
                    types.InlineKeyboardButton(
                        text=texts.t('TICKET_ATTACHMENTS', '📎 Вложения'),
                        callback_data=f'ticket_attachments_{ticket_id}',
                    )
                ],
            )
        except Exception:
            pass
    # Пагинация
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(text='⬅️', callback_data=f'ticket_view_page_{ticket_id}_{page - 1}')
            )
        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(text='➡️', callback_data=f'ticket_view_page_{ticket_id}_{page + 1}')
            )
        try:
            keyboard.inline_keyboard.insert(0, nav_row)
        except Exception:
            pass
    # Показываем как текст (чтобы не упереться в caption лимит)
    page_text = pages[page - 1]
    try:
        await callback.message.edit_text(page_text, reply_markup=keyboard)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(page_text, reply_markup=keyboard)
    await callback.answer()


async def send_ticket_attachments(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        await callback.answer(texts.t('SENDING_ATTACHMENTS', '📎 Отправляю вложения...'))
    except Exception:
        pass
    try:
        ticket_id = int(callback.data.replace('ticket_attachments_', ''))
    except ValueError:
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)
    if not ticket or ticket.user_id != db_user.id:
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    photos = [
        m.media_file_id
        for m in ticket.messages
        if getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo' and m.media_file_id
    ]
    if not photos:
        await callback.answer(texts.t('NO_ATTACHMENTS', 'Вложений нет.'), show_alert=True)
        return

    # Telegram ограничивает media group до 10 элементов. Отправим чанками.
    from aiogram.types import InputMediaPhoto

    chunks = [photos[i : i + 10] for i in range(0, len(photos), 10)]
    last_group_message = None
    for chunk in chunks:
        media = [InputMediaPhoto(media=pid) for pid in chunk]
        try:
            messages = await callback.message.bot.send_media_group(chat_id=callback.from_user.id, media=media)
            if messages:
                last_group_message = messages[-1]
        except Exception:
            pass
    if last_group_message:
        try:
            kb = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('DELETE_MESSAGE', '🗑 Удалить'),
                            callback_data=f'user_delete_message_{last_group_message.message_id}',
                        )
                    ]
                ]
            )
            await callback.message.bot.send_message(
                chat_id=callback.from_user.id, text=texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'), reply_markup=kb
            )
        except Exception:
            pass
    else:
        try:
            await callback.answer(texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'))
        except Exception:
            pass


async def user_delete_message(callback: types.CallbackQuery):
    try:
        msg_id = int(callback.data.replace('user_delete_message_', ''))
    except ValueError:
        await callback.answer('❌')
        return
    try:
        await callback.message.bot.delete_message(chat_id=callback.from_user.id, message_id=msg_id)
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer('✅')


async def _try_delete_message_later(bot: Bot, chat_id: int, message_id: int, delay_seconds: float = 1.0):
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # В приватных чатах удаление сообщений пользователя может быть недоступно — игнорируем ошибки
        pass


async def reply_to_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Начать ответ на тикет"""
    ticket_id = int(callback.data.replace('reply_ticket_', ''))

    await state.update_data(ticket_id=ticket_id)

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_REPLY_INPUT', 'Введите ваш ответ:'),
        reply_markup=get_ticket_reply_cancel_keyboard(db_user.language),
    )

    await state.set_state(TicketStates.waiting_for_reply)
    await callback.answer()


async def handle_ticket_reply(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_reply:
        return

    # Защита от спама: по тикету принимаем только первое сообщение в коротком окне
    try:
        data_rl = await state.get_data()
        rl_ticket_id = data_rl.get('ticket_id') or 'reply'
        limited = await RateLimitCache.is_rate_limited(db_user.id, f'ticket_reply_{rl_ticket_id}', limit=1, window=2)
        if limited:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception:
                pass
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('rl_ts_reply')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception:
                pass
            return
        await state.update_data(rl_ts_reply=now_ts)
    except Exception:
        pass

    """Обработать ответ на тикет"""
    # Поддержка фото для ответа пользователя
    # Ограничение ответа пользователя 500 символов
    reply_text = (message.text or message.caption or '').strip()
    # Строже режем до 400, чтобы учесть форматирование/смайлы
    if len(reply_text) > 400:
        reply_text = reply_text[:400]
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption

    if len(reply_text) < 5:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_TOO_SHORT', 'Ответ должен содержать минимум 5 символов. Попробуйте еще раз:')
        )
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'Ошибка: не найден ID тикета.'))
        await state.clear()
        return

    try:
        # Проверяем, что тикет принадлежит пользователю и не закрыт
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket or ticket.user_id != db_user.id:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'))
            await state.clear()
            return
        if ticket.status == TicketStatus.CLOSED.value:
            texts = get_texts(db_user.language)
            await message.answer(
                texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                                callback_data=f'close_ticket_notification_{ticket.id}',
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        # Блокируем добавление сообщения, если тикет закрыт или заблокирован админом
        if ticket.status == TicketStatus.CLOSED.value or ticket.is_user_reply_blocked:
            texts = get_texts(db_user.language)
            await message.answer(
                texts.t('TICKET_CLOSED_NO_REPLY', '❌ Тикет закрыт, ответить невозможно.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                                callback_data=f'close_ticket_notification_{ticket.id}',
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        # Добавляем сообщение в тикет
        await TicketMessageCRUD.add_message(
            db,
            ticket_id,
            db_user.id,
            reply_text,
            is_from_admin=False,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )

        texts = get_texts(db_user.language)

        await message.answer(
            texts.t('TICKET_REPLY_SENT', '✅ Ваш ответ отправлен!'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'), callback_data=f'view_ticket_{ticket_id}'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_MENU', '🏠 В главное меню'), callback_data='back_to_menu'
                        )
                    ],
                ]
            ),
        )

        await state.clear()

        # Уведомить админов об ответе пользователя
        logger.info(f'Attempting to notify admins about ticket reply #{ticket_id}')
        await notify_admins_about_ticket_reply(ticket, reply_text, db)

    except Exception as e:
        logger.error(f'Error adding ticket reply: {e}')
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_ERROR', '❌ Произошла ошибка при отправке ответа. Попробуйте позже.')
        )


async def close_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Закрыть тикет"""
    ticket_id = int(callback.data.replace('close_ticket_', ''))

    try:
        # Проверяем, что тикет принадлежит пользователю
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket or ticket.user_id != db_user.id:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
            return

        # Запрещаем закрытие, если заблокирован для ответа? (не требуется) Закрываем тикет
        success = await TicketCRUD.close_ticket(db, ticket_id)

        if success:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'), show_alert=True)

            # Обновляем inline-клавиатуру текущего сообщения (убираем кнопки)
            await callback.message.edit_reply_markup(
                reply_markup=get_ticket_view_keyboard(ticket_id, True, db_user.language)
            )
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)

    except Exception as e:
        logger.error(f'Error closing ticket: {e}')
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)


async def cancel_ticket_creation(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Отменить создание тикета"""
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_CREATION_CANCELLED', 'Создание тикета отменено.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_SUPPORT', '⬅️ К поддержке'), callback_data='menu_support'
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def cancel_ticket_reply(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Отменить ответ на тикет"""
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_REPLY_CANCELLED', 'Ответ отменен.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('BACK_TO_TICKETS', '⬅️ К тикетам'), callback_data='my_tickets')]
            ]
        ),
    )
    await callback.answer()


async def close_ticket_notification(callback: types.CallbackQuery, db_user: User):
    """Закрыть уведомление о тикете"""
    texts = get_texts(db_user.language)

    # Проверяем, доступно ли сообщение для удаления
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    await callback.message.delete()
    await callback.answer(texts.t('NOTIFICATION_CLOSED', 'Уведомление закрыто.'))


async def notify_admins_about_new_ticket(ticket: Ticket, db: AsyncSession):
    """Уведомить админов о новом тикете"""
    try:
        from app.config import settings

        if not settings.is_admin_notifications_enabled():
            logger.info(f'Admin notifications disabled. Ticket #{ticket.id} created by user {ticket.user_id}')
            return

        # Получаем язык пользователя для локализации заголовков в уведомлении
        # и формируем удобный текст уведомления для админов
        get_texts(settings.DEFAULT_LANGUAGE)
        title = (ticket.title or '').strip()
        if len(title) > 60:
            title = title[:57] + '...'

        # Загрузим пользователя, чтобы отобразить реальный Telegram ID и username
        try:
            user = await get_user_by_id(db, ticket.user_id)
        except Exception:
            user = None
        full_name = user.full_name if user else 'Unknown'
        telegram_id_display = (user.telegram_id or user.email or f'#{user.id}') if user else '—'
        username_display = (user.username or 'отсутствует') if user else 'отсутствует'

        notification_text = (
            f'🎫 <b>НОВЫЙ ТИКЕТ</b>\n\n'
            f'🆔 <b>ID:</b> <code>{ticket.id}</code>\n'
            f'👤 <b>Пользователь:</b> {full_name}\n'
            f'🆔 <b>ID:</b> <code>{telegram_id_display}</code>\n'
            f'📱 <b>Username:</b> @{username_display}\n'
            f'📝 <b>Заголовок:</b> {title or "—"}\n'
            f'📅 <b>Создан:</b> {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n'
        )

        # Клавиатура с быстрыми действиями для админов в топике
        # Отправляем через общий сервис админ-уведомлений (поддерживает топики)
        # bot доступен из Dispatcher в middlewares; безопаснее взять из уже используемого контекста
        # Здесь используем lazy импорт из maintenance_service, где хранится бот
        from app.services.maintenance_service import maintenance_service

        bot = maintenance_service._bot or None
        if bot is None:
            logger.warning('Bot instance is not available for admin notifications')
            return

        service = AdminNotificationService(bot)
        await service.send_ticket_event_notification(notification_text, None)
    except Exception as e:
        logger.error(f'Error notifying admins about new ticket: {e}')


async def notify_admins_about_ticket_reply(ticket: Ticket, reply_text: str, db: AsyncSession):
    """Уведомить админов об ответе пользователя на тикет"""
    logger.info(f'notify_admins_about_ticket_reply called for ticket #{ticket.id}')
    try:
        from app.config import settings

        if not settings.is_admin_notifications_enabled():
            logger.info(f'Admin notifications disabled. Reply to ticket #{ticket.id}')
            return

        title = (ticket.title or '').strip()
        if len(title) > 60:
            title = title[:57] + '...'

        # Загрузим пользователя
        try:
            user = await get_user_by_id(db, ticket.user_id)
        except Exception:
            user = None
        full_name = user.full_name if user else 'Unknown'
        telegram_id_display = (user.telegram_id or user.email or f'#{user.id}') if user else '—'
        username_display = (user.username or 'отсутствует') if user else 'отсутствует'

        # Обрезаем текст ответа для уведомления
        reply_preview = reply_text[:150] + '...' if len(reply_text) > 150 else reply_text

        notification_text = (
            f'💬 <b>ОТВЕТ НА ТИКЕТ</b>\n\n'
            f'🆔 <b>ID тикета:</b> <code>{ticket.id}</code>\n'
            f'📝 <b>Заголовок:</b> {title or "—"}\n'
            f'👤 <b>Пользователь:</b> {full_name}\n'
            f'🆔 <b>ID:</b> <code>{telegram_id_display}</code>\n'
            f'📱 <b>Username:</b> @{username_display}\n\n'
            f'📩 <b>Сообщение:</b>\n{reply_preview}\n'
        )

        from app.services.maintenance_service import maintenance_service

        bot = maintenance_service._bot or None
        if bot is None:
            logger.warning('Bot instance is not available for admin notifications')
            return

        service = AdminNotificationService(bot)
        result = await service.send_ticket_event_notification(notification_text, None)
        logger.info(f'Ticket #{ticket.id} reply notification sent: {result}')
    except Exception as e:
        logger.error(f'Error notifying admins about ticket reply: {e}')


def register_handlers(dp: Dispatcher):
    """Регистрация обработчиков тикетов"""

    # Создание тикета (теперь без приоритета)
    dp.callback_query.register(show_ticket_priority_selection, F.data == 'create_ticket')

    dp.message.register(handle_ticket_title_input, TicketStates.waiting_for_title)

    dp.message.register(handle_ticket_message_input, TicketStates.waiting_for_message)

    # Просмотр тикетов
    dp.callback_query.register(show_my_tickets, F.data == 'my_tickets')
    dp.callback_query.register(show_my_tickets_closed, F.data == 'my_tickets_closed')
    dp.callback_query.register(show_my_tickets_closed, F.data.startswith('my_tickets_closed_page_'))

    dp.callback_query.register(view_ticket, F.data.startswith('view_ticket_') | F.data.startswith('ticket_view_page_'))

    # Вложения пользователя
    dp.callback_query.register(send_ticket_attachments, F.data.startswith('ticket_attachments_'))

    dp.callback_query.register(user_delete_message, F.data.startswith('user_delete_message_'))

    # Ответы на тикеты
    dp.callback_query.register(reply_to_ticket, F.data.startswith('reply_ticket_'))

    dp.message.register(handle_ticket_reply, TicketStates.waiting_for_reply)

    # Закрытие тикетов
    dp.callback_query.register(close_ticket, F.data.regexp(r'^close_ticket_\d+$'))

    # Отмена операций
    dp.callback_query.register(cancel_ticket_creation, F.data == 'cancel_ticket_creation')

    dp.callback_query.register(cancel_ticket_reply, F.data == 'cancel_ticket_reply')

    # Пагинация тикетов
    dp.callback_query.register(show_my_tickets, F.data.startswith('my_tickets_page_'))

    # Закрытие уведомлений
    dp.callback_query.register(close_ticket_notification, F.data.startswith('close_ticket_notification_'))
