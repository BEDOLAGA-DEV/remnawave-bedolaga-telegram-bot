"""Сохранение автоплатежа перерисовывает карточку без изменения frozen callback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, User

from app.config import settings
from app.database.crud import subscription as crud
from app.handlers.admin import users


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('handler_name', 'data', 'expected_options'),
    [
        ('toggle_admin_user_autopay', 'admin_user_autopay_toggle_42_s7', {}),
        ('set_admin_user_autopay_days', 'admin_user_autopay_days_42_s7_3', {'days_before': 3}),
        ('set_admin_user_autopay_period', 'admin_user_autopay_period_42_s7_30', {'period_days': 30}),
        ('set_admin_user_autopay_period', 'admin_user_autopay_period_42_s7_0', {'period_days': None}),
    ],
)
async def test_autopay_rerenders_without_mutation(monkeypatch, multi, handler_name, data, expected_options):
    bot = Bot('123456:test-token')
    callback = CallbackQuery(
        id='test', from_user=User(id=1, is_bot=False, first_name='Admin'), chat_instance='test', data=data
    ).as_(bot)
    subscription = SimpleNamespace(autopay_enabled=True)
    admin, db = object(), AsyncMock()
    save, render = AsyncMock(), AsyncMock()
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(CallbackQuery, 'answer', AsyncMock())
    monkeypatch.setattr(users, '_load_admin_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(users, '_admin_autopay_available_periods', lambda _: [30])
    monkeypatch.setattr(users, 'show_admin_user_autopay', render)
    monkeypatch.setattr(crud, 'update_subscription_autopay', save)
    handler = getattr(users, handler_name)
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    try:
        await handler(callback, admin, db)
        save.assert_awaited_once()
        assert save.await_args.args == (db, subscription, handler_name != 'toggle_admin_user_autopay')
        assert save.await_args.kwargs == expected_options
        render.assert_awaited_once()
        copied = render.await_args.args[0]
        assert copied is not callback
        assert copied.bot is bot
        assert copied.data == 'admin_user_autopay_42' + ('_s7' if multi else '')
        assert callback.data == data
    finally:
        await bot.session.close()
