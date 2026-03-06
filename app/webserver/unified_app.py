from __future__ import annotations

from pathlib import Path

import structlog
from aiogram import Bot, Dispatcher
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.cabinet.routes import router as cabinet_router
from app.config import settings
from app.services.disposable_email_service import disposable_email_service
from app.services.payment_service import PaymentService
from app.webapi.app import create_web_api_app
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


def _create_base_app() -> FastAPI:
    docs_config = settings.get_web_api_docs_config()

    if settings.is_web_api_enabled():
        app = create_web_api_app()
    else:
        app = FastAPI(
            title='Bedolaga Unified Server',
            version=settings.WEB_API_VERSION,
            docs_url=docs_config.get('docs_url'),
            redoc_url=None,
            openapi_url=docs_config.get('openapi_url'),
        )

        add_redoc_endpoint(
            app,
            redoc_url=docs_config.get('redoc_url'),
            openapi_url=docs_config.get('openapi_url'),
            title='Bedolaga Unified Server',
        )

        # Add cabinet routes even when web API is disabled
        if settings.is_cabinet_enabled():
            from fastapi.middleware.cors import CORSMiddleware

            cabinet_origins = settings.get_cabinet_allowed_origins()
            app.add_middleware(
                CORSMiddleware,
                allow_origins=['*'] if '*' in cabinet_origins else cabinet_origins,
                allow_credentials=True,
                allow_methods=['*'],
                allow_headers=['*'],
            )
            app.include_router(cabinet_router)

    _attach_docs_alias(app, app.docs_url)
    return app


def _mount_miniapp_static(app: FastAPI) -> tuple[bool, Path]:
    static_path: Path = settings.get_miniapp_static_path()
    if not static_path.exists():
        logger.debug('Miniapp static path does not exist, skipping mount', static_path=static_path)
        return False, static_path

    try:
        app.mount('/miniapp/static', StaticFiles(directory=static_path), name='miniapp-static')
        logger.info('📦 Miniapp static files mounted at /miniapp/static from', static_path=static_path)
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
    app = _create_base_app()

    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.payment_service = payment_service

    payments_router = payments.create_payment_router(bot, payment_service)
    if payments_router:
        app.include_router(payments_router)

    # Mount RemnaWave incoming webhook router
    remnawave_webhook_enabled = settings.is_remnawave_webhook_enabled()
    if remnawave_webhook_enabled:
        from app.webserver.remnawave_webhook import create_remnawave_webhook_router

        remnawave_router = create_remnawave_webhook_router(bot)
        app.include_router(remnawave_router)
        logger.info('RemnaWave webhook router mounted at', REMNAWAVE_WEBHOOK_PATH=settings.REMNAWAVE_WEBHOOK_PATH)

    payment_providers_state = {
        'tribute': settings.TRIBUTE_ENABLED,
        'mulenpay': settings.is_mulenpay_enabled(),
        'cryptobot': settings.is_cryptobot_enabled(),
        'yookassa': settings.is_yookassa_enabled(),
        'pal24': settings.is_pal24_enabled(),
        'wata': settings.is_wata_enabled(),
        'heleket': settings.is_heleket_enabled(),
        'freekassa': settings.is_freekassa_enabled(),
        'shkeeper': settings.is_shkeeper_enabled(),
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

        @app.on_event('startup')
        async def start_telegram_webhook_processor() -> None:  # pragma: no cover - event hook
            await telegram_processor.start()

        @app.on_event('shutdown')
        async def stop_telegram_webhook_processor() -> None:  # pragma: no cover - event hook
            await telegram_processor.stop()

        app.include_router(telegram.create_telegram_router(bot, dispatcher, processor=telegram_processor))
    else:
        telegram_processor = None

    @app.on_event('startup')
    async def start_disposable_email_service() -> None:  # pragma: no cover - event hook
        await disposable_email_service.start()

    @app.on_event('shutdown')
    async def stop_disposable_email_service() -> None:  # pragma: no cover - event hook
        await disposable_email_service.stop()

    miniapp_mounted, miniapp_path = _mount_miniapp_static(app)

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
