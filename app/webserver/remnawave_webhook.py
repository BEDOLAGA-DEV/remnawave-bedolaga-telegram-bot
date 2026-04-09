"""
FastAPI router for receiving incoming webhooks from RemnaWave backend.

Handles HMAC-SHA256 signature verification, payload parsing, and
event dispatch to RemnaWaveWebhookService.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import structlog
from aiogram import Bot
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


logger = structlog.get_logger(__name__)

# Max accepted webhook payload size (64 KB) to prevent memory exhaustion DoS
_MAX_BODY_SIZE = 64 * 1024


def _verify_signature(raw_body: bytes, received_signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature from RemnaWave backend."""
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_signature)


def create_remnawave_webhook_router(bot: Bot) -> APIRouter:
    router = APIRouter()
    webhook_service = RemnaWaveWebhookService(bot)
    webhook_path = settings.REMNAWAVE_WEBHOOK_PATH

    @router.get(webhook_path)
    async def remnawave_webhook_health() -> JSONResponse:
        return JSONResponse(
            {
                'status': 'ok',
                'service': 'remnawave_webhook',
                'enabled': settings.is_remnawave_webhook_enabled(),
            }
        )

    @router.post(webhook_path)
    async def remnawave_webhook(request: Request) -> JSONResponse:
        raw_body = await request.body()
        if not raw_body:
            return JSONResponse(
                {'status': 'error', 'reason': 'empty_body'},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if len(raw_body) > _MAX_BODY_SIZE:
            logger.warning('RemnaWave webhook: payload too large (bytes)', raw_body_count=len(raw_body))
            return JSONResponse(
                {'status': 'error', 'reason': 'payload_too_large'},
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        # Verify HMAC-SHA256 signature (always required)
        secret = settings.REMNAWAVE_WEBHOOK_SECRET
        if not secret:
            logger.error('RemnaWave webhook: secret not configured, rejecting request')
            return JSONResponse(
                {'status': 'error', 'reason': 'webhook_not_configured'},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        signature = request.headers.get('X-Remnawave-Signature') or ''
        if not signature:
            logger.warning('RemnaWave webhook: missing signature header')
            return JSONResponse(
                {'status': 'error', 'reason': 'missing_signature'},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        if not _verify_signature(raw_body, signature, secret):
            logger.warning('RemnaWave webhook: invalid signature')
            return JSONResponse(
                {'status': 'error', 'reason': 'invalid_signature'},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        # Parse JSON payload
        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except json.JSONDecodeError:
            return JSONResponse(
                {'status': 'error', 'reason': 'invalid_json'},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # Extract and validate event info. Recent RemnaWave payloads send only
        # the fully-qualified event name (for example "user.modified") without
        # a separate top-level scope field.
        event = str(payload.get('event', '') or '').strip()
        scope = str(payload.get('scope', '') or '').strip()
        data = payload.get('data')

        if not event:
            logger.warning('RemnaWave webhook: missing event')
            return JSONResponse(
                {'status': 'error', 'reason': 'missing_event'},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if not scope and '.' in event:
            scope = event.split('.', 1)[0]

        if not isinstance(data, dict):
            data = {}

        # Inject meta into data so handlers can access it via data.get('_meta')
        meta = payload.get('meta')
        if isinstance(meta, dict):
            data['_meta'] = meta

        # RemnaWave sends event as full qualified name (e.g. "user.modified"),
        # so we use event directly instead of concatenating scope + event.
        event_name = event
        logger.info('RemnaWave webhook received: scope event', scope=scope, event_name=event_name)

        # Process event — return 200 to prevent retries for application-level errors.
        # Only return non-200 for infrastructure failures (DB unavailable).
        # Admin-only events (node/service/crm) don't need a DB session.
        # Dual events (admin + user, e.g. torrent_blocker.report) need DB for user handler.
        if webhook_service.is_admin_event(event_name) and not webhook_service.needs_db_session(event_name):
            try:
                processed = await webhook_service.process_event(None, event_name, data)
                return JSONResponse({'status': 'ok', 'processed': processed})
            except Exception:
                logger.exception('RemnaWave webhook processing error for event', event_name=event_name)
                return JSONResponse({'status': 'ok', 'processed': False})

        # User events and dual events require a DB session
        try:
            async with AsyncSessionLocal() as db:
                try:
                    processed = await webhook_service.process_event(db, event_name, data)
                    await db.commit()
                    return JSONResponse({'status': 'ok', 'processed': processed})
                except Exception:
                    await db.rollback()
                    logger.exception('RemnaWave webhook processing error for event', event_name=event_name)
                    return JSONResponse({'status': 'ok', 'processed': False})
        except Exception:
            logger.error('RemnaWave webhook: failed to get database session')
            return JSONResponse(
                {'status': 'error', 'reason': 'database_unavailable'},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    return router
