"""Centralized structlog configuration.

Configures structlog with ProcessorFormatter so that both structlog.get_logger(__name__)
and logging.getLogger() calls produce identically formatted output.

Usage::

    from app.logging_config import setup_logging

    file_formatter, console_formatter, telegram_notifier = setup_logging()
    # Apply formatters to handlers...
    # Later: telegram_notifier.set_bot(bot)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config import settings


def _create_timezone_timestamper() -> structlog.types.Processor:
    """Create a timestamper processor that uses the configured timezone."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo('UTC')

    from datetime import datetime

    def timestamper(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        dt = datetime.now(tz=tz)
        event_dict['timestamp'] = dt.strftime('%Y-%m-%d %H:%M:%S')
        return event_dict

    return timestamper


def _clean_logger_name(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip __main__ logger name — it's redundant noise in startup logs."""
    if event_dict.get('logger') == '__main__':
        del event_dict['logger']
    return event_dict


def _prefix_logger_name(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Move logger name before event text: [module.name] event text."""
    logger_name = event_dict.pop('logger', None)
    if logger_name:
        event_dict['event'] = f'[{logger_name}] {event_dict.get("event", "")}'
    return event_dict


def _auto_capture_exc_info(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Auto-populate event_dict['exc_info'] so tracebacks render in files/console.

    Without this, callers must pass ``exc_info=True`` at every ``logger.error``
    site. Instead, we try:
      1. exc_info=True → replace with sys.exc_info() (standard structlog behaviour)
      2. no exc_info but we're inside an active except block → use sys.exc_info()
      3. error/exc/exception/e/err kwarg is a BaseException with __traceback__ →
         synthesize an exc_info tuple from it

    Result: ``logger.error('msg', error=e)`` inside any ``except`` block now
    renders the full traceback to files, console, and Telegram automatically.
    """
    exc_info = event_dict.get('exc_info')
    if exc_info is True:
        current = sys.exc_info()
        if current[1] is not None:
            event_dict['exc_info'] = current
        return event_dict

    if exc_info:
        return event_dict

    # Only auto-capture from sys.exc_info() for error/critical levels.
    # For warning/info inside except blocks, callers must pass exc_info=True explicitly.
    if method_name in ('error', 'critical', 'exception'):
        current = sys.exc_info()
        if current[1] is not None:
            event_dict['exc_info'] = current
            return event_dict

    for key in ('error', 'exc', 'exception', 'e', 'err'):
        candidate = event_dict.get(key)
        if isinstance(candidate, BaseException) and candidate.__traceback__ is not None:
            event_dict['exc_info'] = (type(candidate), candidate, candidate.__traceback__)
            return event_dict

    return event_dict


def setup_logging() -> tuple[logging.Formatter, logging.Formatter, Any]:
    """Configure structlog and return formatters + notifier.

    Returns:
        (file_formatter, console_formatter, telegram_notifier)

        - file_formatter: ProcessorFormatter without ANSI colors (for file handlers)
        - console_formatter: ProcessorFormatter with auto-detected colors (for console)
        - telegram_notifier: TelegramNotifierProcessor (call .set_bot(bot) later)
    """
    from app.logging_handler import TelegramNotifierProcessor

    telegram_notifier = TelegramNotifierProcessor()
    timestamper = _create_timezone_timestamper()

    # Shared processors applied to both structlog and stdlib log entries.
    # Order matters: each processor enriches event_dict for the next one.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _clean_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        # Auto-capture traceback from sys.exc_info()/error-kwarg BEFORE any
        # consumer looks at event_dict. Runs for ALL log levels so files,
        # console, and Telegram all see the same traceback without requiring
        # every caller to pass exc_info=True.
        _auto_capture_exc_info,
        # TelegramNotifierProcessor MUST run while exc_info is still a raw
        # tuple so it can extract the traceback for Telegram notifications.
        # ConsoleRenderer handles exc_info formatting downstream (with Rich
        # tracebacks on console, plain text in files).
        telegram_notifier,
    ]

    # Configure structlog for structlog-originated logs.
    # wrap_for_formatter packages event_dict into the stdlib LogRecord
    # so ProcessorFormatter can extract and render it.
    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL, logging.INFO),
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        # NOTE: cache is safe because LOG_LEVEL is set once at startup.
        # If dynamic level changes are ever added, switch to False.
        cache_logger_on_first_use=True,
    )

    # File formatter: no ANSI colors, plain tracebacks (safe for log files)
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _prefix_logger_name,
            structlog.dev.ConsoleRenderer(
                colors=False,
                pad_event_to=0,
                pad_level=False,
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ],
    )

    # Console formatter: colors controlled by LOG_COLORS env var (default: true).
    # Rich tracebacks with conservative limits to avoid 5000-line dumps.
    use_colors = settings.LOG_COLORS
    console_renderer_kwargs: dict[str, Any] = {
        'colors': use_colors,
        'pad_event_to': 0,
        'pad_level': False,
    }
    if use_colors:
        console_renderer_kwargs['exception_formatter'] = structlog.dev.RichTracebackFormatter(
            show_locals=False,
            max_frames=20,
            extra_lines=1,
            width=120,
            suppress=['aiogram', 'aiohttp'],
        )
    else:
        console_renderer_kwargs['exception_formatter'] = structlog.dev.plain_traceback

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _prefix_logger_name,
            structlog.dev.ConsoleRenderer(**console_renderer_kwargs),
        ],
    )

    _configure_noisy_loggers()

    return file_formatter, console_formatter, telegram_notifier


def _configure_noisy_loggers() -> None:
    """Suppress noisy third-party loggers."""
    for name, level in {
        'aiohttp.access': logging.ERROR,
        'aiohttp.client': logging.WARNING,
        'aiohttp.internal': logging.WARNING,
        'app.external.remnawave_api': logging.WARNING,
        'aiogram': logging.WARNING,
        'uvicorn.access': logging.ERROR,
        'uvicorn.error': logging.WARNING,
        'uvicorn.protocols.websockets.websockets_impl': logging.WARNING,
        'websockets.server': logging.WARNING,
        'websockets': logging.WARNING,
    }.items():
        logging.getLogger(name).setLevel(level)
