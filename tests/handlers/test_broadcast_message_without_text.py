"""Шаг ввода текста рассылки не падает на сообщении без текста (#3169).

Из мобильного Telegram на этом шаге иногда приходит апдейт без message.text,
и len(None) ронял обработчик с TypeError.
"""

from unittest.mock import AsyncMock, MagicMock

from app.handlers.admin.messages import process_broadcast_message


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

    await _unwrap(process_broadcast_message)(message, db_user=MagicMock(), state=state, db=MagicMock())

    state.update_data.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert 'текст' in message.answer.await_args.args[0]


async def test_text_message_is_saved() -> None:
    message = _message('Привет')
    state = _state()

    await _unwrap(process_broadcast_message)(message, db_user=MagicMock(language='ru'), state=state, db=MagicMock())

    state.update_data.assert_awaited_once_with(broadcast_message='Привет')
