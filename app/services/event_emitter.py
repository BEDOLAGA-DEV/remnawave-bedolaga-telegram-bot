from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.webhook_service import webhook_service

logger = logging.getLogger(__name__)


class EventEmitter:
    """Event emitter для отслеживания и распространения событий системы."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable]] = {}
        self._websocket_connections: set[Any] = set()

    def on(self, event_type: str, callback: Callable) -> None:
        """Подписаться на событие."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)

    def off(self, event_type: str, callback: Callable) -> None:
        """Отписаться от события."""
        if event_type in self._listeners:
            try:
                self._listeners[event_type].remove(callback)
            except ValueError:
                pass

    def register_websocket(self, websocket: Any) -> None:
        """Зарегистрировать WebSocket подключение."""
        self._websocket_connections.add(websocket)
        logger.debug("WebSocket connection registered. Total: %d", len(self._websocket_connections))

    def unregister_websocket(self, websocket: Any) -> None:
        """Отменить регистрацию WebSocket подключения."""
        self._websocket_connections.discard(websocket)
        logger.debug("WebSocket connection unregistered. Total: %d", len(self._websocket_connections))

    async def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        db: Optional[AsyncSession] = None,
    ) -> None:
        """Отправить событие всем подписчикам."""
        event_data = {
            "type": event_type,
            "payload": payload,
            "timestamp": str(datetime.utcnow()),
        }

        # Вызываем локальные слушатели
        if event_type in self._listeners:
            for callback in self._listeners[event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event_data)
                    else:
                        callback(event_data)
                except Exception as error:
                    logger.exception("Error in event listener for %s: %s", event_type, error)

        # Отправляем через WebSocket
        await self._broadcast_to_websockets(event_data)

        # Отправляем webhooks
        if db:
            await webhook_service.send_webhook(db, event_type, payload)

    async def _broadcast_to_websockets(self, event_data: dict[str, Any]) -> None:
        """Отправить событие всем подключенным WebSocket клиентам."""
        if not self._websocket_connections:
            return

        disconnected = set()
        message = json.dumps(event_data, default=str, ensure_ascii=False)

        for ws in self._websocket_connections:
            try:
                await ws.send_text(message)
            except Exception as error:
                logger.warning("Failed to send WebSocket message: %s", error)
                disconnected.add(ws)

        # Удаляем отключенные соединения
        for ws in disconnected:
            self.unregister_websocket(ws)


# Глобальный экземпляр event emitter
event_emitter = EventEmitter()

