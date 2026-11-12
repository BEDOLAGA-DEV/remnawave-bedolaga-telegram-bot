"""Запуск long polling, переживающий сетевой сбой Telegram.

getUpdates aiogram повторяет сам, но ``await bot.me()`` в начале
``Dispatcher._polling`` стоит вне этого цикла: сбой в момент старта завершает
polling, а за ним и весь процесс. Поэтому личность бота получаем заранее и с
повторами; ``Bot.me()`` кеширует ответ, и внутри polling сетевого вызова уже нет.

Сигналами владеет главный цикл (``GracefulExit`` в ``main.py``), поэтому aiogram
запускается с ``handle_signals=False``. Иначе ``loop.add_signal_handler`` заменяет
его обработчик: SIGTERM молча гасит polling, ``killer.exit`` остаётся ложным, и
процесс доживает до SIGKILL без корректного завершения.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final

import aiohttp_socks
import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.utils.backoff import Backoff, BackoffConfig


logger = structlog.get_logger(__name__)

STARTUP_BACKOFF: Final = BackoffConfig(min_delay=1.0, max_delay=30.0, factor=2.0, jitter=0.1)

# Ошибки aiohttp_socks не наследуют aiohttp.ClientError, поэтому aiogram не
# заворачивает их в TelegramNetworkError. TelegramUnauthorizedError здесь нет
# намеренно: неверный токен повторами не лечится.
_TRANSIENT_ERRORS: Final[tuple[type[Exception], ...]] = (
    TelegramNetworkError,
    TelegramServerError,
    OSError,
    TimeoutError,
    aiohttp_socks.ProxyError,
    aiohttp_socks.ProxyConnectionError,
    aiohttp_socks.ProxyTimeoutError,
)


async def wait_for_telegram(bot: Bot, backoff_config: BackoffConfig = STARTUP_BACKOFF) -> None:
    """Повторяет ``bot.me()``, пока Telegram не ответит; фатальные ошибки пробрасывает."""
    await _retry_startup_request(bot.me, 'getMe', backoff_config)


async def _retry_startup_request(
    request: Callable[[], Awaitable[Any]], operation: str, backoff_config: BackoffConfig
) -> None:
    """Only for repeatable startup operations, never messages or payments."""
    backoff = Backoff(config=backoff_config)
    while True:
        try:
            await request()
            return
        except TelegramRetryAfter as error:
            delay = float(error.retry_after)
            failure: Exception = error
        except _TRANSIENT_ERRORS as error:
            delay = next(backoff)
            failure = error
        logger.warning(
            'Telegram недоступен при запуске, повтор',
            operation=operation,
            error_type=type(failure).__name__,
            # Transport errors can contain proxy credentials or a bot URL/token.
            retry_in_seconds=round(delay, 1),
        )
        await asyncio.sleep(delay)


async def wait_for_telegram_or_stop(
    bot: Bot,
    should_stop: Callable[[], bool],
    backoff_config: BackoffConfig = STARTUP_BACKOFF,
    *,
    poll_interval: float = 0.2,
) -> bool:
    """``wait_for_telegram``, которую можно прервать; False значит «просили остановиться».

    Обработчик сигнала только ставит флаг, а повторы бесконечны: без проверки флага
    ``docker stop`` при недоступном Telegram дожидался бы SIGKILL.
    """
    return await _wait_or_stop(lambda: wait_for_telegram(bot, backoff_config), should_stop, poll_interval)


async def telegram_startup_request_or_stop(
    request: Callable[[], Awaitable[Any]],
    should_stop: Callable[[], bool],
    *,
    operation: str,
    backoff_config: BackoffConfig = STARTUP_BACKOFF,
    poll_interval: float = 0.2,
) -> bool:
    """Retry setWebhook/deleteWebhook with identical parameters, keeping queued updates.

    False means shutdown was requested. Do not use for non-idempotent API calls.
    """
    return await _wait_or_stop(
        lambda: _retry_startup_request(request, operation, backoff_config), should_stop, poll_interval
    )


async def _wait_or_stop(
    request: Callable[[], Awaitable[Any]], should_stop: Callable[[], bool], poll_interval: float
) -> bool:
    if should_stop():
        return False
    waiter = asyncio.create_task(request())
    try:
        while not waiter.done():
            if should_stop():
                return False
            await asyncio.wait({waiter}, timeout=poll_interval)
        waiter.result()  # Invalid token/configuration remains fatal.
        return not should_stop()
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def start_polling_with_retry(
    dp: Dispatcher,
    bot: Bot,
    *,
    startup_backoff: BackoffConfig = STARTUP_BACKOFF,
    handle_signals: bool = False,
    **polling_kwargs: Any,
) -> None:
    """``dp.start_polling``, не завершающийся из-за временного сбоя сети на старте."""
    await wait_for_telegram(bot, startup_backoff)
    await dp.start_polling(bot, handle_signals=handle_signals, **polling_kwargs)
