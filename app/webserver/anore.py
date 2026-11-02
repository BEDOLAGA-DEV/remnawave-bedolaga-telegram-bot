"""Bounded, signed Anore webhooks; never acknowledge uncommitted money."""

import json
from contextlib import aclosing

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.anore_service import anore_service
from app.services.payment.anore import AnoreValidationError


logger = structlog.get_logger(__name__)
_MAX_BODY_BYTES = 65536


def _reject_constant(_value):
    raise ValueError('Invalid JSON number')


def register_anore_webhook(router: APIRouter, payment_service) -> None:
    @router.post(settings.ANORE_WEBHOOK_PATH, include_in_schema=False)
    async def anore_webhook(request: Request):
        body = bytearray()
        async with aclosing(request.stream()) as stream:
            async for chunk in stream:
                body.extend(chunk)
                if len(body) > _MAX_BODY_BYTES:
                    return JSONResponse({'error': 'Request too large'}, status_code=413)
        signature = request.headers.get('Anore-Signature', '')
        if not anore_service.verify_callback_signature(bytes(body), signature):
            return JSONResponse({'error': 'Invalid signature'}, status_code=401)
        try:
            payload = json.loads(body, parse_float=str, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return JSONResponse({'error': 'Invalid JSON'}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({'error': 'Invalid payload'}, status_code=400)
        event = request.headers.get('Anore-Event', '')
        delivery = request.headers.get('Anore-Delivery-Id', '')
        if not event or payload.get('event') != event or not delivery or len(delivery) > 128:
            return JSONResponse({'error': 'Invalid event headers'}, status_code=400)
        payload['_event_header'] = event
        payload['_delivery_id'] = delivery
        from app.webserver.payments import _process_payment_service_callback

        try:
            accepted = await _process_payment_service_callback(payment_service, payload, 'process_anore_callback')
        except AnoreValidationError:
            return JSONResponse({'error': 'Payment validation failed'}, status_code=400)
        except Exception:
            logger.warning('Anore callback processing failed')
            accepted = False
        if not accepted:
            return JSONResponse({'error': 'Payment processing unavailable'}, status_code=500)
        return JSONResponse({'success': True})
