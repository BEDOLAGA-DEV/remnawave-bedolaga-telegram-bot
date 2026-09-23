"""Сервис админ-уведомлений в групповом режиме не кладёт мёртвые кнопки.

Фильтр чатов пропускает в группе только разрешённые callback'и. Любая
клавиатура, которую кто-то в будущем прикрепит к уведомлению в групповой
админ-чат, проходит через одну точку — ``_send_message``. Там неразрешённые
callback-кнопки выкидываются с записью в лог: лучше кнопки не будет, чем она
будет нарисована и ничего не делать. URL-кнопки и разрешённые действия остаются;
в личке админа клавиатура уходит как есть.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.keyboards.group_callbacks import strip_group_unsafe_buttons
from app.services.admin_notification_service import AdminNotificationService


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


URL = InlineKeyboardButton(text='Профиль', url='tg://user?id=1')
CLOSE = InlineKeyboardButton(text='Закрыть', callback_data='admin_close_ticket_5')
REPLY = InlineKeyboardButton(text='Ответить', callback_data='admin_reply_ticket_5')


def _labels(markup: InlineKeyboardMarkup | None) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data or b.url or '' for row in markup.inline_keyboard for b in row]


def test_strip_keeps_urls_and_allowed_actions_drops_the_rest():
    kept, dropped = strip_group_unsafe_buttons(_kb([URL], [CLOSE, REPLY]))

    assert _labels(kept) == ['tg://user?id=1', 'admin_close_ticket_5']
    assert dropped == ['admin_reply_ticket_5']


def test_strip_returns_none_when_nothing_survives():
    kept, dropped = strip_group_unsafe_buttons(_kb([REPLY]))

    assert kept is None
    assert dropped == ['admin_reply_ticket_5']
    assert strip_group_unsafe_buttons(None) == (None, [])


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    svc = AdminNotificationService(MagicMock())
    svc.enabled = True
    svc.bot.send_message = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_group_chat_gets_only_safe_buttons_on_both_send_paths(service, monkeypatch):
    service.chat_id = -100123
    rich = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.admin_notification_service.try_send_rich_admin_message', rich)

    assert await service._send_message('текст', reply_markup=_kb([URL], [CLOSE, REPLY])) is True

    assert _labels(rich.await_args.kwargs['reply_markup']) == ['tg://user?id=1', 'admin_close_ticket_5']
    assert _labels(service.bot.send_message.await_args.kwargs['reply_markup']) == [
        'tg://user?id=1',
        'admin_close_ticket_5',
    ]


@pytest.mark.asyncio
async def test_group_chat_sends_without_keyboard_when_all_buttons_are_dead(service, monkeypatch):
    service.chat_id = -100123
    monkeypatch.setattr(
        'app.services.admin_notification_service.try_send_rich_admin_message', AsyncMock(return_value=False)
    )

    await service._send_message('текст', reply_markup=_kb([REPLY]))

    assert 'reply_markup' not in service.bot.send_message.await_args.kwargs


@pytest.mark.asyncio
async def test_private_admin_chat_keeps_the_keyboard_untouched(service, monkeypatch):
    service.chat_id = 1
    monkeypatch.setattr(
        'app.services.admin_notification_service.try_send_rich_admin_message', AsyncMock(return_value=False)
    )

    await service._send_message('текст', reply_markup=_kb([URL], [CLOSE, REPLY]))

    assert _labels(service.bot.send_message.await_args.kwargs['reply_markup']) == [
        'tg://user?id=1',
        'admin_close_ticket_5',
        'admin_reply_ticket_5',
    ]
