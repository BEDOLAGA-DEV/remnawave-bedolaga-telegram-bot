import html
import time
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.models import Ticket, TicketStatus, User
from app.keyboards.inline import (
    get_admin_ticket_reply_cancel_keyboard,
    get_admin_ticket_view_keyboard,
    get_admin_tickets_keyboard,
)
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.states import AdminTicketStates
from app.utils.cache import RateLimitCache


logger = structlog.get_logger(__name__)

# Максимальная длина сообщения Telegram (с запасом)
MAX_MESSAGE_LEN = 3500


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


def _split_text_into_pages(header: str, message_blocks: list[str], max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    """Разбивает текст на страницы с учётом лимита Telegram."""
    pages: list[str] = []
    current = header
    header_len = len(header)
    block_max_len = max_len - header_len - 50

    for block in message_blocks:
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

    return pages or [header]


async def show_admin_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показать все тикеты для админов"""
    # permission gate: admin or active moderator only
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    texts = get_texts(db_user.language)

    # Определяем текущую страницу и scope
    current_page = 1
    scope = 'open'
    data_str = callback.data
    if data_str == 'admin_tickets_scope_open':
        scope = 'open'
    elif data_str == 'admin_tickets_scope_closed':
        scope = 'closed'
    elif data_str.startswith('admin_tickets_page_'):
        try:
            parts = data_str.split('_')
            # format: admin_tickets_page_{scope}_{page}
            if len(parts) >= 5:
                scope = parts[3]
                current_page = int(parts[4])
            else:
                current_page = int(data_str.replace('admin_tickets_page_', ''))
        except ValueError:
            current_page = 1
    statuses = (
        [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value] if scope == 'open' else [TicketStatus.CLOSED.value]
    )
    page_size = 10
    # total count for proper pagination
    total_count = await TicketCRUD.count_tickets_by_statuses(db, statuses)
    total_pages = max(1, (total_count + page_size - 1) // page_size) if total_count > 0 else 1
    current_page = max(current_page, 1)
    current_page = min(current_page, total_pages)
    offset = (current_page - 1) * page_size
    tickets = await TicketCRUD.get_tickets_by_statuses(db, statuses=statuses, limit=page_size, offset=offset)

    # Даже если тикетов нет, показываем переключатели разделов

    # Формируем данные для клавиатуры
    ticket_data = []
    for ticket in tickets:
        user_name = ticket.user.full_name if ticket.user else 'Unknown'
        username = ticket.user.username if ticket.user else None
        telegram_id = ticket.user.telegram_id if ticket.user else None
        ticket_data.append(
            {
                'id': ticket.id,
                'title': ticket.title,
                'status_emoji': ticket.status_emoji,
                'priority_emoji': ticket.priority_emoji,
                'user_name': user_name,
                'username': username,
                'telegram_id': telegram_id,
                'is_closed': ticket.is_closed,
                'locked_emoji': ('🔒' if ticket.is_user_reply_blocked else ''),
            }
        )

    # Итоговые страницы уже посчитаны выше
    header_text = (
        texts.t('ADMIN_TICKETS_TITLE_OPEN', '🎫 Открытые тикеты поддержки:')
        if scope == 'open'
        else texts.t('ADMIN_TICKETS_TITLE_CLOSED', '🎫 Закрытые тикеты поддержки:')
    )
    # Determine proper back target for moderators
    back_cb = 'admin_submenu_support'
    try:
        if not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(callback.from_user.id):
            back_cb = 'moderator_panel'
    except Exception:
        pass

    keyboard = get_admin_tickets_keyboard(
        ticket_data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        scope=scope,
        back_callback=back_cb,
    )
    from app.utils.photo_message import edit_or_answer_photo

    await edit_or_answer_photo(
        callback=callback,
        caption=header_text,
        keyboard=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def view_admin_ticket(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
    ticket_id: int | None = None,
):
    """Показать детали тикета для админа с пагинацией"""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return

    # Парсим ticket_id и page из callback_data
    page = 1
    data_str = callback.data or ''

    if data_str.startswith('admin_ticket_page_'):
        # format: admin_ticket_page_{ticket_id}_{page}
        try:
            parts = data_str.split('_')
            ticket_id = int(parts[3])
            page = max(1, int(parts[4]))
        except (ValueError, IndexError):
            pass
    elif ticket_id is None:
        try:
            ticket_id = int(data_str.split('_')[-1])
        except (ValueError, AttributeError):
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
            return

    if state is None:
        state = FSMContext(callback.bot, callback.from_user.id)

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True, load_user=True)

    if not ticket:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Формируем заголовок тикета
    status_text = {
        TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'Открыт'),
        TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'Отвечен'),
        TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'Закрыт'),
        TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'В ожидании'),
    }.get(ticket.status, ticket.status)

    user_name = html.escape(ticket.user.full_name) if ticket.user else 'Unknown'
    telegram_id_display = (
        html.escape(str(ticket.user.telegram_id or ticket.user.email or f'#{ticket.user.id}')) if ticket.user else '—'
    )
    username_value = ticket.user.username if ticket.user else None
    id_label = 'Telegram ID' if (ticket.user and ticket.user.telegram_id) else 'ID'

    header = f'🎫 Тикет #{ticket.id}\n\n'
    header += f'👤 Пользователь: {user_name}\n'
    header += f'🆔 {id_label}: <code>{telegram_id_display}</code>\n'
    if username_value:
        safe_username = html.escape(username_value)
        header += f'📱 Username: @{safe_username}\n'
    else:
        header += '📱 Username: отсутствует\n'
    header += f'📝 Заголовок: {html.escape(ticket.title)}\n'
    header += f'📊 Статус: {ticket.status_emoji} {status_text}\n'
    header += f'📅 Создан: {ticket.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    if ticket.is_user_reply_blocked:
        if ticket.user_reply_block_permanent:
            header += '🚫 Пользователь заблокирован навсегда\n\n'
        elif ticket.user_reply_block_until:
            header += f'⏳ Блок до: {ticket.user_reply_block_until.strftime("%d.%m.%Y %H:%M")}\n\n'

    # Формируем блоки сообщений
    message_blocks: list[str] = []
    if ticket.messages:
        message_blocks.append(f'💬 Сообщения ({len(ticket.messages)}):\n\n')
        for msg in ticket.messages:
            sender = '👤 Пользователь' if msg.is_user_message else '🛠️ Поддержка'
            block = f'{sender} ({msg.created_at.strftime("%d.%m %H:%M")}):\n{html.escape(msg.message_text)}\n\n'
            if getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) == 'photo':
                block += '📎 Вложение: фото\n\n'
            message_blocks.append(block)

    # Разбиваем на страницы
    pages = _split_text_into_pages(header, message_blocks, max_len=MAX_MESSAGE_LEN)
    total_pages = len(pages)
    page = min(page, total_pages)

    # Формируем клавиатуру
    has_photos = any(
        getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo' for m in ticket.messages or []
    )
    keyboard = get_admin_ticket_view_keyboard(
        ticket_id, ticket.is_closed, db_user.language, is_user_blocked=ticket.is_user_reply_blocked
    )

    # Кнопка профиля пользователя
    try:
        if ticket.user:
            admin_profile_btn = types.InlineKeyboardButton(
                text='👤 К пользователю', callback_data=f'admin_user_manage_{ticket.user.id}_from_ticket_{ticket.id}'
            )
            keyboard.inline_keyboard.insert(0, [admin_profile_btn])
    except Exception:
        pass

    # Кнопки ЛС и профиль
    try:
        if ticket.user and ticket.user.telegram_id and ticket.user.username:
            safe_username = html.escape(ticket.user.username)
            buttons_row = []
            pm_url = f'tg://resolve?domain={safe_username}'
            buttons_row.append(types.InlineKeyboardButton(text='✉ ЛС', url=pm_url))
            profile_url = f'tg://user?id={ticket.user.telegram_id}'
            buttons_row.append(types.InlineKeyboardButton(text='👤 Профиль', url=profile_url))
            if buttons_row:
                keyboard.inline_keyboard.insert(0, buttons_row)
    except Exception:
        pass

    # Кнопка вложений
    if has_photos:
        try:
            keyboard.inline_keyboard.insert(
                0,
                [
                    types.InlineKeyboardButton(
                        text=texts.t('TICKET_ATTACHMENTS', '📎 Вложения'),
                        callback_data=f'admin_ticket_attachments_{ticket_id}',
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
                types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_ticket_page_{ticket_id}_{page - 1}')
            )
        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(text='➡️', callback_data=f'admin_ticket_page_{ticket_id}_{page + 1}')
            )
        try:
            keyboard.inline_keyboard.insert(0, nav_row)
        except Exception:
            pass

    page_text = pages[page - 1]

    # Отправка сообщения
    try:
        await callback.message.edit_text(page_text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(page_text, reply_markup=keyboard, parse_mode='HTML')

    # Сохраняем id для дальнейших действий
    if state is not None:
        try:
            await state.update_data(ticket_id=ticket_id)
        except Exception:
            pass
    await callback.answer()


async def reply_to_admin_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Начать ответ на тикет от админа"""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_reply_ticket_', ''))

    await state.update_data(ticket_id=ticket_id, reply_mode=True)
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t('ADMIN_TICKET_REPLY_INPUT', 'Введите ответ от поддержки:'),
        reply_markup=get_admin_ticket_reply_cancel_keyboard(db_user.language),
    )

    await state.set_state(AdminTicketStates.waiting_for_reply)
    await callback.answer()


async def handle_admin_ticket_reply(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    if not (settings.is_admin(message.from_user.id) or SupportSettingsService.is_moderator(message.from_user.id)):
        texts = get_texts(db_user.language)
        await message.answer(texts.ACCESS_DENIED)
        await state.clear()
        return
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != AdminTicketStates.waiting_for_reply:
        return

    # Анти-спам: одно сообщение за короткое окно по конкретному тикету
    try:
        data_rl = await state.get_data()
        rl_ticket_id = data_rl.get('ticket_id') or 'admin_reply'
        limited = await RateLimitCache.is_rate_limited(
            db_user.id, f'admin_ticket_reply_{rl_ticket_id}', limit=1, window=2
        )
        if limited:
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('admin_rl_ts_reply')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            return
        await state.update_data(admin_rl_ts_reply=now_ts)
    except Exception:
        pass

    """Обработать ответ админа на тикет"""
    # Поддержка фото вложений в ответе админа
    reply_text = (message.text or message.caption or '').strip()
    if len(reply_text) > 400:
        reply_text = reply_text[:400]
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption

    if len(reply_text) < 1 and not media_file_id:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_TOO_SHORT', 'Ответ должен содержать минимум 5 символов. Попробуйте еще раз:')
        )
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')
    try:
        ticket_id = int(ticket_id) if ticket_id is not None else None
    except (TypeError, ValueError):
        ticket_id = None

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'Ошибка: не найден ID тикета.'))
        await state.clear()
        return

    try:
        # Если это режим ввода длительности блокировки
        if not data.get('reply_mode'):
            try:
                minutes = int(reply_text)
                minutes = max(1, min(60 * 24 * 365, minutes))
            except ValueError:
                await message.answer('❌ Введите целое число минут')
                return
            until = datetime.now(UTC) + timedelta(minutes=minutes)
            ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=until)
            if ok:
                await message.answer(f'✅ Пользователь заблокирован на {minutes} минут')
            else:
                await message.answer('❌ Ошибка блокировки')
            await state.clear()
            return

        # Обычный режим ответа админа
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
        if not ticket:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'))
            await state.clear()
            return

        # Добавляем сообщение от админа (внутри add_message статус станет ANSWERED)
        await TicketMessageCRUD.add_message(
            db,
            ticket_id,
            db_user.id,
            reply_text,
            is_from_admin=True,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )

        texts = get_texts(db_user.language)

        await message.answer(
            texts.t('ADMIN_TICKET_REPLY_SENT', '✅ Ответ отправлен!'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'),
                            callback_data=f'admin_view_ticket_{ticket_id}',
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_TICKETS', '⬅️ К тикетам'), callback_data='admin_tickets'
                        )
                    ],
                ]
            ),
        )

        await state.clear()

        # Уведомляем пользователя о новом ответе
        await notify_user_about_ticket_reply(message.bot, ticket, reply_text, db)
        # Админ-уведомления о ответе в тикет отключены по требованию

    except Exception as e:
        logger.error('Error adding admin ticket reply', error=e)
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_ERROR', '❌ Произошла ошибка при отправке ответа. Попробуйте позже.')
        )


async def mark_ticket_as_answered(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Отметить тикет как отвеченный"""
    ticket_id = int(callback.data.replace('admin_mark_answered_', ''))

    try:
        success = await TicketCRUD.update_ticket_status(db, ticket_id, TicketStatus.ANSWERED.value)

        if success:
            texts = get_texts(db_user.language)
            await callback.answer(
                texts.t('TICKET_MARKED_ANSWERED', '✅ Тикет отмечен как отвеченный.'), show_alert=True
            )

            # Обновляем сообщение
            await view_admin_ticket(callback, db_user, db, state)
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ Ошибка при обновлении тикета.'), show_alert=True)

    except Exception as e:
        logger.error('Error marking ticket as answered', error=e)
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ Ошибка при обновлении тикета.'), show_alert=True)


async def close_all_open_admin_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Закрыть все открытые тикеты."""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return

    texts = get_texts(db_user.language)

    try:
        closed_ticket_ids = await TicketCRUD.close_all_open_tickets(db)
    except Exception as error:
        logger.error('Error closing all open tickets', error=error)
        await callback.answer(texts.t('TICKET_UPDATE_ERROR', '❌ Ошибка при обновлении тикета.'), show_alert=True)
        return

    closed_count = len(closed_ticket_ids)

    if closed_count == 0:
        await callback.answer(
            texts.t('ADMIN_CLOSE_ALL_OPEN_TICKETS_EMPTY', 'ℹ️ Нет открытых тикетов для закрытия.'), show_alert=True
        )
        return

    try:
        is_moderator = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
            callback.from_user.id
        )
        await TicketCRUD.add_support_audit(
            db,
            actor_user_id=db_user.id if db_user else None,
            actor_telegram_id=callback.from_user.id,
            is_moderator=is_moderator,
            action='close_all_tickets',
            ticket_id=None,
            target_user_id=None,
            details={
                'count': closed_count,
                'ticket_ids': closed_ticket_ids,
            },
        )
    except Exception as audit_error:
        logger.warning('Failed to add support audit for bulk close', audit_error=audit_error)

    # Обновляем список тикетов
    await show_admin_tickets(callback, db_user, db)

    success_text = texts.t('ADMIN_CLOSE_ALL_OPEN_TICKETS_SUCCESS', '✅ Закрыто открытых тикетов: {count}').format(
        count=closed_count
    )

    notification_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]]
    )

    try:
        await callback.message.answer(success_text, reply_markup=notification_keyboard)
    except Exception:
        # Если не удалось отправить отдельное сообщение, пробуем ответить алертом
        try:
            await callback.answer(success_text, show_alert=True)
        except Exception:
            pass


async def close_admin_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Закрыть тикет админом"""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_close_ticket_', ''))

    try:
        success = await TicketCRUD.close_ticket(db, ticket_id)

        if success:
            # audit
            try:
                is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                    callback.from_user.id
                )
                # обогатим details контактами пользователя тикета
                details = {}
                try:
                    t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                    if t and t.user:
                        details.update(
                            {
                                'target_telegram_id': t.user.telegram_id,
                                'target_username': t.user.username,
                            }
                        )
                except Exception:
                    pass
                await TicketCRUD.add_support_audit(
                    db,
                    actor_user_id=db_user.id if db_user else None,
                    actor_telegram_id=callback.from_user.id,
                    is_moderator=is_mod,
                    action='close_ticket',
                    ticket_id=ticket_id,
                    target_user_id=None,
                    details=details,
                )
            except Exception:
                pass
            texts = get_texts(db_user.language)
            # Notify with deletable inline message
            try:
                await callback.message.answer(
                    texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'),
                    reply_markup=types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]
                        ]
                    ),
                )
            except Exception:
                await callback.answer(texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'), show_alert=True)

            # Обновляем inline-клавиатуру в текущем сообщении без кнопок действий
            await callback.message.edit_reply_markup(
                reply_markup=get_admin_ticket_view_keyboard(ticket_id, True, db_user.language)
            )
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)

    except Exception as e:
        logger.error('Error closing admin ticket', error=e)
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)


async def cancel_admin_ticket_reply(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Отменить ответ админа на тикет"""
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_REPLY_CANCELLED', 'Ответ отменен.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_TICKETS', '⬅️ К тикетам'), callback_data='admin_tickets'
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def block_user_in_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_block_user_ticket_', ''))
    texts = get_texts(db_user.language)
    # Save original ticket message ids to update it after blocking without reopening
    try:
        await state.update_data(origin_chat_id=callback.message.chat.id, origin_message_id=callback.message.message_id)
    except Exception:
        pass
    await callback.message.edit_text(
        texts.t('ENTER_BLOCK_MINUTES', 'Введите количество минут для блокировки пользователя (например, 15):'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CANCEL_REPLY', '❌ Отменить ввод'), callback_data='cancel_admin_ticket_reply'
                    )
                ]
            ]
        ),
    )
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(AdminTicketStates.waiting_for_block_duration)
    await callback.answer()


async def handle_admin_block_duration_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # permission gate for message flow
    if not (settings.is_admin(message.from_user.id) or SupportSettingsService.is_moderator(message.from_user.id)):
        texts = get_texts(db_user.language)
        await message.answer(texts.ACCESS_DENIED)
        await state.clear()
        return
    # Проверяем состояние
    current_state = await state.get_state()
    if current_state != AdminTicketStates.waiting_for_block_duration:
        return

    reply_text = message.text.strip()
    if len(reply_text) < 1:
        await message.answer('❌ Введите целое число минут')
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')
    origin_chat_id = data.get('origin_chat_id')
    origin_message_id = data.get('origin_message_id')
    try:
        minutes = int(reply_text)
        minutes = max(1, min(60 * 24 * 365, minutes))  # максимум 1 год
    except ValueError:
        await message.answer('❌ Введите целое число минут')
        return

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'Ошибка: не найден ID тикета.'))
        await state.clear()
        return

    try:
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'))
            await state.clear()
            return

        until = datetime.now(UTC) + timedelta(minutes=minutes)
        ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=until)
        if not ok:
            await message.answer('❌ Ошибка блокировки')
            return
        # audit
        try:
            is_mod = not settings.is_admin(message.from_user.id) and SupportSettingsService.is_moderator(
                message.from_user.id
            )
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=message.from_user.id,
                is_moderator=is_mod,
                action='block_user_timed',
                ticket_id=ticket_id,
                target_user_id=ticket.user_id if ticket else None,
                details={'minutes': minutes},
            )
        except Exception:
            pass
        # Refresh original ticket card (caption/text and buttons) in place
        try:
            updated = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True, load_user=True)
            texts = get_texts(db_user.language)
            status_text = {
                TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'Открыт'),
                TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'Отвечен'),
                TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'Закрыт'),
                TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'В ожидании'),
            }.get(updated.status, updated.status)
            user_name = html.escape(updated.user.full_name) if updated.user else 'Unknown'
            ticket_text = f'🎫 Тикет #{updated.id}\n\n'
            ticket_text += f'👤 Пользователь: {user_name}\n'
            ticket_text += f'📝 Заголовок: {html.escape(updated.title)}\n'
            ticket_text += f'📊 Статус: {updated.status_emoji} {status_text}\n'
            ticket_text += f'📅 Создан: {updated.created_at.strftime("%d.%m.%Y %H:%M")}\n'
            ticket_text += f'🔄 Обновлен: {updated.updated_at.strftime("%d.%m.%Y %H:%M")}\n'
            if updated.user and updated.user.telegram_id:
                ticket_text += f'🆔 Telegram ID: <code>{updated.user.telegram_id}</code>\n'
                if updated.user.username:
                    safe_username = html.escape(updated.user.username)
                    ticket_text += f'📱 Username: @{safe_username}\n'
                    ticket_text += (
                        f'🔗 ЛС: <a href="tg://resolve?domain={safe_username}">'
                        f'tg://resolve?domain={safe_username}</a>\n'
                    )
                else:
                    ticket_text += '📱 Username: отсутствует\n'
                    chat_link = f'tg://user?id={int(updated.user.telegram_id)}'
                    ticket_text += f'🔗 Чат по ID: <a href="{chat_link}">{chat_link}</a>\n'
            elif updated.user:
                # Email-only user
                user_id_display = html.escape(str(updated.user.email or f'#{updated.user.id}'))
                ticket_text += f'🆔 ID: <code>{user_id_display}</code>\n'
                ticket_text += '📧 Тип: Email-пользователь\n'
            ticket_text += '\n'
            if updated.is_user_reply_blocked:
                if updated.user_reply_block_permanent:
                    ticket_text += '🚫 Пользователь заблокирован навсегда для ответов в этом тикете\n'
                elif updated.user_reply_block_until:
                    ticket_text += f'⏳ Блок до: {updated.user_reply_block_until.strftime("%d.%m.%Y %H:%M")}\n'
            if updated.messages:
                ticket_text += f'💬 Сообщения ({len(updated.messages)}):\n\n'
                for msg in updated.messages:
                    sender = '👤 Пользователь' if msg.is_user_message else '🛠️ Поддержка'
                    ticket_text += f'{sender} ({msg.created_at.strftime("%d.%m %H:%M")}):\n'
                    ticket_text += f'{html.escape(msg.message_text)}\n\n'
                    if getattr(msg, 'has_media', False) and getattr(msg, 'media_type', None) == 'photo':
                        ticket_text += '📎 Вложение: фото\n\n'

            kb = get_admin_ticket_view_keyboard(
                updated.id, updated.is_closed, db_user.language, is_user_blocked=updated.is_user_reply_blocked
            )
            # Кнопка открытия профиля пользователя в админке
            try:
                if updated.user:
                    admin_profile_btn = types.InlineKeyboardButton(
                        text='👤 К пользователю',
                        callback_data=f'admin_user_manage_{updated.user.id}_from_ticket_{updated.id}',
                    )
                    kb.inline_keyboard.insert(0, [admin_profile_btn])
            except Exception:
                pass
            # Кнопки ЛС и профиль при обновлении карточки
            try:
                if updated.user and updated.user.telegram_id and updated.user.username:
                    safe_username = html.escape(updated.user.username)
                    buttons_row = []
                    pm_url = f'tg://resolve?domain={safe_username}'
                    buttons_row.append(types.InlineKeyboardButton(text='✉ Написать в ЛС', url=pm_url))
                    profile_url = f'tg://user?id={updated.user.telegram_id}'
                    buttons_row.append(types.InlineKeyboardButton(text='👤 Профиль', url=profile_url))
                    if buttons_row:
                        kb.inline_keyboard.insert(0, buttons_row)
            except Exception:
                pass
            has_photos = any(
                getattr(m, 'has_media', False) and getattr(m, 'media_type', None) == 'photo'
                for m in updated.messages or []
            )
            if has_photos:
                try:
                    kb.inline_keyboard.insert(
                        0,
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('TICKET_ATTACHMENTS', '📎 Вложения'),
                                callback_data=f'admin_ticket_attachments_{updated.id}',
                            )
                        ],
                    )
                except Exception:
                    pass
            if origin_chat_id and origin_message_id:
                try:
                    await message.bot.edit_message_caption(
                        chat_id=origin_chat_id,
                        message_id=origin_message_id,
                        caption=ticket_text,
                        reply_markup=kb,
                        parse_mode='HTML',
                    )
                except Exception:
                    try:
                        await message.bot.edit_message_text(
                            chat_id=origin_chat_id,
                            message_id=origin_message_id,
                            text=ticket_text,
                            reply_markup=kb,
                            parse_mode='HTML',
                        )
                    except Exception:
                        await message.answer(f'✅ Пользователь заблокирован на {minutes} минут')
            else:
                await message.answer(f'✅ Пользователь заблокирован на {minutes} минут')
        except Exception:
            await message.answer(f'✅ Пользователь заблокирован на {minutes} минут')
        finally:
            await state.clear()
    except Exception as e:
        logger.error('Error setting block duration', error=e)
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', '❌ Произошла ошибка. Попробуйте позже.'))


async def unblock_user_in_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_unblock_user_ticket_', ''))
    ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=False, until=None)
    if ok:
        try:
            await callback.message.answer(
                '✅ Блок снят',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]
                    ]
                ),
            )
        except Exception:
            await callback.answer('✅ Блок снят')
        # audit
        try:
            is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                callback.from_user.id
            )
            ticket_id = int(callback.data.replace('admin_unblock_user_ticket_', ''))
            details = {}
            try:
                t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                if t and t.user:
                    details.update(
                        {
                            'target_telegram_id': t.user.telegram_id,
                            'target_username': t.user.username,
                        }
                    )
            except Exception:
                pass
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=callback.from_user.id,
                is_moderator=is_mod,
                action='unblock_user',
                ticket_id=ticket_id,
                target_user_id=None,
                details=details,
            )
        except Exception:
            pass
        await view_admin_ticket(callback, db_user, db, state)
    else:
        await callback.answer('❌ Ошибка', show_alert=True)


async def block_user_permanently(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
        texts = get_texts(db_user.language)
        await callback.answer(texts.ACCESS_DENIED, show_alert=True)
        return
    ticket_id = int(callback.data.replace('admin_block_user_perm_ticket_', ''))
    ok = await TicketCRUD.set_user_reply_block(db, ticket_id, permanent=True, until=None)
    if ok:
        try:
            await callback.message.answer(
                '✅ Пользователь заблокирован навсегда',
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(text='🗑 Удалить', callback_data='admin_support_delete_msg')]
                    ]
                ),
            )
        except Exception:
            await callback.answer('✅ Пользователь заблокирован')
        # audit
        try:
            is_mod = not settings.is_admin(callback.from_user.id) and SupportSettingsService.is_moderator(
                callback.from_user.id
            )
            details = {}
            try:
                t = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_user=True)
                if t and t.user:
                    details.update(
                        {
                            'target_telegram_id': t.user.telegram_id,
                            'target_username': t.user.username,
                        }
                    )
            except Exception:
                pass
            await TicketCRUD.add_support_audit(
                db,
                actor_user_id=db_user.id if db_user else None,
                actor_telegram_id=callback.from_user.id,
                is_moderator=is_mod,
                action='block_user_perm',
                ticket_id=ticket_id,
                target_user_id=None,
                details=details,
            )
        except Exception:
            pass
        await view_admin_ticket(callback, db_user, db, state)
    else:
        await callback.answer('❌ Ошибка', show_alert=True)


async def notify_user_about_ticket_reply(bot: Bot, ticket: Ticket, reply_text: str, db: AsyncSession):
    """Уведомить пользователя о новом ответе в тикете"""
    try:
        # Respect runtime toggle for user ticket notifications
        try:
            if not SupportSettingsService.get_user_ticket_notifications_enabled():
                return
        except Exception:
            pass
        from app.localization.texts import get_texts

        # Обеспечим наличие данных пользователя в объекте тикета
        ticket_with_user = ticket
        if not getattr(ticket_with_user, 'user', None):
            ticket_with_user = await TicketCRUD.get_ticket_by_id(db, ticket.id, load_user=True)

        user = getattr(ticket_with_user, 'user', None)
        if not user:
            logger.error('User not found for ticket #', ticket_id=ticket.id)
            return

        if not getattr(user, 'telegram_id', None):
            logger.warning(
                'Cannot notify ticket # user without telegram_id (username auth_type=)',
                ticket_id=ticket.id,
                getattr=getattr(user, 'username', None),
                getattr_2=getattr(user, 'auth_type', None),
            )
            return

        chat_id = int(user.telegram_id)
        texts = get_texts(user.language)

        # Формируем уведомление
        base_text = texts.t(
            'TICKET_REPLY_NOTIFICATION',
            '🎫 Получен ответ по тикету #{ticket_id}\n\n{reply_preview}\n\nНажмите кнопку ниже, чтобы перейти к тикету:',
        ).format(ticket_id=ticket.id, reply_preview=reply_text[:100] + '...' if len(reply_text) > 100 else reply_text)
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'), callback_data=f'view_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                        callback_data=f'close_ticket_notification_{ticket.id}',
                    )
                ],
            ]
        )

        # Если было фото в последнем ответе админа — отправим как фото
        last_message = await TicketMessageCRUD.get_last_message(db, ticket.id)
        if (
            last_message
            and last_message.has_media
            and last_message.media_type == 'photo'
            and last_message.is_from_admin
        ):
            caption = base_text
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=last_message.media_file_id,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except TelegramBadRequest as photo_error:
                logger.error(
                    'Не удалось отправить фото-уведомление пользователю для тикета',
                    chat_id=chat_id,
                    ticket_id=ticket.id,
                    photo_error=photo_error,
                )
            except Exception as e:
                logger.error('Не удалось отправить фото-уведомление', error=e)
        # Фоллбек: текстовое уведомление
        await bot.send_message(
            chat_id=chat_id,
            text=base_text,
            reply_markup=keyboard,
        )

        logger.info('Ticket # reply notification sent to user', ticket_id=ticket.id, chat_id=chat_id)

    except Exception as e:
        logger.error('Error notifying user about ticket reply', error=e)


def register_handlers(dp: Dispatcher):
    """Регистрация админских обработчиков тикетов"""

    # Просмотр тикетов
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets')
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets_scope_open')
    dp.callback_query.register(show_admin_tickets, F.data == 'admin_tickets_scope_closed')
    dp.callback_query.register(close_all_open_admin_tickets, F.data == 'admin_tickets_close_all_open')

    dp.callback_query.register(view_admin_ticket, F.data.startswith('admin_view_ticket_'))
    dp.callback_query.register(view_admin_ticket, F.data.startswith('admin_ticket_page_'))

    # Ответы на тикеты
    dp.callback_query.register(reply_to_admin_ticket, F.data.startswith('admin_reply_ticket_'))

    dp.message.register(handle_admin_ticket_reply, AdminTicketStates.waiting_for_reply)
    dp.message.register(handle_admin_block_duration_input, AdminTicketStates.waiting_for_block_duration)

    # Управление статусами: явная кнопка больше не используется (статус меняется автоматически)

    dp.callback_query.register(close_admin_ticket, F.data.startswith('admin_close_ticket_'))
    dp.callback_query.register(block_user_in_ticket, F.data.startswith('admin_block_user_ticket_'))
    dp.callback_query.register(unblock_user_in_ticket, F.data.startswith('admin_unblock_user_ticket_'))
    dp.callback_query.register(block_user_permanently, F.data.startswith('admin_block_user_perm_ticket_'))

    # Отмена операций
    dp.callback_query.register(cancel_admin_ticket_reply, F.data == 'cancel_admin_ticket_reply')

    # Пагинация админских тикетов
    dp.callback_query.register(show_admin_tickets, F.data.startswith('admin_tickets_page_'))

    # Управление компоновкой ответа — (отключено)

    # Вложения в тикете (админ)
    async def send_admin_ticket_attachments(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
        # permission gate for attachments view
        if not (settings.is_admin(callback.from_user.id) or SupportSettingsService.is_moderator(callback.from_user.id)):
            texts = get_texts(db_user.language)
            await callback.answer(texts.ACCESS_DENIED, show_alert=True)
            return
        texts = get_texts(db_user.language)
        try:
            ticket_id = int(callback.data.replace('admin_ticket_attachments_', ''))
        except ValueError:
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
            return
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)
        if not ticket:
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
        # После отправки добавим кнопку удалить под последним сообщением группы
        if last_group_message:
            try:
                kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('DELETE_MESSAGE', '🗑 Удалить'),
                                callback_data=f'admin_delete_message_{last_group_message.message_id}',
                            )
                        ]
                    ]
                )
                await callback.message.bot.send_message(
                    chat_id=callback.from_user.id,
                    text=texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'),
                    reply_markup=kb,
                )
            except Exception:
                await callback.answer(texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'))
        else:
            await callback.answer(texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'))

    dp.callback_query.register(send_admin_ticket_attachments, F.data.startswith('admin_ticket_attachments_'))

    async def admin_delete_message(callback: types.CallbackQuery):
        try:
            msg_id = int(callback.data.replace('admin_delete_message_', ''))
        except ValueError:
            await callback.answer('❌')
            return
        try:
            await callback.message.bot.delete_message(chat_id=callback.from_user.id, message_id=msg_id)
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer('✅')

    dp.callback_query.register(admin_delete_message, F.data.startswith('admin_delete_message_'))
