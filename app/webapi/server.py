from __future__ import annotations

import asyncio

import structlog
import uvicorn

from app.config import settings

from .app import create_web_api_app


logger = structlog.get_logger(__name__)


class WebAPIServer:
    """Асинхронный uvicorn-сервер для административного API."""

    def __init__(self, app: object | None = None) -> None:
        self._app = app or create_web_api_app()

        workers = max(1, int(settings.WEB_API_WORKERS or 1))
        if workers > 1:
            logger.warning('WEB_API_WORKERS > 1 не поддерживается в embed-режиме, используем 1')
            workers = 1

        # Кастомный конфиг логирования - скрываем спам от WebSocket
        log_config = {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'default': {
                    '()': 'uvicorn.logging.DefaultFormatter',
                    'fmt': '%(levelprefix)s %(message)s',
                    'use_colors': None,
                },
            },
            'handlers': {
                'default': {
                    'formatter': 'default',
                    'class': 'logging.StreamHandler',
                    'stream': 'ext://sys.stderr',
                },
            },
            'loggers': {
                'uvicorn': {'handlers': ['default'], 'level': 'WARNING', 'propagate': False},
                'uvicorn.error': {'level': 'WARNING', 'propagate': False},
                'uvicorn.access': {'level': 'ERROR', 'propagate': False},
                'uvicorn.protocols': {'level': 'WARNING', 'propagate': False},
                'uvicorn.protocols.websockets': {'level': 'WARNING', 'propagate': False},
                'uvicorn.protocols.websockets.websockets_impl': {'level': 'WARNING', 'propagate': False},
                'websockets': {'level': 'WARNING', 'propagate': False},
                'websockets.server': {'level': 'WARNING', 'propagate': False},
            },
        }

        self._config = uvicorn.Config(
            app=self._app,
            host=settings.WEB_API_HOST,
            port=int(settings.WEB_API_PORT or 8080),
            log_level='warning',
            workers=workers,
            lifespan='on',
            access_log=False,
            log_config=log_config,
        )
        self._server = uvicorn.Server(self._config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.info('🌐 Административное веб-API уже запущено')
            return

        async def _serve() -> None:
            try:
                await self._server.serve()
            except Exception as error:  # pragma: no cover - логируем ошибки сервера
                logger.exception('❌ Ошибка работы веб-API', error=error)
                raise

        logger.info(
            '🌐 Запуск административного API',
            WEB_API_HOST=settings.WEB_API_HOST,
            WEB_API_PORT=settings.WEB_API_PORT,
        )
        self._task = asyncio.create_task(_serve(), name='web-api-server')

        started_attr = getattr(self._server, 'started', None)
        started_event = getattr(self._server, 'started_event', None)

        if isinstance(started_attr, asyncio.Event):
            await started_attr.wait()
        elif isinstance(started_event, asyncio.Event):
            await started_event.wait()
        else:
            while not getattr(self._server, 'started', False):
                if self._task.done():
                    break
                await asyncio.sleep(0.1)

        if self._task.done() and self._task.exception():
            raise self._task.exception()

    async def stop(self) -> None:
        if not self._task:
            return

        logger.info('🛑 Остановка административного API')
        self._server.should_exit = True
        await self._task
        self._task = None
