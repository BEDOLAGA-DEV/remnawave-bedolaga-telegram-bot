"""Единая точка создания клиента Redis.

redis-py ≥ 8 по умолчанию (``maint_notifications_config.enabled="auto"``) на каждом
новом соединении шлёт ``CLIENT MAINT_NOTIFICATIONS`` — уведомления о плановых
работах Redis Enterprise. Обычный Redis команду не знает, и библиотека на каждое
соединение пишет в лог «Failed to enable maintenance notifications». Бот с Redis
Enterprise не работает, поэтому механизм выключен явно.

Аргумент принимает только асинхронное соединение redis-py ≥ 8.1.0 — отсюда
нижняя граница в pyproject. На 7.1.1–8.0.1 ``from_url`` его молча складывает в
параметры пула, а падает уже первое создание соединения, то есть Redis у бота
не работает вообще.

Повторы подключения задаются здесь же: по умолчанию у redis-py их ноль
(``Retry(NoBackoff(), 0)``), и разовая заминка на старте контейнера — гонка за
резолвером имён, пока поднимается всё остальное — сразу становится ошибкой в
логе и пропущенным тактом фоновых сервисов. Повтор охватывает только
установку соединения (``AbstractConnection.connect``), уже отправленные
команды заново не выполняются.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis

# Именно асинхронный Retry: у redis-py их два, и синхронный здесь не срабатывает —
# ``AbstractConnection.connect`` ждёт корутину, повтор молча не происходит.
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff

from app.config import settings


try:
    from redis.maint_notifications import MaintNotificationsConfig
except ImportError:  # redis-py < 6.x: механизма нет, выключать нечего
    MaintNotificationsConfig = None  # type: ignore[assignment,misc]


# Три попытки с быстрым нарастанием: заминка на старте занимает доли секунды,
# а недоступный Redis не должен задерживать вызывающего надолго.
_CONNECT_RETRY = Retry(ExponentialWithJitterBackoff(base=0.05, cap=1.0), retries=3)


def create_redis(url: str | None = None, **kwargs: Any) -> redis.Redis:
    """Клиент с пулом соединений к ``url`` (по умолчанию ``settings.REDIS_URL``)."""
    if MaintNotificationsConfig is not None:
        kwargs.setdefault('maint_notifications_config', MaintNotificationsConfig(enabled=False))
    kwargs.setdefault('retry', _CONNECT_RETRY)
    return redis.from_url(url or settings.REDIS_URL, **kwargs)
