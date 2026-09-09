"""Контракт клиента Redis: аргументы должны приниматься, подключение — повторяться.

Две жалобы из «Багов», обе про Redis на старте контейнера.

1. ``TypeError: AbstractConnection.__init__() got an unexpected keyword argument
   'maint_notifications_config'``. Мы выключаем уведомления о плановых работах
   Redis Enterprise, передавая аргумент в клиент. Синхронный класс его принимает
   с 7.1.1, а АСИНХРОННЫЙ — только с 8.1.0; на 7.1.1–8.0.1 ``from_url`` проходит
   молча (аргумент просто складывается в параметры пула), и падает уже первое
   создание соединения — то есть Redis у бота не работает вообще. Нижняя граница
   в pyproject такие версии пропускала.

2. ``TimeoutError`` при первом обращении к Redis сразу после старта. У клиента
   по умолчанию ``Retry(NoBackoff(), 0)`` — ни одной повторной попытки, поэтому
   разовая заминка подключения (старт контейнера, гонка за резолвером) сразу
   становится ошибкой в логе и пропущенным тактом фоновой очереди.
"""

from __future__ import annotations

import inspect

import pytest

from tests.fixtures.real_redis import ensure_real_redis


def _injected_kwargs(create_redis) -> dict:
    """Аргументы, которые ``create_redis`` добавляет от себя."""
    client = create_redis('redis://localhost:6379/0')
    pool_kwargs = dict(client.connection_pool.connection_kwargs)
    for connection_only in ('host', 'port', 'db', 'username', 'password'):
        pool_kwargs.pop(connection_only, None)
    return pool_kwargs


@pytest.fixture
def redis_client_module(monkeypatch):
    return ensure_real_redis(monkeypatch)


def test_every_injected_kwarg_is_accepted_by_async_connection(redis_client_module):
    """Каждый добавленный аргумент обязан приниматься асинхронным соединением.

    ``from_url`` не проверяет аргументы — он складывает их в параметры пула,
    и несовместимый аргумент выстреливает только при первом создании
    соединения, то есть уже в бою.
    """
    import redis.asyncio as redis_asyncio

    accepted = set(inspect.signature(redis_asyncio.connection.AbstractConnection.__init__).parameters)
    unsupported = sorted(key for key in _injected_kwargs(redis_client_module.create_redis) if key not in accepted)

    assert not unsupported, (
        f'установленная redis-py не принимает в асинхронном соединении: {unsupported}. '
        'Поднимите нижнюю границу redis в pyproject или перестаньте передавать аргумент.'
    )


def test_connection_is_actually_creatable(redis_client_module):
    """Соединение создаётся (не подключается) — ровно там падал TypeError."""
    import redis.asyncio as redis_asyncio

    client = redis_client_module.create_redis('redis://localhost:6379/0')
    connection = client.connection_pool.make_connection()

    assert isinstance(connection, redis_asyncio.connection.AbstractConnection)


def test_connect_is_retried(redis_client_module):
    """У подключения есть повторы: разовая заминка на старте не должна быть ошибкой."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    client = redis_client_module.create_redis('redis://localhost:6379/0')
    connection = client.connection_pool.make_connection()
    retry = connection.retry

    assert retry is not None
    assert getattr(retry, '_retries', 0) > 0, 'повторов подключения нет'
    assert RedisTimeoutError in retry._supported_errors


@pytest.mark.asyncio
async def test_transient_connect_failure_is_retried(monkeypatch, redis_client_module):
    """Первая попытка упала по таймауту — вторая доводит подключение до конца."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    client = redis_client_module.create_redis('redis://localhost:6379/0')
    connection = client.connection_pool.make_connection()

    attempts = {'count': 0}

    async def flaky(*args, **kwargs):
        attempts['count'] += 1
        if attempts['count'] == 1:
            raise RedisTimeoutError('Timeout connecting to server')

    monkeypatch.setattr(connection, 'connect_check_health', flaky)
    monkeypatch.setattr(connection, 'disconnect', lambda *a, **kw: _noop())

    await connection.connect()

    assert attempts['count'] == 2, 'повторной попытки не было'


async def _noop() -> None:
    return None
