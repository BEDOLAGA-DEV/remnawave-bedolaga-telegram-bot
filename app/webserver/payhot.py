"""Bounded signed PayHot callbacks; acknowledge only durable processing."""

import json
import re
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.services.payhot_service import MAX_BODY_BYTES, _identifier, _reject_constant, _unique_object, verify_signature


def register_payhot_webhook(router: APIRouter, payment_service) -> None:
    @router.post(getattr(settings, 'PAYHOT_WEBHOOK_PATH', '/payhot-webhook'), include_in_schema=False)
    async def payhot_webhook(request: Request):
        secret = getattr(settings, 'PAYHOT_WEBHOOK_SECRET', '') or ''
        if not secret:
            return JSONResponse({'error': 'Webhook unavailable'}, status_code=500)
        body = bytearray()
        async with aclosing(request.stream()) as stream:
            async for chunk in stream:
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return JSONResponse({'error': 'Request too large'}, status_code=413)
        for name in (
            'PayHot-Signature',
            'PayHot-Event-Id',
            'PayHot-Delivery-Id',
            'PayHot-Delivery-Attempt',
            'PayHot-Api-Version',
        ):
            if len(request.headers.getlist(name)) > 1:
                return JSONResponse({'error': 'Invalid event headers'}, status_code=400)
        secrets = [secret, getattr(settings, 'PAYHOT_PREVIOUS_WEBHOOK_SECRET', '') or '']
        if not verify_signature(bytes(body), request.headers.get('PayHot-Signature', ''), secrets):
            return JSONResponse({'error': 'Invalid signature'}, status_code=401)
        try:
            payload = json.loads(body, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or payload.get('api_version') != 'v2':
                raise ValueError
            _identifier(payload.get('event_id'))
            if (
                request.headers.get('PayHot-Event-Id') != payload['event_id']
                or request.headers.get('PayHot-Api-Version') != 'v2'
            ):
                raise ValueError
            if not re.fullmatch(r'[!-~]{1,128}', request.headers.get('PayHot-Delivery-Id', '')):
                raise ValueError
            if not re.fullmatch(r'[1-9][0-9]{0,8}', request.headers.get('PayHot-Delivery-Attempt', '')):
                raise ValueError
            event_type = payload.get('event_type')
            if not isinstance(event_type, str):
                raise ValueError
            if payload.get('environment') not in (None, 'production'):
                raise ValueError
            expected_object = (
                'recurring_charge'
                if event_type in ('recurring_charge.cancelled', 'recurring_charge.skipped')
                else 'payment'
            )
            if event_type not in (
                'payment.succeeded',
                'payment.failed',
                'payment.manual_reconciliation',
                'payment.captured',
                'payment.cancelled',
                'payment.refunded',
                'recurring_charge.cancelled',
                'recurring_charge.skipped',
            ):
                raise ValueError
            if not isinstance(payload.get('data'), dict) or payload['data'].get('object') != expected_object:
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            return JSONResponse({'error': 'Invalid event'}, status_code=400)
        from app.webserver.payments import _process_payment_service_callback

        try:
            accepted = await _process_payment_service_callback(payment_service, payload, 'process_payhot_callback')
        except ValueError:
            return JSONResponse({'error': 'Payment validation failed'}, status_code=400)
        except Exception:
            accepted = False
        if not accepted:
            return JSONResponse({'error': 'Payment processing unavailable'}, status_code=500)
        return JSONResponse({'success': True})
