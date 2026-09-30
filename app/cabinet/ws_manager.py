"""Менеджер WebSocket-подключений кабинета — без зависимостей от CRUD.

Уведомления в кабинет шлют сервисы, стоящие на путях удаления подписки
(автопродления Cashera и др.). Держать менеджер в routes.websocket, который импортирует
crud.user, значило замыкать кольца импортов через каждого отправителя.
"""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import WebSocket


logger = structlog.get_logger(__name__)


class CabinetConnectionManager:
    """Менеджер WebSocket подключений для кабинета."""

    def __init__(self):
        # user_id -> set of websocket connections
        self._user_connections: dict[int, set[WebSocket]] = {}
        # admin user_ids -> set of websocket connections
        self._admin_connections: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int, is_admin: bool) -> None:
        """Зарегистрировать подключение."""
        async with self._lock:
            if user_id not in self._user_connections:
                self._user_connections[user_id] = set()
            self._user_connections[user_id].add(websocket)

            if is_admin:
                if user_id not in self._admin_connections:
                    self._admin_connections[user_id] = set()
                self._admin_connections[user_id].add(websocket)

        logger.debug(
            'Cabinet WS connected: user_id is_admin total_users',
            user_id=user_id,
            is_admin=is_admin,
            user_connections_count=len(self._user_connections),
        )

    async def disconnect(self, websocket: WebSocket, user_id: int) -> None:
        """Отменить регистрацию подключения."""
        async with self._lock:
            if user_id in self._user_connections:
                self._user_connections[user_id].discard(websocket)
                if not self._user_connections[user_id]:
                    del self._user_connections[user_id]

            if user_id in self._admin_connections:
                self._admin_connections[user_id].discard(websocket)
                if not self._admin_connections[user_id]:
                    del self._admin_connections[user_id]

        logger.debug('Cabinet WS disconnected: user_id', user_id=user_id)

    async def send_to_user(self, user_id: int, message: dict) -> None:
        """Отправить сообщение конкретному пользователю."""
        # Snapshot connections under the lock to avoid mutation during iteration
        async with self._lock:
            connections = list(self._user_connections.get(user_id, set()))

        if not connections:
            return

        disconnected = set()
        data = json.dumps(message, default=str, ensure_ascii=False)

        for ws in connections:
            try:
                await ws.send_text(data)
            except Exception as e:
                logger.warning('Failed to send to user', user_id=user_id, e=e)
                disconnected.add(ws)

        # Cleanup disconnected
        if disconnected:
            async with self._lock:
                for ws in disconnected:
                    self._user_connections.get(user_id, set()).discard(ws)

    async def send_to_admins(self, message: dict) -> None:
        """Отправить сообщение всем админам."""
        # Snapshot connections under the lock to avoid mutation during iteration
        async with self._lock:
            if not self._admin_connections:
                return
            # Create a snapshot: list of (user_id, list of websockets)
            admin_snapshot = [(user_id, list(connections)) for user_id, connections in self._admin_connections.items()]

        data = json.dumps(message, default=str, ensure_ascii=False)
        disconnected_by_user: dict[int, set[WebSocket]] = {}

        for user_id, connections in admin_snapshot:
            for ws in connections:
                try:
                    await ws.send_text(data)
                except Exception as e:
                    logger.warning('Failed to send to admin', user_id=user_id, e=e)
                    if user_id not in disconnected_by_user:
                        disconnected_by_user[user_id] = set()
                    disconnected_by_user[user_id].add(ws)

        # Cleanup disconnected
        if disconnected_by_user:
            async with self._lock:
                for user_id, ws_set in disconnected_by_user.items():
                    for ws in ws_set:
                        self._admin_connections.get(user_id, set()).discard(ws)


# Глобальный менеджер подключений
cabinet_ws_manager = CabinetConnectionManager()
