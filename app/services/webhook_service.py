from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import aiohttp
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.webhook import (
    get_active_webhooks_for_event,
    record_webhook_delivery,
    update_webhook_stats,
)


logger = structlog.get_logger(__name__)


@dataclass
class DeliveryResult:
    """Результат доставки webhook."""

    webhook: Any
    event_type: str
    payload: dict[str, Any]
    status: str
    response_status: int | None = None
    response_body: str | None = None
    error_message: str | None = None


class WebhookService:
    """Сервис для отправки webhooks."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать HTTP сессию."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Закрыть HTTP сессию."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign_payload(self, payload: str, secret: str) -> str:
        """Подписать payload с помощью секрета."""
        return hmac.new(
            secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    async def send_webhook(
        self,
        db: AsyncSession,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Отправить webhook для события."""
        webhooks = await get_active_webhooks_for_event(db, event_type)

        if not webhooks:
            logger.debug('No active webhooks for event type', event_type=event_type)
            return

        # Выполняем HTTP запросы параллельно (без операций с БД)
        tasks = [self._deliver_webhook_http(webhook, event_type, payload) for webhook in webhooks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Записываем результаты в БД последовательно (избегаем concurrent session access)
        for result in results:
            if isinstance(result, Exception):
                logger.exception('Unexpected error during webhook delivery', result=result)
                continue
            if isinstance(result, DeliveryResult):
                await self._record_result(db, result)

    async def _deliver_webhook_http(
        self,
        webhook: Any,
        event_type: str,
        payload: dict[str, Any],
    ) -> DeliveryResult:
        """Выполнить HTTP доставку webhook (без операций с БД)."""
        payload_json = json.dumps(payload, default=str, ensure_ascii=False)
        headers = {
            'Content-Type': 'application/json',
            'X-Webhook-Event': event_type,
            'X-Webhook-Id': str(webhook.id),
        }

        # Добавляем подпись, если есть секрет
        if webhook.secret:
            signature = self._sign_payload(payload_json, webhook.secret)
            headers['X-Webhook-Signature'] = f'sha256={signature}'

        try:
            session = await self._get_session()
            async with session.post(
                webhook.url,
                data=payload_json,
                headers=headers,
            ) as response:
                response_body = await response.text()
                # Ограничиваем размер ответа для хранения
                if len(response_body) > 1000:
                    response_body = response_body[:1000] + '... (truncated)'

                status = 'success' if 200 <= response.status < 300 else 'failed'
                error_message = None
                if status == 'failed':
                    error_message = f'HTTP {response.status}: {response_body[:500]}'

                return DeliveryResult(
                    webhook=webhook,
                    event_type=event_type,
                    payload=payload,
                    status=status,
                    response_status=response.status,
                    response_body=response_body,
                    error_message=error_message,
                )

        except TimeoutError:
            return DeliveryResult(
                webhook=webhook,
                event_type=event_type,
                payload=payload,
                status='failed',
                error_message='Request timeout',
            )

        except Exception as error:
            return DeliveryResult(
                webhook=webhook,
                event_type=event_type,
                payload=payload,
                status='failed',
                error_message=str(error),
            )

    async def _record_result(self, db: AsyncSession, result: DeliveryResult) -> None:
        """Записать результат доставки в БД (последовательно)."""
        try:
            await record_webhook_delivery(
                db,
                webhook_id=result.webhook.id,
                event_type=result.event_type,
                payload=result.payload,
                status=result.status,
                response_status=result.response_status,
                response_body=result.response_body,
                error_message=result.error_message,
            )

            await update_webhook_stats(db, result.webhook, result.status == 'success')

            if result.status == 'success':
                logger.info('Webhook delivered successfully', id=result.webhook.id, url=result.webhook.url)
            else:
                logger.warning('Webhook delivery failed', id=result.webhook.id, error_message=result.error_message)
        except Exception as error:
            logger.exception('Failed to record webhook delivery result', id=result.webhook.id, error=error)


# Глобальный экземпляр сервиса
webhook_service = WebhookService()
