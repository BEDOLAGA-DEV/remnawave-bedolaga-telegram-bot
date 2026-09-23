"""После действия в групповом админ-чате карточка тикета остаётся групповой.

«Закрыть тикет», «Заблокировать» и «Разблокировать» перерисовывали карточку
клавиатурой личной админки: с «Ответить»/«Блок по времени» (FSM, в группе не
работают) и «⬅️ Назад» в меню админки. Оператор видел в группе «Блок по
времени», которая ничего не делает. В группе карточка перерисовывается той же
клавиатурой, что и уведомление: только надёжные кнопки, по новому состоянию.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType

import app.handlers.admin.tickets as admin_tickets
from app.config import Settings


FSM_OR_MENU = ('admin_reply_ticket_', 'admin_block_user_ticket_', 'admin_tickets')


def _ticket(*, is_closed: bool = False, blocked: bool = False) -> MagicMock:
    ticket = MagicMock()
    ticket.id = 5
    ticket.is_closed = is_closed
    ticket.is_user_reply_blocked = blocked
    ticket.user = MagicMock()
    ticket.user.id = 42
    ticket.user.telegram_id = 777
    ticket.user.username = 'author'
    return ticket


def _callback(data: str, chat_type: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user.id = 1
    callback.answer = AsyncMock()
    callback.message.chat.type = chat_type
    callback.message.chat.id = -100123 if chat_type != ChatType.PRIVATE else 1
    callback.message.answer = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _db_user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    return user


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.fixture(autouse=True)
def admin_and_crud(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    monkeypatch.setattr(Settings, 'is_cabinet_mode', lambda self: False)
    crud = admin_tickets.TicketCRUD
    monkeypatch.setattr(crud, 'close_ticket', AsyncMock(return_value=True))
    monkeypatch.setattr(crud, 'set_user_reply_block', AsyncMock(return_value=True))
    monkeypatch.setattr(crud, 'add_support_audit', AsyncMock())
    view = AsyncMock()
    monkeypatch.setattr(admin_tickets, 'view_admin_ticket', view)
    return view


def _serve(monkeypatch, ticket: MagicMock) -> None:
    monkeypatch.setattr(admin_tickets.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))


@pytest.mark.asyncio
async def test_close_in_group_redraws_the_group_card_without_fsm_buttons(monkeypatch):
    _serve(monkeypatch, _ticket(is_closed=True))
    callback = _callback('admin_close_ticket_5', ChatType.SUPERGROUP)

    await admin_tickets.close_admin_ticket(callback, _db_user(), AsyncMock())

    callback.message.edit_reply_markup.assert_awaited_once()
    callbacks = _callbacks(callback.message.edit_reply_markup.await_args.kwargs['reply_markup'])
    assert 'admin_block_user_perm_ticket_5' in callbacks, 'закрытый тикет: блок-контролы остаются'
    assert 'admin_close_ticket_5' not in callbacks
    assert not [c for c in callbacks if c.startswith(FSM_OR_MENU)], callbacks


@pytest.mark.asyncio
async def test_permanent_block_in_group_redraws_the_card_and_skips_the_private_view(monkeypatch, admin_and_crud):
    _serve(monkeypatch, _ticket(blocked=True))
    callback = _callback('admin_block_user_perm_ticket_5', ChatType.SUPERGROUP)

    await admin_tickets.block_user_permanently(callback, _db_user(), AsyncMock(), AsyncMock())

    admin_and_crud.assert_not_awaited()
    callbacks = _callbacks(callback.message.edit_reply_markup.await_args.kwargs['reply_markup'])
    assert 'admin_unblock_user_ticket_5' in callbacks
    assert 'admin_close_ticket_5' in callbacks
    assert not [c for c in callbacks if c.startswith(FSM_OR_MENU)], callbacks


@pytest.mark.asyncio
async def test_unblock_in_group_redraws_the_card(monkeypatch, admin_and_crud):
    _serve(monkeypatch, _ticket(blocked=False))
    callback = _callback('admin_unblock_user_ticket_5', ChatType.SUPERGROUP)

    await admin_tickets.unblock_user_in_ticket(callback, _db_user(), AsyncMock(), AsyncMock())

    admin_and_crud.assert_not_awaited()
    callbacks = _callbacks(callback.message.edit_reply_markup.await_args.kwargs['reply_markup'])
    assert 'admin_block_user_perm_ticket_5' in callbacks
    assert 'admin_unblock_user_ticket_5' not in callbacks
    assert not [c for c in callbacks if c.startswith(FSM_OR_MENU)], callbacks


@pytest.mark.asyncio
async def test_private_chat_keeps_the_admin_view(monkeypatch, admin_and_crud):
    """В личке админа поведение прежнее: полный экран тикета с FSM-кнопками."""
    _serve(monkeypatch, _ticket(blocked=True))
    callback = _callback('admin_block_user_perm_ticket_5', ChatType.PRIVATE)

    await admin_tickets.block_user_permanently(callback, _db_user(), AsyncMock(), AsyncMock())

    admin_and_crud.assert_awaited_once()
    callback.message.edit_reply_markup.assert_not_awaited()
