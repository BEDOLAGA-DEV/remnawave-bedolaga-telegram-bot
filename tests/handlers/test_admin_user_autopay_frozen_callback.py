"""Админка → автоплатёж: после сохранения экран перерисовывается без мутации frozen CallbackQuery."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

import app.handlers.admin.users as mod


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _real_callback(data: str) -> CallbackQuery:
    tg_user = TgUser(id=1, is_bot=False, first_name='Admin')
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type='private'),
        from_user=tg_user,
        text='Автоплатёж',
    )
    return CallbackQuery(
        id='query_1',
        from_user=tg_user,
        chat_instance='chat_instance',
        data=data,
        message=message,
    ).as_(MagicMock())


@pytest.mark.parametrize(
    ('handler_name', 'data'),
    [
        ('toggle_admin_user_autopay', 'admin_user_autopay_toggle_11'),
        ('set_admin_user_autopay_days', 'admin_user_autopay_days_set_11_3'),
        ('set_admin_user_autopay_period', 'admin_user_autopay_period_set_11_0'),
    ],
)
async def test_autopay_handlers_rerender_without_mutating_frozen_callback(handler_name, data, monkeypatch) -> None:
    subscription = SimpleNamespace(id=8, autopay_enabled=True)
    monkeypatch.setattr(mod, '_load_admin_subscription', AsyncMock(return_value=subscription))
    show = AsyncMock()
    monkeypatch.setattr(mod, 'show_admin_user_autopay', show)
    callback = _real_callback(data)
    update = AsyncMock()

    with (
        patch('app.database.crud.subscription.update_subscription_autopay', update),
        patch.object(CallbackQuery, 'answer', AsyncMock()),
    ):
        await _unwrap(getattr(mod, handler_name))(callback, SimpleNamespace(language='ru', id=1), MagicMock())

    update.assert_awaited_once()
    assert callback.data == data
    show.assert_awaited_once()
    assert show.await_args.args[0].data == 'admin_user_autopay_11'
