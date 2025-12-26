from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Optional

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.webhook import (
    get_active_webhooks_for_event,
    record_webhook_delivery,
    update_webhook_stats,
)

logger = logging.getLogger(__name__)


class WebhookService:
    """Сервис для отправки webhooks."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

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
            secret.encode("utf-8"),
            payload.encode("utf-8"),
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
            logger.debug("No active webhooks for event type: %s", event_type)
            return

        tasks = [
            self._deliver_webhook(db, webhook, event_type, payload)
            for webhook in webhooks
        ]

        # Отправляем все webhooks параллельно
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver_webhook(
        self,
        db: AsyncSession,
        webhook: Any,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Доставить webhook одному получателю."""
        payload_json = json.dumps(payload, default=str, ensure_ascii=False)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": event_type,
            "X-Webhook-Id": str(webhook.id),
        }

        # Добавляем подпись, если есть секрет
        if webhook.secret:
            signature = self._sign_payload(payload_json, webhook.secret)
            headers["X-Webhook-Signature"] = f"sha256={signature}"

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
                    response_body = response_body[:1000] + "... (truncated)"

                status = "success" if 200 <= response.status < 300 else "failed"
                error_message = None
                if status == "failed":
                    error_message = f"HTTP {response.status}: {response_body[:500]}"

                await record_webhook_delivery(
                    db,
                    webhook_id=webhook.id,
                    event_type=event_type,
                    payload=payload,
                    status=status,
                    response_status=response.status,
                    response_body=response_body,
                    error_message=error_message,
                )

                await update_webhook_stats(db, webhook, status == "success")

                if status == "success":
                    logger.info(
                        "Webhook %s delivered successfully to %s",
                        webhook.id,
                        webhook.url,
                    )
                else:
                    logger.warning(
                        "Webhook %s delivery failed: %s",
                        webhook.id,
                        error_message,
                    )

        except asyncio.TimeoutError:
            error_message = "Request timeout"
            await record_webhook_delivery(
                db,
                webhook_id=webhook.id,
                event_type=event_type,
                payload=payload,
                status="failed",
                error_message=error_message,
            )
            await update_webhook_stats(db, webhook, False)
            logger.warning("Webhook %s delivery timeout: %s", webhook.id, webhook.url)

        except Exception as error:
            error_message = str(error)
            await record_webhook_delivery(
                db,
                webhook_id=webhook.id,
                event_type=event_type,
                payload=payload,
                status="failed",
                error_message=error_message,
            )
            await update_webhook_stats(db, webhook, False)
            logger.exception(
                "Failed to deliver webhook %s to %s: %s",
                webhook.id,
                webhook.url,
                error,
            )


# Глобальный экземпляр сервиса
webhook_service = WebhookService()

