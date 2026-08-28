"""Карточка тикета открывается на последней странице.

Пользователь приходит сюда по кнопке «Посмотреть тикет» из уведомления об
ответе поддержки — то есть за свежим сообщением. Открытие на первой странице
заставляло долистывать длинную переписку до конца, чтобы увидеть то, ради чего
уведомление и пришло. Явный номер страницы (кнопки ⬅️/➡️) по-прежнему главнее.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.handlers.tickets as handler


def _ticket(message_count: int, body: str) -> SimpleNamespace:
    created = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=17,
        user_id=1,
        title='Не подключается VPN',
        status='open',
        status_emoji='🟢',
        is_closed=False,
        created_at=created,
        messages=[
            SimpleNamespace(
                created_at=created,
                message_text=f'{body} #{index}',
                is_user_message=index % 2 == 0,
                has_media=False,
                media_type=None,
            )
            for index in range(message_count)
        ],
    )


async def _render(callback_data: str, ticket: SimpleNamespace) -> str:
    """Возвращает текст страницы, которую хендлер отдал в чат."""
    callback = SimpleNamespace(data=callback_data, message=SimpleNamespace(), answer=AsyncMock())
    db_user = SimpleNamespace(id=1, language='ru')

    with (
        patch.object(handler.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket)),
        patch.object(handler, 'safe_edit_or_resend', AsyncMock()) as send_mock,
    ):
        await handler.view_ticket(callback, db_user, AsyncMock())

    return send_mock.await_args.args[1]


@pytest.mark.asyncio
async def test_opens_on_the_last_page_when_no_page_requested() -> None:
    ticket = _ticket(message_count=40, body='очень длинное сообщение ' * 40)

    shown = await _render('view_ticket_17', ticket)

    assert ticket.messages[-1].message_text in shown, 'открылась не последняя страница'
    assert ticket.messages[0].message_text not in shown, 'переписка уместилась на одну страницу — тест бессмысленен'


@pytest.mark.asyncio
async def test_explicit_page_from_pagination_button_wins() -> None:
    ticket = _ticket(message_count=40, body='очень длинное сообщение ' * 40)

    shown = await _render('ticket_view_page_17_1', ticket)

    assert ticket.messages[0].message_text in shown
    assert ticket.messages[-1].message_text not in shown
