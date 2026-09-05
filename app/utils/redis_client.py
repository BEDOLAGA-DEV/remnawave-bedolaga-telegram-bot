"""Единая точка создания клиента Redis.

redis-py ≥ 8 по умолчанию (``maint_notifications_config.enabled="auto"``) на каждом
новом соединении шлёт ``CLIENT MAINT_NOTIFICATIONS`` — уведомления о плановых
работах Redis Enterprise. Обычный Redis команду не знает, и библиотека на каждое
соединение пишет в лог «Failed to enable maintenance notifications». Бот с Redis
Enterprise не работает, поэтому механизм выключен явно.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis

from app.config import settings


try:
    from redis.maint_notifications import MaintNotificationsConfig
except ImportError:  # redis-py < 6.x: механизма нет, выключать нечего
    MaintNotificationsConfig = None  # type: ignore[assignment,misc]


def create_redis(url: str | None = None, **kwargs: Any) -> redis.Redis:
    """Клиент с пулом соединений к ``url`` (по умолчанию ``settings.REDIS_URL``)."""
    if MaintNotificationsConfig is not None:
        kwargs.setdefault('maint_notifications_config', MaintNotificationsConfig(enabled=False))
    return redis.from_url(url or settings.REDIS_URL, **kwargs)
