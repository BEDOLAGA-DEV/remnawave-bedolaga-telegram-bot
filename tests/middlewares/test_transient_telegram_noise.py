"""Обрывы длинного getUpdates не уходят в админ-чат.

Лог владельца 21 сентября: площадка роняет длинный запрос getUpdates несколько раз в час,
aiogram повторяет сам, пользователь ничего не замечает. Но сборщик system_error_events
перехватывает запись на уровне structlog — мимо GlobalErrorMiddleware — и каждый такой
обрыв приезжает отчётом «LogError (no traceback available)».

Фильтр устроен как _is_transient_remnawave_error: событие по-прежнему пишется в базу,
меняется только статус доставки. Посторонние ошибки он глушить не должен — это и
проверяет третий случай.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from aiogram.exceptions import TelegramNetworkError

from app.logging_handler import _is_transient_telegram_error


def _caught(error: BaseException) -> tuple:
    try:
        raise error
    except BaseException as caught:
        return (type(caught), caught, caught.__traceback__)


def test_dropped_get_updates_is_transient():
    event = {
        'event': 'Failed to fetch updates - TelegramNetworkError: HTTP Client says - Request timeout error',
        'logger': 'aiogram.dispatcher',
        'level': 'error',
        'exc_info': _caught(TelegramNetworkError(method=MagicMock(), message='Request timeout error')),
    }

    assert _is_transient_telegram_error(event) is True


def test_transient_error_is_found_through_cause_chain():
    network = TelegramNetworkError(method=MagicMock(), message='Request timeout error')
    wrapper = RuntimeError('polling loop failed')
    wrapper.__cause__ = network

    event = {
        'event': 'Failed to fetch updates',
        'logger': 'aiogram.dispatcher',
        'level': 'error',
        'exc_info': _caught(wrapper),
    }

    assert _is_transient_telegram_error(event) is True


def test_unrelated_error_is_not_suppressed():
    event = {
        'event': 'Ошибка обработки платежа',
        'logger': 'app.services.payment',
        'level': 'error',
        'exc_info': _caught(ValueError('unexpected payload')),
    }

    assert _is_transient_telegram_error(event) is False
