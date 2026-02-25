"""Structlog processor for sending ERROR/CRITICAL logs to admin Telegram chat.

Intercepts all log events at ERROR level and above, deduplicates them,
and schedules async delivery to the admin Telegram chat via the existing
``send_error_to_admin_chat`` infrastructure.

Deduplication:
- Events already processed by GlobalErrorMiddleware / @error_handler
  carry ``_admin_notified=True`` and are skipped.
- Recent message hashes are kept in a TTL cache to prevent duplicate
  notifications for the same error within a short window.

Async bridge:
- structlog processors are synchronous.  We use
  ``asyncio.get_running_loop().call_soon_threadsafe()`` to schedule an
  asyncio.Task from any thread.

Deferred init:
- The Bot instance is created later in main.py.  ``set_bot()`` injects
  it after creation.  Until then, events are silently passed through.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
import traceback
from typing import Any, Final

from aiogram import Bot

from app.config import settings
from app.localization.texts import get_texts

# Constants
RECENT_HASHES_MAX_SIZE: Final[int] = 256
RECENT_HASH_TTL_SECONDS: Final[float] = 300.0  # 5 min — matches cooldown in global_error

# Logger name prefixes we never want notifications from
# (noisy transport-level loggers).
IGNORED_LOGGER_PREFIXES: Final[tuple[str, ...]] = (
    'aiohttp.access',
    'aiohttp.client',
    'aiohttp.internal',
    'uvicorn.access',
    'uvicorn.error',
    'uvicorn.protocols',
    'websockets',
    'asyncio',
    # Payment modules — isolated to payments.log, must not leak to Telegram
    'app.payments',
    'app.services.payment',
    'app.services.yookassa_service',
    'app.services.tribute_service',
    'app.services.mulenpay_service',
    'app.services.cloudpayments_service',
    'app.services.platega_service',
    'app.services.pal24_service',
    'app.services.wata_service',
    'app.services.kassa_ai_service',
    'app.services.freekassa_service',
    'app.external.cryptobot',
    'app.external.heleket',
    'app.external.tribute',
    'app.external.yookassa_webhook',
    'app.external.wata_webhook',
    'app.external.heleket_webhook',
    'app.external.pal24_client',
    'app.external.telegram_stars',
    'app.webserver.payments',
)


class TelegramNotifierProcessor:
    """Structlog processor that sends ERROR/CRITICAL events to the admin Telegram chat.

    Uses the existing throttling and buffering from
    ``app.middlewares.global_error.send_error_to_admin_chat``.

    Usage::

        notifier = TelegramNotifierProcessor()
        # Add to shared_processors list in logging_config.py
        # Later, when Bot is created:
        notifier.set_bot(bot)
    """

    def __init__(self) -> None:
        self._bot: Bot | None = None
        # LRU-like cache of recent message hashes: hash -> timestamp
        self._recent_hashes: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_bot(self, bot: Bot) -> None:
        """Inject the Bot instance for sending messages.

        Called from main.py after the bot is created.
        """
        self._bot = bot

    # ------------------------------------------------------------------
    # Processor interface
    # ------------------------------------------------------------------

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        """Process a log event. Passthrough — always returns event_dict."""

        # 1. Only handle error-level events
        level = event_dict.get('level', '')
        if level not in ('error', 'critical', 'exception'):
            return event_dict

        # 2. Already sent via GlobalErrorMiddleware / @error_handler
        if event_dict.get('_admin_notified'):
            return event_dict

        # 3. Filter noisy loggers
        logger_name = event_dict.get('logger', '')
        if any(logger_name.startswith(prefix) for prefix in IGNORED_LOGGER_PREFIXES):
            return event_dict

        # 4. Resolve exc_info=True to actual tuple while still in except block.
        # logger.exception() sets exc_info=True (bool); we need the tuple for
        # traceback extraction. sys.exc_info() works because the processor runs
        # synchronously inside the except clause.
        exc_info = event_dict.get('exc_info')
        if exc_info is True:
            event_dict['exc_info'] = sys.exc_info()

        # 5. Bot not initialized yet — skip
        bot = self._bot
        if bot is None:
            return event_dict

        # 6. Deduplication via hash
        msg_hash = self._compute_hash(event_dict)
        now = time.monotonic()

        with self._lock:
            self._evict_stale(now)
            if msg_hash in self._recent_hashes:
                return event_dict
            self._recent_hashes[msg_hash] = now

        # 7. Schedule async send
        self._schedule_send(bot, event_dict)

        return event_dict

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_hash(event_dict: dict[str, Any]) -> str:
        """Compute a short hash for deduplication.

        Hashes logger name + event message + exception type (if present).
        """
        logger_name = event_dict.get('logger', '')
        event_msg = event_dict.get('event', '')

        # Include exception type for better dedup granularity
        exc_type = ''
        exc_info = event_dict.get('exc_info')
        if exc_info and isinstance(exc_info, tuple) and exc_info[0] is not None:
            exc_type = exc_info[0].__name__

        raw = f'{logger_name}:{event_msg}:{exc_type}'
        return hashlib.md5(raw.encode('utf-8', errors='replace')).hexdigest()

    def _evict_stale(self, now: float) -> None:
        """Remove expired entries from the hash cache. Must be called under self._lock."""
        if not self._recent_hashes:
            return
        stale_keys = [k for k, ts in self._recent_hashes.items() if (now - ts) > RECENT_HASH_TTL_SECONDS]
        for k in stale_keys:
            self._recent_hashes.pop(k, None)
        # Force eviction on overflow — remove oldest entries
        if len(self._recent_hashes) > RECENT_HASHES_MAX_SIZE:
            sorted_keys = sorted(self._recent_hashes, key=lambda k: self._recent_hashes[k])
            for k in sorted_keys[: len(self._recent_hashes) - RECENT_HASHES_MAX_SIZE]:
                self._recent_hashes.pop(k, None)

    def _schedule_send(self, bot: Bot, event_dict: dict[str, Any]) -> None:
        """Schedule async delivery via event loop.

        Works from any thread:
        - From async context: creates Task directly.
        - From other threads: uses call_soon_threadsafe.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — silently skip.
            # The structlog processor runs in sync context; if there's no loop,
            # we can't send anything.
            return
        else:
            # We're in async context — create task directly
            self._create_send_task(bot, event_dict, loop)

    def _create_send_task(
        self,
        bot: Bot,
        event_dict: dict[str, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Create an asyncio.Task for sending the notification."""
        loop.create_task(self._send(bot, event_dict))

    @staticmethod
    async def _send(bot: Bot, event_dict: dict[str, Any]) -> None:
        """Send the log event to the admin chat via existing infrastructure."""
        try:
            # Lazy import to avoid circular dependencies at startup
            from app.middlewares.global_error import send_error_to_admin_chat
            texts = get_texts(getattr(settings, 'DEFAULT_LANGUAGE', 'ru') or 'ru')

            # Build a pseudo-Exception from the event_dict
            error = _make_event_dict_error(event_dict)

            # Build rich context from event_dict
            context_parts: list[str] = []
            logger_name = event_dict.get('logger', '')
            if logger_name:
                context_parts.append(
                    texts.t(
                        'LOGGING_HANDLER_CONTEXT_LOGGER_LINE',
                        'Logger: {logger_name}',
                    ).format(logger_name=logger_name)
                )
            user_id = event_dict.get('user_id')
            username = event_dict.get('username')
            if user_id:
                if username:
                    user_str = texts.t(
                        'LOGGING_HANDLER_CONTEXT_USER_WITH_USERNAME_LINE',
                        'User: {user_id} (@{username})',
                    ).format(user_id=user_id, username=username)
                else:
                    user_str = texts.t(
                        'LOGGING_HANDLER_CONTEXT_USER_LINE',
                        'User: {user_id}',
                    ).format(user_id=user_id)
                context_parts.append(user_str)

            context = '\n'.join(context_parts)

            # Extract traceback from exc_info if present
            tb_override: str | None = None
            exc_info = event_dict.get('exc_info')
            if exc_info and isinstance(exc_info, tuple) and exc_info[2] is not None:
                tb_override = ''.join(traceback.format_exception(*exc_info))

            await send_error_to_admin_chat(bot, error, context, tb_override=tb_override)

        except Exception:
            # Never let an exception leak — this is a logging processor,
            # recursion would kill the application.
            pass


def _make_event_dict_error(event_dict: dict[str, Any]) -> Exception:
    """Create an Exception wrapper for a structlog event_dict.

    ``send_error_to_admin_chat`` uses ``type(error).__name__`` as error_type.
    If exc_info contains a real exception, use its type name.
    Otherwise, create a descriptive class from the log level.
    """
    # Prefer the real exception type from exc_info or error kwarg
    exc_info = event_dict.get('exc_info')
    if exc_info and isinstance(exc_info, tuple) and exc_info[1] is not None:
        real_exc = exc_info[1]
        class_name = type(real_exc).__name__
    else:
        error_kwarg = event_dict.get('error')
        if error_kwarg and isinstance(error_kwarg, BaseException):
            class_name = type(error_kwarg).__name__
        else:
            level = event_dict.get('level', 'error')
            class_name = f'Log{level.capitalize()}'

    error_cls = type(
        class_name,
        (Exception,),
        {
            '__str__': lambda self: self.args[0] if self.args else '',
        },
    )
    message = str(event_dict.get('event', ''))
    error = error_cls(message)
    error.event_dict = event_dict  # type: ignore[attr-defined]
    return error
