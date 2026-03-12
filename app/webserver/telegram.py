from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import settings


logger = structlog.get_logger(__name__)


class TelegramWebhookProcessorError(RuntimeError):
    """Базовое исключение очереди Telegram webhook."""


class TelegramWebhookProcessorNotRunningError(TelegramWebhookProcessorError):
    """Очередь ещё не запущена или уже остановлена."""


class TelegramWebhookOverloadedError(TelegramWebhookProcessorError):
    """Очередь переполнена и не успевает обрабатывать новые обновления."""


class TelegramWebhookProcessor:
    """Асинхронная очередь обработки Telegram webhook-ов."""

    def __init__(
        self,
        *,
        bot: Bot | None = None,
        dispatcher: Dispatcher,
        queue_maxsize: int,
        worker_count: int,
        enqueue_timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self._default_bot = bot
        self._dispatcher = dispatcher
        self._queue_maxsize = max(1, queue_maxsize)
        self._worker_count = max(0, worker_count)
        self._enqueue_timeout = max(0.0, enqueue_timeout)
        self._shutdown_timeout = max(1.0, shutdown_timeout)
        self._queue: asyncio.Queue[tuple[Bot, Update] | object] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._stop_sentinel: object = object()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return

            self._running = True
            self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
            self._workers.clear()

            for index in range(self._worker_count):
                task = asyncio.create_task(
                    self._worker_loop(index),
                    name=f'telegram-webhook-worker-{index}',
                )
                self._workers.append(task)

            if self._worker_count:
                logger.info(
                    '🚀 Telegram webhook processor запущен: воркеров, очередь',
                    worker_count=self._worker_count,
                    queue_maxsize=self._queue_maxsize,
                )
            else:
                logger.warning('Telegram webhook processor запущен без воркеров — обновления не будут обрабатываться')

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._running:
                return

            self._running = False

            if self._worker_count > 0:
                try:
                    await asyncio.wait_for(self._queue.join(), timeout=self._shutdown_timeout)
                except TimeoutError:
                    logger.warning(
                        '⏱️ Не удалось дождаться завершения очереди Telegram webhook за секунд',
                        shutdown_timeout=self._shutdown_timeout,
                    )
            else:
                drained = 0
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - гонка состояния
                        break
                    else:
                        drained += 1
                        self._queue.task_done()
                if drained:
                    logger.warning(
                        'Очередь Telegram webhook остановлена без воркеров, потеряно обновлений', drained=drained
                    )

            for _ in range(len(self._workers)):
                try:
                    self._queue.put_nowait(self._stop_sentinel)
                except asyncio.QueueFull:
                    # Очередь переполнена, подождём пока освободится место
                    await self._queue.put(self._stop_sentinel)

            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            logger.info('🛑 Telegram webhook processor остановлен')

    async def enqueue(self, bot: Bot | Update, update: Update | None = None) -> None:
        if not self._running:
            raise TelegramWebhookProcessorNotRunningError

        resolved_bot: Bot | None
        resolved_update: Update
        if update is None:
            resolved_bot = self._default_bot
            resolved_update = bot  # type: ignore[assignment]
        else:
            resolved_bot = bot  # type: ignore[assignment]
            resolved_update = update

        if resolved_bot is None:
            raise TelegramWebhookProcessorNotRunningError

        try:
            if self._enqueue_timeout <= 0:
                self._queue.put_nowait((resolved_bot, resolved_update))
            else:
                await asyncio.wait_for(
                    self._queue.put((resolved_bot, resolved_update)),
                    timeout=self._enqueue_timeout,
                )
        except asyncio.QueueFull as error:  # pragma: no cover - защитный сценарий
            raise TelegramWebhookOverloadedError from error
        except TimeoutError as error:
            raise TelegramWebhookOverloadedError from error

    async def wait_until_drained(self, timeout: float | None = None) -> None:
        if not self._running or self._worker_count == 0:
            return
        if timeout is None:
            await self._queue.join()
            return
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def _worker_loop(self, worker_id: int) -> None:
        try:
            while True:
                try:
                    item = await self._queue.get()
                except asyncio.CancelledError:  # pragma: no cover - остановка приложения
                    logger.debug('Worker cancelled', worker_id=worker_id)
                    raise

                if item is self._stop_sentinel:
                    self._queue.task_done()
                    break

                bot, update = item
                try:
                    await self._dispatcher.feed_update(bot, update)
                except asyncio.CancelledError:  # pragma: no cover - остановка приложения
                    logger.debug('Worker cancelled during processing', worker_id=worker_id)
                    raise
                except Exception as error:  # pragma: no cover - логируем сбой обработчика
                    logger.exception('Ошибка обработки Telegram update в worker', worker_id=worker_id, error=error)
                finally:
                    self._queue.task_done()
        finally:
            logger.debug('Worker завершён', worker_id=worker_id)


async def _dispatch_update(
    update: Update,
    *,
    dispatcher: Dispatcher,
    bot: Bot,
    processor: TelegramWebhookProcessor | None,
) -> None:
    if processor is not None:
        try:
            await processor.enqueue(bot, update)
        except TelegramWebhookOverloadedError as error:
            logger.warning('Очередь Telegram webhook переполнена', error=error)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='webhook_queue_full') from error
        except TelegramWebhookProcessorNotRunningError as error:
            logger.error('Telegram webhook processor неактивен', error=error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='webhook_processor_unavailable'
            ) from error
        return

    await dispatcher.feed_update(bot, update)


def create_telegram_router(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    processor: TelegramWebhookProcessor | None = None,
    bot_routes: list[tuple[str, Bot]] | None = None,
) -> APIRouter:
    router = APIRouter()
    secret_token = settings.WEBHOOK_SECRET_TOKEN
    routes = bot_routes or [(settings.get_telegram_webhook_path(), bot)]

    def _normalize_path(path: str) -> str:
        normalized = (path or '').strip()
        if not normalized:
            normalized = '/webhook'
        if not normalized.startswith('/'):
            normalized = '/' + normalized
        return normalized

    def _build_webhook_handler(route_bot: Bot, webhook_path: str):
        async def telegram_webhook(request: Request) -> JSONResponse:
            if secret_token:
                header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
                if header_token != secret_token:
                    logger.warning('Получен Telegram webhook с неверным секретом', webhook_path=webhook_path)
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid_secret_token')

            content_type = request.headers.get('content-type', '')
            if content_type and 'application/json' not in content_type.lower():
                raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail='invalid_content_type')

            try:
                payload: Any = await request.json()
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error('Ошибка чтения Telegram webhook', error=error, webhook_path=webhook_path)
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_payload') from error

            try:
                update = Update.model_validate(payload)
            except Exception as error:  # pragma: no cover - defensive logging
                logger.error('Ошибка валидации Telegram update', error=error, webhook_path=webhook_path)
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_update') from error

            await _dispatch_update(update, dispatcher=dispatcher, bot=route_bot, processor=processor)
            return JSONResponse({'status': 'ok'})

        return telegram_webhook

    for raw_webhook_path, route_bot in routes:
        webhook_path = _normalize_path(raw_webhook_path)
        router.add_api_route(webhook_path, _build_webhook_handler(route_bot, webhook_path), methods=['POST'])

    @router.get('/health/telegram-webhook')
    async def telegram_webhook_health() -> JSONResponse:
        route_paths = [_normalize_path(path) for path, _ in routes]
        return JSONResponse(
            {
                'status': 'ok',
                'mode': settings.get_bot_run_mode(),
                'path': route_paths[0] if route_paths else None,
                'paths': route_paths,
                'webhook_configured': bool(settings.get_telegram_webhook_url()),
                'queue_maxsize': settings.get_webhook_queue_maxsize(),
                'workers': settings.get_webhook_worker_count(),
            }
        )

    return router
