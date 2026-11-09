"""Запуск long polling, переживающий сетевой сбой Telegram.

getUpdates aiogram повторяет сам, но ``await bot.me()`` в начале
``Dispatcher._polling`` стоит вне этого цикла: сбой в момент старта завершает
polling, а за ним и весь процесс. Поэтому личность бота получаем заранее и с
повторами; ``Bot.me()`` кеширует ответ, и внутри polling сетевого вызова уже нет.

Повторять ``start_polling`` целиком нельзя: первый запуск заменяет обработчики
SIGTERM/SIGINT, и сигнал, пришедший в паузе между запусками, потерялся бы.
"""

from __future__ import annotations

import asyncio
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
    backoff = Backoff(config=backoff_config)
    while True:
        try:
            await bot.me()
            return
        except TelegramRetryAfter as error:
            delay = float(error.retry_after)
            failure: Exception = error
        except _TRANSIENT_ERRORS as error:
            delay = next(backoff)
            failure = error
        logger.warning(
            'Telegram недоступен при запуске polling, повтор',
            error_type=type(failure).__name__,
            error=str(failure),
            retry_in_seconds=round(delay, 1),
        )
        await asyncio.sleep(delay)


async def start_polling_with_retry(
    dp: Dispatcher,
    bot: Bot,
    *,
    startup_backoff: BackoffConfig = STARTUP_BACKOFF,
    **polling_kwargs: Any,
) -> None:
    """``dp.start_polling``, не завершающийся из-за временного сбоя сети на старте."""
    await wait_for_telegram(bot, startup_backoff)
    await dp.start_polling(bot, **polling_kwargs)
