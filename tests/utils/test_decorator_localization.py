"""Authorization and error responses keep the requesting user's language."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types

from app.database.models import User
from app.localization.texts import get_texts
from app.utils import decorators


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('positional', [False, True])
async def test_denied_admin_action_uses_account_language_without_calling_handler(monkeypatch, language, positional):
    monkeypatch.setattr(decorators, 'settings', SimpleNamespace(is_admin=lambda _: False))
    event = MagicMock(spec=types.CallbackQuery)
    event.from_user = types.User(id=1, is_bot=False, first_name='Example', language_code='ru')
    event.answer = AsyncMock()
    handler = AsyncMock()
    user = User(language=language)
    wrapped = decorators.admin_required(handler)
    if positional:
        await wrapped(event, user)
    else:
        await wrapped(event, db_user=user)
    handler.assert_not_awaited()
    event.answer.assert_awaited_once_with(get_texts(language).ACCESS_DENIED, show_alert=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('account_language', [None, 'fa'])
async def test_error_message_uses_positional_account_or_telegram_language(account_language):
    event = MagicMock(spec=types.CallbackQuery)
    event.from_user = types.User(id=1, is_bot=False, first_name='Example', language_code='en')
    event.answer = AsyncMock()
    args = (event, User(language=account_language)) if account_language else (event,)
    await decorators._send_error_message(args, {}, ValueError('internal'), 'example')
    event.answer.assert_awaited_once_with(get_texts(account_language or 'en').ERROR, show_alert=True)
