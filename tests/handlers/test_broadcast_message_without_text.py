"""Шаг ввода текста рассылки не падает на сообщении без текста (#3169).

Из мобильного Telegram на этом шаге иногда приходит апдейт без message.text,
и len(None) ронял обработчик с TypeError.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.handlers.admin.messages import process_broadcast_message
from app.states import AdminStates


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _message(text):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


def _state():
    state = MagicMock()
    state.update_data = AsyncMock()
    return state


async def test_message_without_text_is_rejected() -> None:
    message = _message(None)
    state = _state()

    await _unwrap(process_broadcast_message)(message, db_user=MagicMock(language='ru'), state=state, db=MagicMock())

    state.update_data.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert 'текст' in message.answer.await_args.args[0]


async def test_text_message_is_saved() -> None:
    message = _message('Привет')
    state = _state()

    await _unwrap(process_broadcast_message)(message, db_user=MagicMock(language='ru'), state=state, db=MagicMock())

    state.update_data.assert_awaited_once_with(broadcast_message='Привет')


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
@pytest.mark.parametrize(
    'content',
    [
        {'photo': [{'file_id': 'test', 'file_unique_id': 'test', 'width': 1, 'height': 1}], 'caption': 'Caption'},
        {'document': {'file_id': 'test', 'file_unique_id': 'test'}, 'caption': 'Caption'},
        {
            'sticker': {
                'file_id': 'test',
                'file_unique_id': 'test',
                'type': 'regular',
                'width': 1,
                'height': 1,
                'is_animated': False,
                'is_video': False,
            }
        },
        {'video': {'file_id': 'test', 'file_unique_id': 'test', 'width': 1, 'height': 1, 'duration': 1}},
        {},
    ],
)
async def test_non_text_preserves_fsm_and_allows_retry(monkeypatch, language, content):
    reply = AsyncMock()
    monkeypatch.setattr(Message, 'answer', reply)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=42, chat_id=1, user_id=1))
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await state.update_data(broadcast_target='all')
    handler = _unwrap(process_broadcast_message)
    user = SimpleNamespace(language=language)
    base = {'message_id': 1, 'date': 0, 'chat': {'id': 1, 'type': 'private'}}
    await handler(Message.model_validate({**base, **content}), user, state, MagicMock())
    assert await state.get_state() == AdminStates.waiting_for_broadcast_message.state
    assert await state.get_data() == {'broadcast_target': 'all'}
    locale = json.loads((Path('app/localization/locales') / f'{language}.json').read_text())
    reply.assert_awaited_once_with(locale['ADMIN_BROADCAST_TEXT_REQUIRED'])

    await handler(Message.model_validate({**base, 'text': 'Hello'}), user, state, MagicMock())
    assert await state.get_data() == {'broadcast_target': 'all', 'broadcast_message': 'Hello'}
    assert reply.await_count == 2
    await state.storage.close()


@pytest.mark.parametrize('length', [4000, 4001])
async def test_text_length_boundary_is_preserved(length):
    message = _message('a' * length)
    state = _state()
    await _unwrap(process_broadcast_message)(message, MagicMock(language='ru'), state, MagicMock())
    if length == 4000:
        state.update_data.assert_awaited_once_with(broadcast_message='a' * length)
    else:
        state.update_data.assert_not_awaited()
        assert '4000' in message.answer.await_args.args[0]
