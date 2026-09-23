"""Обрывы длинного getUpdates не уходят в админ-чат.

Лог владельца 21 сентября: площадка роняет длинный запрос getUpdates несколько
раз в час, aiogram повторяет сам, пользователь ничего не замечает. Но сборщик
system_error_events перехватывает запись на уровне structlog — мимо
GlobalErrorMiddleware — и каждый обрыв приезжает отчётом
«LogError (no traceback available)».

Форма записи — как у aiogram 3.x (``dispatcher.py``): исключение НЕ в exc_info,
а позиционным аргументом ``%s``::

    loggers.dispatcher.error('Failed to fetch updates - %s: %s', type(e).__name__, e)

Тесты собирают событие ровно так — stdlib-запись, прошедшая foreign_pre_chain.
Фильтр, который ищет исключение только в exc_info, эту запись не видит.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import structlog
from aiogram.exceptions import TelegramNetworkError
from aiohttp import ClientOSError

from app.logging_handler import STATUS_SUPPRESSED, TelegramNotifierProcessor, _is_transient_telegram_error


def _aiogram_record(error: BaseException, *, logger: str = 'aiogram.dispatcher') -> dict:
    """Событие, каким его получает процессор: stdlib-запись через foreign_pre_chain."""
    record = logging.LogRecord(
        logger, logging.ERROR, __file__, 1, 'Failed to fetch updates - %s: %s', (type(error).__name__, error), None
    )
    event_dict = {
        'event': record.msg,
        'logger': record.name,
        'level': 'error',
        '_record': record,
        '_from_structlog': False,
        'positional_args': record.args,
    }
    return structlog.stdlib.PositionalArgumentsFormatter()(None, 'error', event_dict)


def _network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=MagicMock(), message='HTTP Client says - Request timeout error')


def test_dropped_get_updates_is_transient():
    event = _aiogram_record(_network_error())

    assert event['event'].startswith('Failed to fetch updates - TelegramNetworkError')
    assert _is_transient_telegram_error(event) is True


def test_transient_error_is_found_through_cause_chain():
    wrapper = RuntimeError('polling loop failed')
    wrapper.__cause__ = _network_error()
    event = {'event': 'Failed to fetch updates', 'logger': 'aiogram.dispatcher', 'level': 'error', 'error': wrapper}

    assert _is_transient_telegram_error(event) is True


def test_raw_socket_drop_from_aiogram_is_transient():
    """aiohttp иногда отдаёт обрыв без обёртки aiogram."""
    assert _is_transient_telegram_error(_aiogram_record(ClientOSError(104, 'Connection reset by peer'))) is True


def test_socket_drop_from_another_client_is_not_telegram_noise():
    """ClientOSError бывает у любого aiohttp-клиента — платёжки, панель; их не глушим."""
    event = _aiogram_record(ClientOSError(104, 'Connection reset by peer'), logger='app.services.payment.cispay')

    assert _is_transient_telegram_error(event) is False


def test_unrelated_error_is_not_suppressed():
    event = _aiogram_record(ValueError('unexpected payload'), logger='app.services.payment')

    assert _is_transient_telegram_error(event) is False


@pytest.mark.parametrize(
    ('error', 'expected_sends'),
    [(_network_error(), 0), (ValueError('boom'), 1)],
    ids=['transient', 'real'],
)
def test_processor_suppresses_only_the_transport_noise(monkeypatch, error, expected_sends):
    """Сквозной путь: событие пишется в базу, но в чат уходит только настоящая ошибка."""
    statuses: list[str] = []
    monkeypatch.setattr('app.logging_handler._record_error_event', lambda *_: 'uid')
    monkeypatch.setattr('app.logging_handler._mark_error_event', lambda _uid, status: statuses.append(status))
    sends: list[dict] = []
    monkeypatch.setattr(TelegramNotifierProcessor, '_schedule_send', lambda self, bot, ev, uid: sends.append(ev))

    processor = TelegramNotifierProcessor()
    processor.set_bot(MagicMock())
    processor(None, 'error', _aiogram_record(error))

    assert len(sends) == expected_sends
    if not expected_sends:
        assert statuses == [STATUS_SUPPRESSED]
