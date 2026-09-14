"""Поздний answerCallbackQuery — не ошибка, а штатный шум.

Telegram ждёт ответ на нажатие кнопки недолго; обработчик, который сначала ходит
к платёжному провайдеру, а потом зовёт callback.answer(), получает
«query is too old and response timeout expired or query ID is invalid». По всему
боту 16 обработчиков ловят это своим `except Exception`, пишут error и шлют отчёт
админам, хотя работа сделана. Чинить каждый — переписывать половину бота; вместо
этого исходящий запрос перехватывает middleware сессии: устаревший ответ на нажатие
превращается в предупреждение, всё остальное летит дальше как раньше.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

from app.utils.telegram_errors import is_stale_callback_query_error


STALE = 'Bad Request: query is too old and response timeout expired or query ID is invalid'


def _bad_request(method, message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=method, message=message)


def test_phrase_matcher_covers_both_telegram_wordings():
    assert is_stale_callback_query_error(_bad_request(AnswerCallbackQuery(callback_query_id='1'), STALE))
    assert is_stale_callback_query_error(
        _bad_request(AnswerCallbackQuery(callback_query_id='1'), 'Bad Request: query ID is invalid')
    )
    assert not is_stale_callback_query_error(
        _bad_request(AnswerCallbackQuery(callback_query_id='1'), 'Bad Request: MESSAGE_TOO_LONG')
    )
    assert not is_stale_callback_query_error(RuntimeError('query is too old'))


@pytest.mark.asyncio
async def test_stale_answer_becomes_warning_and_returns_true(monkeypatch):
    from app.middlewares import stale_callback_answer as mw

    fake_logger = MagicMock()
    monkeypatch.setattr(mw, 'logger', fake_logger)
    method = AnswerCallbackQuery(callback_query_id='42', text='готово')

    async def make_request(_bot, _method):
        raise _bad_request(method, STALE)

    result = await mw.StaleCallbackAnswerMiddleware()(make_request, MagicMock(), method)

    assert result is True
    fake_logger.warning.assert_called_once()
    fake_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_other_errors_on_answer_still_raise():
    from app.middlewares import stale_callback_answer as mw

    method = AnswerCallbackQuery(callback_query_id='42')

    async def make_request(_bot, _method):
        raise _bad_request(method, 'Bad Request: MESSAGE_TOO_LONG')

    with pytest.raises(TelegramBadRequest):
        await mw.StaleCallbackAnswerMiddleware()(make_request, MagicMock(), method)


@pytest.mark.asyncio
async def test_stale_phrases_on_other_methods_are_not_swallowed():
    """Middleware узкий: только ответ на нажатие. Редактирование сообщения — не его дело."""
    from app.middlewares import stale_callback_answer as mw

    method = EditMessageText(text='x', chat_id=1, message_id=1)

    async def make_request(_bot, _method):
        raise _bad_request(method, STALE)

    with pytest.raises(TelegramBadRequest):
        await mw.StaleCallbackAnswerMiddleware()(make_request, MagicMock(), method)


@pytest.mark.asyncio
async def test_successful_request_passes_through():
    from app.middlewares import stale_callback_answer as mw

    method = AnswerCallbackQuery(callback_query_id='42')

    async def make_request(_bot, _method):
        return 'ok'

    assert await mw.StaleCallbackAnswerMiddleware()(make_request, MagicMock(), method) == 'ok'


def test_bot_factory_installs_the_middleware_for_every_bot():
    """Все боты (основной, из кабинета, из фоновых задач) создаются фабрикой — защита общая."""
    from app.bot_factory import create_bot
    from app.middlewares.stale_callback_answer import StaleCallbackAnswerMiddleware

    bot = create_bot(token='123456:ABC-DEF_ghi')

    assert any(isinstance(m, StaleCallbackAnswerMiddleware) for m in bot.session.middleware)
