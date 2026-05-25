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


_WEBHOOK_REASON_STATUS = {
    'invalid_signature': status.HTTP_403_FORBIDDEN,
    'configuration_error': status.HTTP_503_SERVICE_UNAVAILABLE,
    'missing_notification_uuid': status.HTTP_400_BAD_REQUEST,
    'signed_transaction_verification_failed': status.HTTP_400_BAD_REQUEST,
}


def _webhook_error_status(reason: str) -> int:
    return _WEBHOOK_REASON_STATUS.get(reason, status.HTTP_500_INTERNAL_SERVER_ERROR)


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
        if not ok:
            return JSONResponse({'status': 'error', 'reason': reason}, status_code=_webhook_error_status(reason))
        return JSONResponse({'status': 'ok', 'reason': reason})

    @router.get('/health/apple-iap')
    async def apple_iap_health() -> JSONResponse:
        enabled = settings.is_apple_iap_enabled()
        if enabled:
            status_text = 'ok'
            status_code = status.HTTP_200_OK
        elif settings.APPLE_IAP_ENABLED:
            status_text = 'configuration_error'
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            status_text = 'disabled'
            status_code = status.HTTP_200_OK

        return JSONResponse(
            {
                'status': status_text,
                'enabled': enabled,
                'environment': settings.get_apple_iap_environment(),
                'webhook_path': settings.APPLE_IAP_WEBHOOK_PATH,
                'products_count': len(settings.get_apple_iap_products()),
                'root_certificates_count': len(settings.get_apple_iap_root_cert_paths()),
                'online_certificate_checks': settings.APPLE_IAP_ENABLE_ONLINE_CERT_CHECKS,
            },
            status_code=status_code,
        )

    return router
