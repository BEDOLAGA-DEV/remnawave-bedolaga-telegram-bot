"""Кнопки тикетов в групповом админ-чате доходят до обработчиков.

``ChatTypeFilterMiddleware`` появился, чтобы бот молчал в чужих группах, и
глушил ВСЕ нажатия вне лички. Позже карточку тикета для группового админ-чата
собрали из «надёжных» кнопок («Закрыть», «Заблокировать», «Разблокировать»,
«Удалить») — а фильтр по-прежнему выбрасывал их нажатия, и кнопки ничего не
делали (жалоба оператора на 4.14.0). Разрешаем в группе ровно те кнопки, что
карточка туда кладёт; FSM-кнопки и меню админки в группе по-прежнему не ходят.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from app.keyboards.group_callbacks import is_group_safe_callback
from app.keyboards.inline import get_ticket_notification_keyboard
from app.middlewares.chat_type_filter import ChatTypeFilterMiddleware


def _callback(data: str, chat_type: str) -> MagicMock:
    event = MagicMock(spec=CallbackQuery)
    event.data = data
    event.message = MagicMock()
    event.message.chat = MagicMock()
    event.message.chat.id = -100123 if chat_type != ChatType.PRIVATE else 123
    event.message.chat.type = chat_type
    return event


def _message(chat_type: str) -> MagicMock:
    event = MagicMock(spec=Message)
    event.chat = MagicMock()
    event.chat.id = -100123 if chat_type != ChatType.PRIVATE else 123
    event.chat.type = chat_type
    return event


async def _passes(event) -> bool:
    handler = AsyncMock(return_value='handled')
    result = await ChatTypeFilterMiddleware()(handler, event, {})
    return result == 'handled' and handler.await_count == 1


@pytest.mark.asyncio
async def test_private_chat_passes_everything():
    assert await _passes(_message(ChatType.PRIVATE))
    assert await _passes(_callback('admin_reply_ticket_5', ChatType.PRIVATE))
    assert await _passes(_callback('menu_main', ChatType.PRIVATE))


@pytest.mark.asyncio
async def test_group_messages_are_still_dropped():
    assert not await _passes(_message(ChatType.SUPERGROUP))
    assert not await _passes(_message(ChatType.GROUP))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'data',
    [
        'admin_close_ticket_5',
        'admin_block_user_perm_ticket_5',
        'admin_unblock_user_ticket_5',
        'admin_support_delete_msg',
        'admin_withdrawal_approve_5',
        'admin_withdrawal_reject_5',
        'admin_withdrawal_complete_5',
    ],
)
async def test_group_ticket_card_buttons_reach_handlers(data):
    assert await _passes(_callback(data, ChatType.SUPERGROUP))
    assert await _passes(_callback(data, ChatType.GROUP))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'data', ['admin_reply_ticket_5', 'admin_block_user_ticket_5', 'admin_tickets', 'menu_main', '']
)
async def test_fsm_and_menu_callbacks_stay_blocked_in_groups(data):
    """FSM-ввод в группе не работает (privacy mode), меню админки в общем чате не место."""
    assert not await _passes(_callback(data, ChatType.SUPERGROUP))


def test_every_button_of_the_group_card_is_allowed_in_groups():
    """Сторож: новая кнопка в групповой карточке без разрешения в фильтре — падение здесь, а не у оператора."""
    for is_closed in (False, True):
        for is_user_blocked in (False, True):
            keyboard = get_ticket_notification_keyboard(
                5,
                user_id=42,
                telegram_id=1,
                username='u',
                is_closed=is_closed,
                is_user_blocked=is_user_blocked,
                is_admin=False,
                fsm_enabled=False,
            )
            callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
            blocked = [data for data in callbacks if not is_group_safe_callback(data)]
            assert not blocked, f'кнопки групповой карточки не пройдут фильтр: {blocked}'


def test_fsm_buttons_are_not_group_safe():
    assert not is_group_safe_callback('admin_reply_ticket_5')
    assert not is_group_safe_callback('admin_block_user_ticket_5')
    assert not is_group_safe_callback(None)
