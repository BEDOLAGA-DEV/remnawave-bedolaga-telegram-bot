"""FastAPI router for App Store Server Notifications V2."""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.apple_iap import (
    AppleIAPFulfillmentService,
    AppleIAPNotificationService,
    apple_iap_fulfillment_service,
)


logger = structlog.get_logger(__name__)


def create_apple_iap_router(bot: Any = None) -> APIRouter:
    router = APIRouter()
    fulfillment_service = AppleIAPFulfillmentService(apple_iap_fulfillment_service.apple_service, bot=bot)
    notification_service = AppleIAPNotificationService(
        apple_service=apple_iap_fulfillment_service.apple_service,
        fulfillment_service=fulfillment_service,
    )

    @router.options(settings.APPLE_IAP_WEBHOOK_PATH)
    async def apple_iap_options() -> Response:
        return Response(status_code=status.HTTP_200_OK)

    @router.post(settings.APPLE_IAP_WEBHOOK_PATH)
    async def apple_iap_webhook(request: Request) -> JSONResponse:
        content_type = request.headers.get('content-type', '')
        if content_type and 'application/json' not in content_type.lower():
            return JSONResponse({'status': 'error', 'reason': 'unsupported_media_type'}, status_code=415)

        raw_body = await request.body()
        if not raw_body:
            return JSONResponse({'status': 'error', 'reason': 'empty_body'}, status_code=400)
        if len(raw_body) > 256_000:
            return JSONResponse({'status': 'error', 'reason': 'body_too_large'}, status_code=413)

        try:
            body = json.loads(raw_body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({'status': 'error', 'reason': 'invalid_json'}, status_code=400)

        signed_payload = body.get('signedPayload')
        if not signed_payload:
            return JSONResponse({'status': 'error', 'reason': 'missing_signed_payload'}, status_code=400)

        ok, reason = await notification_service.process_signed_payload(signed_payload, raw_body)
        if not ok and reason == 'invalid_signature':
            return JSONResponse({'status': 'error', 'reason': reason}, status_code=403)
        if not ok and reason == 'configuration_error':
            return JSONResponse({'status': 'error', 'reason': reason}, status_code=503)
        if not ok:
            return JSONResponse({'status': 'error', 'reason': reason}, status_code=500)
        return JSONResponse({'status': 'ok', 'reason': reason})

    @router.get('/health/apple-iap')
    async def apple_iap_health() -> JSONResponse:
        return JSONResponse(
            {
                'status': 'ok',
                'enabled': settings.is_apple_iap_enabled(),
                'environment': settings.get_apple_iap_environment(),
                'webhook_path': settings.APPLE_IAP_WEBHOOK_PATH,
                'products_count': len(settings.get_apple_iap_products()),
                'root_certificates_count': len(settings.get_apple_iap_root_cert_paths()),
                'online_certificate_checks': settings.APPLE_IAP_ENABLE_ONLINE_CERT_CHECKS,
            }
        )

    return router
