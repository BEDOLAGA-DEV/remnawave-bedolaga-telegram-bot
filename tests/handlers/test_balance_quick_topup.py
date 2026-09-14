"""Быстрое пополнение (кнопки с суммой): ответ на нажатие — до похода к провайдеру.

Отчёт об ошибке: `TelegramBadRequest: query is too old ...` из `handle_topup_amount_callback`.
Обработчик создавал платёж у провайдера (секунды), а `callback.answer()` звал в конце;
Telegram к тому моменту уже не ждал ответ. Собственный `except Exception` ловил это
раньше `@error_handler`, писал «Ошибка быстрого пополнения» и слал отчёт админам,
хотя платёж был создан и сообщение со ссылкой ушло.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from app.handlers.balance import main as balance_main


STALE = 'Bad Request: query is too old and response timeout expired or query ID is invalid'


def _callback(data: str = 'topup_amount|yookassa|10000') -> SimpleNamespace:
    message = MagicMock(spec=types.Message)
    message.answer = AsyncMock()
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=message,
        from_user=SimpleNamespace(id=1, username='probe'),
    )


def _state(platega_method: int = 0) -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'platega_method': platega_method})
    return state


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, language='ru')


def _stale() -> TelegramBadRequest:
    return TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id='1'), message=STALE)


@pytest.mark.asyncio
async def test_answers_the_tap_before_calling_the_provider(monkeypatch):
    callback = _callback()
    state = _state()
    answered_before_provider: list[int] = []

    async def route(message, db_user, amount_kopeks, st, method):
        answered_before_provider.append(callback.answer.await_count)
        assert message is callback.message
        assert (amount_kopeks, method) == (10000, 'yookassa')
        return True

    monkeypatch.setattr(balance_main, 'route_payment_by_method', route)

    await balance_main.handle_topup_amount_callback(callback, _user(), state)

    assert answered_before_provider == [1], 'часики снимаются до похода к провайдеру'
    assert callback.answer.await_count == 1
    state.update_data.assert_awaited_once_with(payment_method='yookassa')
    state.set_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_query_is_not_reported_as_topup_error(monkeypatch):
    """Устаревший запрос по дороге — предупреждение декоратора, а не отчёт об ошибке."""
    callback = _callback()

    async def route(*_args, **_kwargs):
        raise _stale()

    monkeypatch.setattr(balance_main, 'route_payment_by_method', route)
    error_log = MagicMock()
    monkeypatch.setattr(balance_main.logger, 'error', error_log)

    assert await balance_main.handle_topup_amount_callback(callback, _user(), _state()) is None

    error_log.assert_not_called()
    callback.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_is_reported_once_without_second_answer(monkeypatch):
    callback = _callback()

    async def route(*_args, **_kwargs):
        raise RuntimeError('provider down')

    monkeypatch.setattr(balance_main, 'route_payment_by_method', route)
    error_log = MagicMock()
    monkeypatch.setattr(balance_main.logger, 'error', error_log)

    await balance_main.handle_topup_amount_callback(callback, _user(), _state())

    error_log.assert_called_once()
    assert callback.answer.await_count == 1, 'на одно нажатие Telegram принимает один ответ'
    callback.message.answer.assert_awaited_once_with('❌ Ошибка обработки запроса')


@pytest.mark.asyncio
async def test_unknown_method_is_reported_with_a_message(monkeypatch):
    callback = _callback('topup_amount|nope|10000')

    async def route(*_args, **_kwargs):
        return False

    monkeypatch.setattr(balance_main, 'route_payment_by_method', route)

    await balance_main.handle_topup_amount_callback(callback, _user(), _state())

    assert callback.answer.await_count == 1
    callback.message.answer.assert_awaited_once_with('❌ Неизвестный способ оплаты')


@pytest.mark.asyncio
async def test_tribute_flow_owns_the_answer(monkeypatch):
    """Сценарии, которым передаётся сам callback, отвечают на нажатие сами — родитель не лезет."""
    from app.handlers.balance import tribute

    callback = _callback('topup_amount|tribute|10000')
    started = AsyncMock()
    monkeypatch.setattr(tribute, 'start_tribute_payment', started)

    await balance_main.handle_topup_amount_callback(callback, _user(), _state())

    started.assert_awaited_once()
    assert started.await_args.args[0] is callback
    callback.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_amount_alerts_immediately(monkeypatch):
    callback = _callback('topup_amount|yookassa|0')
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(balance_main, 'route_payment_by_method', route)

    await balance_main.handle_topup_amount_callback(callback, _user(), _state())

    callback.answer.assert_awaited_once_with('❌ Некорректная сумма', show_alert=True)
    route.assert_not_awaited()
