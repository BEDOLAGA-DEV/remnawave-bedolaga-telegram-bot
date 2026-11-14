"""Сетевой сбой Telegram не должен останавливать бота.

8 ноября обычное пересоздание контейнера попало на нестабильную SOCKS-ноду:
``TelegramNetworkError`` → «Polling завершился с ошибкой» → завершение процесса,
7 рестартов за 6 минут. getUpdates aiogram повторяет сам, но ``await bot.me()``
в начале ``Dispatcher._polling`` стоит вне этого цикла.

Тесты гоняют настоящие ``Dispatcher`` и ``Bot``; подменён только транспорт.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp_socks
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import User
from aiogram.utils.backoff import BackoffConfig

from app.utils import telegram_polling
from app.utils.telegram_polling import start_polling_with_retry, wait_for_telegram_or_stop


FAST_BACKOFF = BackoffConfig(min_delay=0.001, max_delay=0.002, factor=1.5, jitter=0.0)
GET_ME = GetMe()


class ScriptedSession(BaseSession):
    """Транспорт: ошибки из сценария, затем успешные ответы."""

    def __init__(self, get_me_errors: list[Exception], get_updates_errors: tuple[Exception, ...] = ()) -> None:
        super().__init__()
        self.get_me_errors = list(get_me_errors)
        self.get_updates_errors = list(get_updates_errors)
        self.get_me_calls = 0
        self.polling_reached = asyncio.Event()

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        await asyncio.sleep(0)
        if isinstance(method, GetMe):
            self.get_me_calls += 1
            if self.get_me_errors:
                raise self.get_me_errors.pop(0)
            return User(id=42, is_bot=True, first_name='Bot', username='bot')
        if isinstance(method, GetUpdates):
            if self.get_updates_errors:
                raise self.get_updates_errors.pop(0)
            self.polling_reached.set()
            return []
        raise AssertionError(f'неожиданный метод: {method!r}')

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        yield b''


def _make(session: ScriptedSession) -> tuple[Dispatcher, Bot]:
    return Dispatcher(), Bot('42:TEST', session=session)


async def _run_until_polling(dp: Dispatcher, bot: Bot, session: ScriptedSession) -> None:
    """Polling дошёл до getUpdates и остановлен штатно; упавший запуск поднимет своё исключение."""
    task = asyncio.create_task(
        start_polling_with_retry(
            dp, bot, handle_signals=False, backoff_config=FAST_BACKOFF, startup_backoff=FAST_BACKOFF
        )
    )
    reached = asyncio.create_task(session.polling_reached.wait())
    done, _ = await asyncio.wait({task, reached}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        reached.cancel()
        task.result()  # поднимет исключение упавшего запуска
        pytest.fail('polling завершился, не дойдя до getUpdates')
    assert reached in done, 'polling не дошёл до getUpdates за 5 с'
    await dp.stop_polling()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        pytest.param(TelegramNetworkError(method=GET_ME, message='ClientOSError: reset'), id='telegram-network'),
        pytest.param(TelegramServerError(method=GET_ME, message='Bad Gateway'), id='telegram-5xx'),
        pytest.param(aiohttp.ClientOSError(104, 'Connection reset by peer'), id='bare-client-os-error'),
        pytest.param(aiohttp_socks.ProxyError('general SOCKS failure', error_code=1), id='socks-proxy-error'),
        pytest.param(aiohttp_socks.ProxyConnectionError('refused'), id='socks-connection-error'),
        pytest.param(aiohttp_socks.ProxyTimeoutError('timed out'), id='socks-timeout-error'),
        pytest.param(TimeoutError(), id='timeout'),
    ],
)
async def test_transient_error_at_polling_start_is_retried(error: Exception) -> None:
    session = ScriptedSession(get_me_errors=[error, error])
    dp, bot = _make(session)

    await _run_until_polling(dp, bot, session)

    assert session.get_me_calls == 3


@pytest.mark.asyncio
async def test_invalid_token_stays_fatal() -> None:
    session = ScriptedSession(get_me_errors=[TelegramUnauthorizedError(method=GET_ME, message='Unauthorized')])
    dp, bot = _make(session)

    with pytest.raises(TelegramUnauthorizedError):
        await asyncio.wait_for(
            start_polling_with_retry(dp, bot, handle_signals=False, startup_backoff=FAST_BACKOFF), timeout=5
        )

    assert session.get_me_calls == 1


@pytest.mark.asyncio
async def test_network_error_during_get_updates_does_not_end_polling() -> None:
    """Страховка на обновление aiogram: сейчас этот участок он повторяет сам."""
    session = ScriptedSession(
        get_me_errors=[],
        get_updates_errors=(TelegramNetworkError(method=GetUpdates(), message='ClientOSError: reset'),),
    )
    dp, bot = _make(session)

    await _run_until_polling(dp, bot, session)


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        if delay:  # sleep(0) транспорта — не пауза между попытками
            delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(telegram_polling.asyncio, 'sleep', fake_sleep)
    return delays


@pytest.mark.asyncio
async def test_retry_after_waits_as_long_as_telegram_asked(recorded_sleeps: list[float]) -> None:
    session = ScriptedSession(get_me_errors=[TelegramRetryAfter(method=GET_ME, message='Flood', retry_after=17)])
    dp, bot = _make(session)

    await _run_until_polling(dp, bot, session)

    assert 17 in recorded_sleeps


@pytest.mark.asyncio
async def test_backoff_grows_and_is_capped(recorded_sleeps: list[float]) -> None:
    error = TelegramNetworkError(method=GET_ME, message='ClientOSError: reset')
    session = ScriptedSession(get_me_errors=[error] * 5)
    dp, bot = _make(session)

    await asyncio.wait_for(
        telegram_polling.wait_for_telegram(bot, BackoffConfig(min_delay=1.0, max_delay=4.0, factor=2.0, jitter=0.0)),
        timeout=5,
    )

    assert recorded_sleeps == [1.0, 2.0, 4.0, 4.0, 4.0]


@pytest.mark.asyncio
async def test_shutdown_cancels_retry_wait() -> None:
    error = TelegramNetworkError(method=GET_ME, message='ClientOSError: reset')
    session = ScriptedSession(get_me_errors=[error] * 1000)
    dp, bot = _make(session)
    slow = BackoffConfig(min_delay=60.0, max_delay=61.0, factor=1.1, jitter=0.0)
    task = asyncio.create_task(start_polling_with_retry(dp, bot, handle_signals=False, startup_backoff=slow))
    while session.get_me_calls == 0:
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


def test_main_starts_polling_through_retry_wrapper() -> None:
    source = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')

    assert 'start_polling_with_retry(dp, bot' in source
    assert 'dp.start_polling(' not in source


async def _until_first_get_me(session: ScriptedSession, task: asyncio.Task[Any]) -> None:
    """Ждёт первый запрос; упавшая задача не должна подвешивать тест."""
    async with asyncio.timeout(5):
        while session.get_me_calls == 0 and not task.done():
            await asyncio.sleep(0)
    if task.done():
        task.result()
        pytest.fail('ожидание завершилось раньше первого запроса к Telegram')


def test_main_waits_for_telegram_before_startup_calls() -> None:
    source = (Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8')

    assert 'wait_for_telegram_or_stop(bot, lambda: killer.exit)' in source
    assert 'bot.get_me()' not in source


@pytest.mark.asyncio
async def test_startup_wait_survives_network_error_and_caches_identity() -> None:
    error = TelegramNetworkError(method=GET_ME, message='ClientOSError: reset')
    session = ScriptedSession(get_me_errors=[error, error])
    _, bot = _make(session)

    assert await asyncio.wait_for(wait_for_telegram_or_stop(bot, lambda: False, FAST_BACKOFF), timeout=5) is True

    assert (await bot.me()).username == 'bot'
    assert session.get_me_calls == 3


@pytest.mark.asyncio
async def test_startup_wait_keeps_invalid_token_fatal() -> None:
    session = ScriptedSession(get_me_errors=[TelegramUnauthorizedError(method=GET_ME, message='Unauthorized')])
    _, bot = _make(session)

    with pytest.raises(TelegramUnauthorizedError):
        await asyncio.wait_for(wait_for_telegram_or_stop(bot, lambda: False, FAST_BACKOFF), timeout=5)

    assert session.get_me_calls == 1


@pytest.mark.asyncio
async def test_startup_wait_stops_during_long_backoff() -> None:
    """docker stop, пока Telegram недоступен: ждать конца минутной паузы нельзя."""
    error = TelegramNetworkError(method=GET_ME, message='ClientOSError: reset')
    session = ScriptedSession(get_me_errors=[error] * 1000)
    _, bot = _make(session)
    slow = BackoffConfig(min_delay=60.0, max_delay=61.0, factor=1.1, jitter=0.0)
    stop = False
    task = asyncio.create_task(wait_for_telegram_or_stop(bot, lambda: stop, slow, poll_interval=0.01))
    await _until_first_get_me(session, task)

    stop = True

    assert await asyncio.wait_for(task, timeout=1) is False


class HangingSession(ScriptedSession):
    """Запрос повис: сокет открыт, ответа нет."""

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.get_me_calls += 1
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_startup_wait_stops_during_hung_request() -> None:
    session = HangingSession(get_me_errors=[])
    _, bot = _make(session)
    stop = False
    task = asyncio.create_task(wait_for_telegram_or_stop(bot, lambda: stop, FAST_BACKOFF, poll_interval=0.01))
    await _until_first_get_me(session, task)

    stop = True

    assert await asyncio.wait_for(task, timeout=1) is False


@pytest.mark.asyncio
async def test_real_sigterm_reaches_handler_installed_before_polling() -> None:
    """Без handle_signals=False aiogram забирает SIGTERM, и главный цикл его не видит."""
    received: list[int] = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, lambda signum, _frame: received.append(signum))
    session = ScriptedSession(get_me_errors=[])
    dp, bot = _make(session)
    task = asyncio.create_task(start_polling_with_retry(dp, bot, backoff_config=FAST_BACKOFF))
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(session.polling_reached.wait(), timeout=5)

        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.05)

        assert received == [signal.SIGTERM]
        assert not task.done(), 'polling остановился сам: главный цикл об этом не узнает'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


@pytest.mark.asyncio
async def test_cancelled_startup_wait_cleans_up_request(monkeypatch):
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def request(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(telegram_polling, 'wait_for_telegram', request)
    task = asyncio.create_task(wait_for_telegram_or_stop(None, lambda: False))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            assert await task is None
        assert finished.is_set(), 'startup request outlived its owner'
    finally:
        # Clean up the orphan on the unfixed implementation too.
        for pending in asyncio.all_tasks():
            if pending.get_coro().__qualname__ == request.__qualname__:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_already_requested_does_not_contact_telegram():
    session = ScriptedSession(get_me_errors=[])
    _, bot = _make(session)
    assert await wait_for_telegram_or_stop(bot, lambda: True) is False
    assert session.get_me_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['setWebhook', 'deleteWebhook'])
@pytest.mark.parametrize('failure_kind', ['network', 'server', 'rate-limit', 'proxy', 'unauthorized', 'bad-request'])
async def test_transport_setup_retry_policy(operation, failure_kind, recorded_sleeps):
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteWebhook, SetWebhook

    method = SetWebhook(url='https://bot.example/webhook') if operation == 'setWebhook' else DeleteWebhook()
    errors = {
        'network': TelegramNetworkError(method=method, message='reset'),
        'server': TelegramServerError(method=method, message='unavailable'),
        'rate-limit': TelegramRetryAfter(method=method, message='Flood', retry_after=17),
        'proxy': aiohttp_socks.ProxyConnectionError('refused'),
        'unauthorized': TelegramUnauthorizedError(method=method, message='Unauthorized'),
        'bad-request': TelegramBadRequest(method=method, message='invalid URL'),
    }
    request = AsyncMock(side_effect=[errors[failure_kind], True])
    coroutine = telegram_polling.telegram_startup_request_or_stop(request, lambda: False, operation=operation)
    if failure_kind in ('unauthorized', 'bad-request'):
        with pytest.raises(type(errors[failure_kind])):
            assert await coroutine is True
        assert request.await_count == 1
        assert recorded_sleeps == []
    else:
        assert await coroutine is True
        assert request.await_count == 2
        assert recorded_sleeps == ([17.0] if failure_kind == 'rate-limit' else [1.0])


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_transport_setup_stop_cleans_up_hung_request(cancel):
    entered = asyncio.Event()
    finished = asyncio.Event()
    stop = False

    async def request():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = asyncio.create_task(
        telegram_polling.telegram_startup_request_or_stop(
            request,
            lambda: stop,
            operation='setWebhook',
            poll_interval=0.001,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    else:
        stop = True
        assert await asyncio.wait_for(task, timeout=1) is False
    assert finished.is_set()


@pytest.mark.asyncio
async def test_retry_log_does_not_include_transport_credentials(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    logger = Mock()
    monkeypatch.setattr(telegram_polling, 'logger', logger)
    request = AsyncMock(side_effect=[OSError('socks://test-user:test-secret@proxy.example'), True])
    assert (
        await telegram_polling.telegram_startup_request_or_stop(
            request,
            lambda: False,
            operation='deleteWebhook',
            backoff_config=FAST_BACKOFF,
        )
        is True
    )
    assert 'test-secret' not in str(logger.mock_calls)
    assert logger.warning.call_args.kwargs['error_type'] == 'OSError'
