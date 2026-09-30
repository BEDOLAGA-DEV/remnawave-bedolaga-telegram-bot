"""Приём вебхуков DPI//CHECKER: подпись HMAC-SHA256 по сырому телу, разбор — в фасаде.

Метка времени в подпись у сервиса не входит, поэтому повторы отсекаются по ``X-DPIChecker-Delivery``
(в фасаде). Тело бывает сотни КБ: сервис шлёт результаты проверки целиком.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse


logger = structlog.get_logger(__name__)

WEBHOOK_PATH = '/dpichecker/webhook'
MAX_BODY_BYTES = 8 * 1024 * 1024
SIGNATURE_PREFIX = 'sha256='


def _signature_ok(raw_body: bytes, header: str, secret: str) -> bool:
    if not secret or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(SIGNATURE_PREFIX) :])


def _reply(reason: str, code: int) -> JSONResponse:
    return JSONResponse({'status': 'error', 'reason': reason}, status_code=code)


def create_dpichecker_webhook_router(service: Any) -> APIRouter:
    router = APIRouter()

    @router.post(WEBHOOK_PATH)
    async def dpichecker_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        if not raw_body:
            return _reply('empty_body', status.HTTP_400_BAD_REQUEST)
        if len(raw_body) > MAX_BODY_BYTES:
            logger.warning('DPI//CHECKER webhook: тело слишком большое', size=len(raw_body))
            return _reply('payload_too_large', status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        header = request.headers.get('X-DPIChecker-Signature') or ''
        if not header:
            return _reply('missing_signature', status.HTTP_401_UNAUTHORIZED)
        try:
            secret = await service.webhook_secret()
            valid = _signature_ok(raw_body, header, secret)
            if not valid:  # секрет мог смениться (новый ключ API) — перечитать один раз
                valid = _signature_ok(raw_body, header, await service.webhook_secret(refresh=True))
        except Exception as error:
            logger.warning('DPI//CHECKER webhook: секрет подписи не получен', error=str(error)[:200])
            return _reply('secret_unavailable', status.HTTP_503_SERVICE_UNAVAILABLE)
        if not valid:
            logger.warning('DPI//CHECKER webhook: неверная подпись')
            return _reply('invalid_signature', status.HTTP_401_UNAUTHORIZED)
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _reply('invalid_json', status.HTTP_400_BAD_REQUEST)
        event = request.headers.get('X-DPIChecker-Event') or str(payload.get('event') or '')
        try:
            delivery_id = int(request.headers.get('X-DPIChecker-Delivery') or payload.get('delivery_id') or 0)
        except ValueError:
            delivery_id = 0
        await service.handle_webhook(event=event, delivery_id=delivery_id, payload=payload)
        return JSONResponse({'status': 'ok'})

    return router
