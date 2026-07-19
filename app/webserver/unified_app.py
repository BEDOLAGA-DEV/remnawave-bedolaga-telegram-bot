from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from fastapi import FastAPI, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.cabinet.apple_iap import apple_iap_only_router
from app.config import settings
from app.services.disposable_email_service import disposable_email_service
from app.services.payment_service import PaymentService
from app.webapi.docs import add_redoc_endpoint

from . import payments, telegram


logger = structlog.get_logger(__name__)


def _attach_docs_alias(app: FastAPI, docs_url: str | None) -> None:
    if not docs_url:
        return

    alias_path = '/doc'
    if alias_path == docs_url:
        return

    for route in app.router.routes:
        if getattr(route, 'path', None) == alias_path:
            return

    target_url = docs_url

    @app.get(alias_path, include_in_schema=False)
    async def redirect_doc() -> RedirectResponse:  # pragma: no cover - simple redirect
        return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


def _create_base_app(lifespan: Any = None) -> FastAPI:
    docs_config = settings.get_web_api_docs_config()

    if settings.is_web_api_enabled():
        from app.webapi.app import create_web_api_app

        app = create_web_api_app(lifespan=lifespan)
    else:
        app = FastAPI(
            title='Bedolaga Unified Server',
            version=settings.WEB_API_VERSION,
            docs_url=docs_config.get('docs_url'),
            redoc_url=None,
            openapi_url=docs_config.get('openapi_url'),
            lifespan=lifespan,
        )

        add_redoc_endpoint(
            app,
            redoc_url=docs_config.get('redoc_url'),
            openapi_url=docs_config.get('openapi_url'),
            title='Bedolaga Unified Server',
        )

        # Add cabinet routes even when web API is disabled
        if settings.is_cabinet_enabled() or settings.is_apple_iap_enabled():
            from fastapi.middleware.cors import CORSMiddleware

            cabinet_origins = settings.get_cabinet_allowed_origins()
            if '*' in cabinet_origins:
                logger.warning('CORS wildcard with credentials is insecure, disabling credentials for wildcard')
                app.add_middleware(
                    CORSMiddleware,
                    allow_origins=['*'],
                    allow_credentials=False,
                    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
                    allow_headers=['Authorization', 'Content-Type', 'X-CSRF-Token', 'X-Telegram-Init-Data'],
                )
            else:
                app.add_middleware(
                    CORSMiddleware,
                    allow_origins=cabinet_origins,
                    allow_credentials=True,
                    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
                    allow_headers=['Authorization', 'Content-Type', 'X-CSRF-Token', 'X-Telegram-Init-Data'],
                )
            if settings.is_cabinet_enabled():
                from app.cabinet.routes import router as cabinet_router

                app.include_router(cabinet_router)
            else:
                app.include_router(apple_iap_only_router)

    _attach_docs_alias(app, app.docs_url)
    return app


def _mount_uploads_static(app: FastAPI) -> None:
    """Mount the media uploads directory as a static file server at /uploads."""
    uploads_path = settings.get_media_upload_path()
    uploads_path.mkdir(parents=True, exist_ok=True)
    try:
        app.mount('/uploads', StaticFiles(directory=uploads_path), name='media-uploads')
        logger.info('Media uploads static files mounted at /uploads', uploads_path=str(uploads_path))
    except RuntimeError as error:  # pragma: no cover - defensive guard
        logger.warning('Failed to mount media uploads static files', error=error)


def _mount_miniapp_static(app: FastAPI) -> tuple[bool, Path]:
    static_path: Path = settings.get_miniapp_static_path()
    if not static_path.exists():
        logger.debug('Miniapp static path does not exist, skipping mount', static_path=static_path)
        return False, static_path

    try:
        app.mount('/miniapp/static', StaticFiles(directory=static_path), name='miniapp-static')
        logger.info('📦 Miniapp static files mounted at /miniapp/static', static_path=static_path)
    except RuntimeError as error:  # pragma: no cover - defensive guard
        logger.warning('Не удалось смонтировать статические файлы миниаппа', error=error)
        return False, static_path

    return True, static_path


def create_unified_app(
    bot: Bot,
    dispatcher: Dispatcher,
    payment_service: PaymentService,
    *,
    enable_telegram_webhook: bool,
) -> FastAPI:
    # Single ASGI lifespan, заменяет 5 deprecated @app.on_event() хуков.
    # Хэндлеры регистрируются ниже после того, как соответствующие сервисы
    # инстанцируются. Замыкаемся на списки — FastAPI(lifespan=...) фиксирует
    # объект функции, но функция читает списки по ссылке в момент срабатывания.
    startup_handlers: list[Callable[[], Awaitable[None]]] = []
    # Shutdown идёт в порядке append'а (НЕ reverse), чтобы критичные drain'ы
    # (например RemnaWave webhook drain — он шлёт через aiogram) выполнились
    # ПЕРЕД остановкой нижележащих процессоров. См. порядок ниже.
    shutdown_handlers: list[Callable[[], Awaitable[None]]] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # pragma: no cover - ASGI lifespan
        # Startup — fail-fast: если критичный сервис не поднялся, лучше
        # crash loop в orchestrator'е, чем «бот запустился» в /health, но
        # не принимает updates. Совпадает с прежней семантикой @app.on_event,
        # где raise в startup-хэндлере отменял ASGI lifespan.
        for handler in startup_handlers:
            await handler()
        try:
            yield
        finally:
            # Shutdown — best-effort: ошибка в одном drain'е не должна блокировать
            # остальные. Особенно важно для drain'а RemnaWave webhook'а, который
            # шлёт реальные Telegram-сообщения и может упасть на flood control.
            for handler in shutdown_handlers:
                try:
                    await handler()
                except Exception:
                    logger.exception(
                        'Lifespan shutdown handler failed',
                        handler=getattr(handler, '__qualname__', repr(handler)),
                    )

    app = _create_base_app(lifespan=lifespan)

    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.payment_service = payment_service

    payments_router = payments.create_payment_router(bot, payment_service)
    if payments_router:
        app.include_router(payments_router)

    # Mount RemnaWave incoming webhook router
    remnawave_webhook_enabled = settings.is_remnawave_webhook_enabled()
    if remnawave_webhook_enabled:
        from app.services.remnawave_webhook_service import RemnaWaveWebhookService
        from app.webserver.remnawave_webhook import create_remnawave_webhook_router

        remnawave_webhook_service = RemnaWaveWebhookService(bot)
        remnawave_router = create_remnawave_webhook_router(bot, remnawave_webhook_service)
        app.include_router(remnawave_router)
        app.state.remnawave_webhook_service = remnawave_webhook_service
        logger.info('RemnaWave webhook router mounted', REMNAWAVE_WEBHOOK_PATH=settings.REMNAWAVE_WEBHOOK_PATH)

        # ВАЖНО: drain должен выполниться ПЕРЕД остановкой telegram-процессора
        # и disposable-email сервиса (последние могут закрыть aiogram session,
        # без которой drain не сможет послать уведомление). Поэтому добавляем
        # ПЕРВЫМ в shutdown_handlers — итерация без reverse.
        shutdown_handlers.append(remnawave_webhook_service.stop)

    payment_providers_state = {
        'tribute': settings.TRIBUTE_ENABLED,
        'mulenpay': settings.is_mulenpay_enabled(),
        'cryptobot': settings.is_cryptobot_enabled(),
        'yookassa': settings.is_yookassa_enabled(),
        'pal24': settings.is_pal24_enabled(),
        'wata': settings.is_wata_enabled(),
        'heleket': settings.is_heleket_enabled(),
        'apple_iap': settings.is_apple_iap_enabled(),
        'freekassa': settings.is_freekassa_enabled(),
        'riopay': settings.is_riopay_enabled(),
    }

    if enable_telegram_webhook:
        telegram_processor = telegram.TelegramWebhookProcessor(
            bot=bot,
            dispatcher=dispatcher,
            queue_maxsize=settings.get_webhook_queue_maxsize(),
            worker_count=settings.get_webhook_worker_count(),
            enqueue_timeout=settings.get_webhook_enqueue_timeout(),
            shutdown_timeout=settings.get_webhook_shutdown_timeout(),
        )
        app.state.telegram_webhook_processor = telegram_processor

        startup_handlers.append(telegram_processor.start)
        shutdown_handlers.append(telegram_processor.stop)

        app.include_router(telegram.create_telegram_router(bot, dispatcher, processor=telegram_processor))
    else:
        telegram_processor = None

    startup_handlers.append(disposable_email_service.start)
    shutdown_handlers.append(disposable_email_service.stop)

    miniapp_mounted, miniapp_path = _mount_miniapp_static(app)
    _mount_uploads_static(app)

    # Root-level Antilopay site-verification file. Antilopay crawler ходит
    # точно по `/<host>/apay-meta-file.txt`, поэтому не можем спрятать роут
    # под /cabinet prefix. Отдаём 404, если значение не настроено, чтобы
    # не светить статус «фича выключена» отдельным сигналом.
    # `no-store`: если оператор ротирует токен, верификация Antilopay не должна
    # упираться в кэш промежуточных CDN/прокси.
    @app.get('/apay-meta-file.txt', include_in_schema=False)
    async def apay_meta_file() -> Response:  # pragma: no cover - thin static endpoint
        token = (settings.ANTILOPAY_APAY_VERIFICATION_TAG or '').strip()
        if not token:
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        return PlainTextResponse(
            content=token,
            media_type='text/plain; charset=utf-8',
            headers={'Cache-Control': 'no-store'},
        )

    unified_health_path = '/health/unified' if settings.is_web_api_enabled() else '/health'

    @app.get(unified_health_path)
    async def unified_health() -> JSONResponse:
        webhook_path = settings.get_telegram_webhook_path() if enable_telegram_webhook else None

        telegram_state = {
            'enabled': enable_telegram_webhook,
            'running': bool(telegram_processor and telegram_processor.is_running),
            'url': settings.get_telegram_webhook_url(),
            'path': webhook_path,
            'secret_configured': bool(settings.WEBHOOK_SECRET_TOKEN),
            'queue_maxsize': settings.get_webhook_queue_maxsize(),
            'workers': settings.get_webhook_worker_count(),
        }

        payment_state = {
            'enabled': bool(payments_router),
            'providers': payment_providers_state,
        }

        miniapp_state = {
            'mounted': miniapp_mounted,
            'path': str(miniapp_path),
        }

        remnawave_webhook_state = {
            'enabled': remnawave_webhook_enabled,
            'path': settings.REMNAWAVE_WEBHOOK_PATH if remnawave_webhook_enabled else None,
        }

        return JSONResponse(
            {
                'status': 'ok',
                'bot_run_mode': settings.get_bot_run_mode(),
                'web_api_enabled': settings.is_web_api_enabled(),
                'payment_webhooks': payment_state,
                'telegram_webhook': telegram_state,
                'remnawave_webhook': remnawave_webhook_state,
                'miniapp_static': miniapp_state,
            }
        )

    return app
