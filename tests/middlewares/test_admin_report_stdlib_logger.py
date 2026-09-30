"""Ошибка из stdlib-логгера доезжает до админ-чата со своим текстом и traceback.

Лог владельца 2026-09-24: в админ-чат пришёл отчёт «Тип: LogError», в файле пустой
Message и «(no traceback available)», без строки «Контекст». В консоли в ту же
секунду — полноценная ошибка ``aiogram.dispatcher`` с traceback.

Причина — общий ``event_dict``. Для stdlib-записи (aiogram, APScheduler, SQLAlchemy —
всё, что пишет через ``logging``, а не structlog) ``ProcessorFormatter`` гонит ОДИН
словарь сначала через ``foreign_pre_chain`` (там процессор ставит отправку в очередь),
потом через ``processors``: ``_prefix_logger_name`` забирает ``logger``,
``ConsoleRenderer`` забирает ``event``, ``level`` и ``exc_info``. Задача отправки
стартует позже и видит пустой словарь: тип ``Log`` + уровень по умолчанию, пустой
текст, нет traceback. Structlog-записи не страдают — для них formatter работает с
копией ``record.msg``.

Поэтому тест идёт сквозным путём через настоящий ``setup_logging()``, а не собирает
``event_dict`` руками: в собранном вручную словаре рендерер ничего не вычищает.
"""

from __future__ import annotations

import asyncio
import io
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

import app.middlewares.global_error as ge
from app import logging_config


@pytest.fixture
def stdlib_logger(monkeypatch):
    """Stdlib-логгер с консольным formatter'ом бота и подключённым процессором."""
    saved_config = structlog.get_config()
    monkeypatch.setattr(logging_config, '_configure_noisy_loggers', lambda: None)
    monkeypatch.setattr('app.logging_handler._record_error_event', lambda *_: None)
    _file_formatter, console_formatter, notifier = logging_config.setup_logging()
    notifier.set_bot(MagicMock())

    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(console_formatter)
    logger = logging.getLogger('apscheduler.executors.default')
    saved = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield logger
    finally:
        logger.handlers, level, logger.propagate = saved
        logger.setLevel(level)
        structlog.configure(**saved_config)


@pytest.mark.asyncio
async def test_stdlib_error_report_keeps_type_message_logger_and_traceback(monkeypatch, stdlib_logger):
    sent = AsyncMock(return_value='sent')
    monkeypatch.setattr(ge, 'send_error_to_admin_chat', sent)

    try:
        raise ValueError('job exploded')
    except ValueError:
        stdlib_logger.exception('Job "sync_nodes" raised an exception')
    await asyncio.sleep(0)  # дать запланированной задаче отправки отработать

    sent.assert_awaited_once()
    _bot, error, context = sent.await_args.args
    tb_override = sent.await_args.kwargs['tb_override']
    assert type(error).__name__ == 'ValueError'
    assert str(error) == 'Job "sync_nodes" raised an exception'
    assert context == 'Logger: apscheduler.executors.default'
    assert tb_override is not None
    assert 'ValueError: job exploded' in tb_override
