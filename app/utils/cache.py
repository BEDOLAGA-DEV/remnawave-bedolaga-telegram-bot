import json
from datetime import timedelta
from typing import Any

import redis.asyncio as redis
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class CacheService:
    def __init__(self):
        self.redis_client: redis.Redis | None = None
        self._connected = False

    async def connect(self):
        try:
            self.redis_client = redis.from_url(settings.REDIS_URL)
            await self.redis_client.ping()
            self._connected = True
            logger.info('✅ Подключение к Redis кешу установлено')
        except Exception as e:
            logger.warning('⚠️ Не удалось подключиться к Redis', error=e)
            self._connected = False

    async def disconnect(self):
        if self.redis_client:
            await self.redis_client.close()
            self._connected = False

    async def get(self, key: str) -> Any | None:
        if not self._connected:
            return None

        try:
            value = await self.redis_client.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error('Ошибка получения из кеша', key=key, error=e)
            return None

    async def set(self, key: str, value: Any, expire: int | timedelta = None) -> bool:
        if not self._connected:
            return False

        try:
            serialized_value = json.dumps(value, default=str)

            if isinstance(expire, timedelta):
                expire = int(expire.total_seconds())

            await self.redis_client.set(key, serialized_value, ex=expire)
            return True
        except Exception as e:
            logger.error('Ошибка записи в кеш', key=key, error=e)
            return False

    async def setnx(self, key: str, value: Any, expire: int | timedelta = None) -> bool:
        """Атомарная операция SET IF NOT EXISTS.

        Устанавливает значение только если ключ не существует.
        Возвращает True если значение было установлено, False если ключ уже существовал.
        """
        if not self._connected:
            return False

        try:
            serialized_value = json.dumps(value, default=str)

            if isinstance(expire, timedelta):
                expire = int(expire.total_seconds())

            # SET с NX возвращает True если установлено, None если ключ существует
            result = await self.redis_client.set(key, serialized_value, ex=expire, nx=True)
            return result is True
        except Exception as e:
            logger.error('Ошибка setnx в кеш', key=key, error=e)
            return False

    async def getdel(self, key: str) -> Any | None:
        """Atomically get and delete a key (Redis GETDEL).

        Returns the deserialized value if it existed, None otherwise.
        """
        if not self._connected:
            return None

        try:
            value = await self.redis_client.getdel(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error('Ошибка атомарного getdel из кеша', key=key, error=e)
            return None

    async def delete(self, key: str) -> bool:
        if not self._connected:
            return False

        try:
            deleted = await self.redis_client.delete(key)
            return deleted > 0
        except Exception as e:
            logger.error('Ошибка удаления из кеша', key=key, error=e)
            return False

    async def delete_pattern(self, pattern: str) -> int:
        if not self._connected:
            return 0

        try:
            keys = await self.redis_client.keys(pattern)
            if not keys:
                return 0

            deleted = await self.redis_client.delete(*keys)
            return int(deleted)
        except Exception as e:
            logger.error('Ошибка удаления ключей по шаблону', pattern=pattern, error=e)
            return 0

    async def exists(self, key: str) -> bool:
        if not self._connected:
            return False

        try:
            return await self.redis_client.exists(key)
        except Exception as e:
            logger.error('Ошибка проверки существования в кеше', key=key, error=e)
            return False

    async def expire(self, key: str, seconds: int) -> bool:
        if not self._connected:
            return False

        try:
            return await self.redis_client.expire(key, seconds)
        except Exception as e:
            logger.error('Ошибка установки TTL для', key=key, error=e)
            return False

    async def get_keys(self, pattern: str = '*') -> list:
        if not self._connected:
            return []

        try:
            keys = await self.redis_client.keys(pattern)
            return [key.decode() if isinstance(key, bytes) else key for key in keys]
        except Exception as e:
            logger.error('Ошибка получения ключей по паттерну', pattern=pattern, error=e)
            return []

    async def flush_all(self) -> bool:
        if not self._connected:
            return False

        try:
            await self.redis_client.flushall()
            logger.info('🗑️ Кеш полностью очищен')
            return True
        except Exception as e:
            logger.error('Ошибка очистки кеша', error=e)
            return False

    async def increment(self, key: str, amount: int = 1) -> int | None:
        if not self._connected:
            return None

        try:
            return await self.redis_client.incrby(key, amount)
        except Exception as e:
            logger.error('Ошибка инкремента', key=key, error=e)
            return None

    async def set_hash(self, name: str, mapping: dict, expire: int = None) -> bool:
        if not self._connected:
            return False

        try:
            await self.redis_client.hset(name, mapping=mapping)
            if expire:
                await self.redis_client.expire(name, expire)
            return True
        except Exception as e:
            logger.error('Ошибка записи хеша', name=name, error=e)
            return False

    async def get_hash(self, name: str, key: str = None) -> dict | str | None:
        if not self._connected:
            return None

        try:
            if key:
                value = await self.redis_client.hget(name, key)
                return value.decode() if value else None
            hash_data = await self.redis_client.hgetall(name)
            return {k.decode(): v.decode() for k, v in hash_data.items()}
        except Exception as e:
            logger.error('Ошибка получения хеша', name=name, error=e)
            return None

    async def lpush(self, key: str, value: Any) -> bool:
        """Добавить элемент в начало списка (очереди)."""
        if not self._connected:
            return False

        try:
            serialized = json.dumps(value, default=str)
            await self.redis_client.lpush(key, serialized)
            return True
        except Exception as e:
            logger.error('Ошибка добавления в очередь', key=key, error=e)
            return False

    async def rpop(self, key: str) -> Any | None:
        """Извлечь элемент из конца списка (FIFO очередь)."""
        if not self._connected:
            return None

        try:
            value = await self.redis_client.rpop(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error('Ошибка извлечения из очереди', key=key, error=e)
            return None

    async def llen(self, key: str) -> int:
        """Получить длину списка (очереди)."""
        if not self._connected:
            return 0

        try:
            return await self.redis_client.llen(key)
        except Exception as e:
            logger.error('Ошибка получения длины очереди', key=key, error=e)
            return 0

    async def lrange(self, key: str, start: int = 0, end: int = -1) -> list:
        """Получить элементы списка без удаления."""
        if not self._connected:
            return []

        try:
            items = await self.redis_client.lrange(key, start, end)
            return [json.loads(item) for item in items]
        except Exception as e:
            logger.error('Ошибка чтения очереди', key=key, error=e)
            return []


cache = CacheService()


def cache_key(*parts) -> str:
    return ':'.join(str(part) for part in parts)


async def cached_function(key: str, expire: int = 300):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            cache_result = await cache.get(key)
            if cache_result is not None:
                return cache_result

            result = await func(*args, **kwargs)
            await cache.set(key, result, expire)
            return result

        return wrapper

    return decorator


class UserCache:
    @staticmethod
    async def get_user_data(user_id: int) -> dict | None:
        key = cache_key('user', user_id)
        return await cache.get(key)

    @staticmethod
    async def set_user_data(user_id: int, data: dict, expire: int = 3600) -> bool:
        key = cache_key('user', user_id)
        return await cache.set(key, data, expire)

    @staticmethod
    async def delete_user_data(user_id: int) -> bool:
        key = cache_key('user', user_id)
        return await cache.delete(key)

    @staticmethod
    async def get_user_session(user_id: int, session_key: str) -> Any | None:
        key = cache_key('session', user_id, session_key)
        return await cache.get(key)

    @staticmethod
    async def set_user_session(user_id: int, session_key: str, data: Any, expire: int = 1800) -> bool:
        key = cache_key('session', user_id, session_key)
        return await cache.set(key, data, expire)

    @staticmethod
    async def delete_user_session(user_id: int, session_key: str) -> bool:
        key = cache_key('session', user_id, session_key)
        return await cache.delete(key)


class SystemCache:
    @staticmethod
    async def get_system_stats() -> dict | None:
        return await cache.get('system:stats')

    @staticmethod
    async def set_system_stats(stats: dict, expire: int = 300) -> bool:
        return await cache.set('system:stats', stats, expire)

    @staticmethod
    async def get_nodes_status() -> list | None:
        return await cache.get('remnawave:nodes')

    @staticmethod
    async def set_nodes_status(nodes: list, expire: int = 60) -> bool:
        return await cache.set('remnawave:nodes', nodes, expire)

    @staticmethod
    async def get_daily_stats(date: str) -> dict | None:
        key = cache_key('stats', 'daily', date)
        return await cache.get(key)

    @staticmethod
    async def set_daily_stats(date: str, stats: dict) -> bool:
        key = cache_key('stats', 'daily', date)
        return await cache.set(key, stats, 86400)  # 24 часа


class RateLimitCache:
    @staticmethod
    async def is_rate_limited(user_id: int, action: str, limit: int, window: int) -> bool:
        key = cache_key('rate_limit', user_id, action)
        current = await cache.get(key)

        if current is None:
            await cache.set(key, 1, window)
            return False

        if current >= limit:
            return True

        await cache.increment(key)
        return False

    @staticmethod
    async def reset_rate_limit(user_id: int, action: str) -> bool:
        key = cache_key('rate_limit', user_id, action)
        return await cache.delete(key)


class ChannelSubCache:
    """Cache for user channel subscription statuses.

    Redis keys:
    - channel_sub:{telegram_id}:{channel_id} -> "1" or "0" (TTL 600s)
    - required_channels:active -> JSON list of active channels (TTL 60s)
    """

    SUB_TTL = 600  # 10 min -- individual user subscription status
    CHANNELS_TTL = 60  # 1 min -- list of required channels

    @staticmethod
    async def get_sub_status(telegram_id: int, channel_id: str) -> bool | None:
        """Get subscription status from cache. None = cache miss."""
        key = cache_key('channel_sub', telegram_id, channel_id)
        result = await cache.get(key)
        if result is None:
            return None
        return result == 1

    @staticmethod
    async def get_sub_statuses(telegram_id: int, channel_ids: list[str]) -> dict[str, bool | None]:
        """Batch-fetch subscription statuses via Redis MGET (single round-trip).

        Returns {channel_id: True/False/None} where None = cache miss.
        Falls back to sequential gets if Redis pipeline is unavailable.
        """
        if not channel_ids:
            return {}

        if not cache._connected or cache.redis_client is None:
            return dict.fromkeys(channel_ids, None)

        keys = [cache_key('channel_sub', telegram_id, ch_id) for ch_id in channel_ids]
        try:
            raw_values = await cache.redis_client.mget(keys)
        except Exception as e:
            logger.warning('Redis MGET failed, falling back to sequential', error=str(e))
            result: dict[str, bool | None] = {}
            for ch_id in channel_ids:
                result[ch_id] = await ChannelSubCache.get_sub_status(telegram_id, ch_id)
            return result

        statuses: dict[str, bool | None] = {}
        for ch_id, raw in zip(channel_ids, raw_values, strict=True):
            if raw is None:
                statuses[ch_id] = None
            else:
                try:
                    parsed = json.loads(raw)
                    statuses[ch_id] = parsed == 1
                except (ValueError, TypeError):
                    statuses[ch_id] = None
        return statuses

    @staticmethod
    async def set_sub_status(telegram_id: int, channel_id: str, is_member: bool) -> None:
        key = cache_key('channel_sub', telegram_id, channel_id)
        await cache.set(key, 1 if is_member else 0, expire=ChannelSubCache.SUB_TTL)

    @staticmethod
    async def invalidate_sub(telegram_id: int, channel_id: str) -> None:
        key = cache_key('channel_sub', telegram_id, channel_id)
        await cache.delete(key)

    @staticmethod
    async def invalidate_user_channels(telegram_id: int, channel_ids: list[str]) -> None:
        """Invalidate specific channel keys for a user using single Redis DELETE.

        Uses multi-key DELETE (O(K)) instead of delete_pattern() which uses KEYS (O(N)).
        At 100k users * 5 channels = 500k keys, KEYS would block Redis for seconds.
        """
        if not channel_ids or not cache._connected or not cache.redis_client:
            return
        keys = [cache_key('channel_sub', telegram_id, ch_id) for ch_id in channel_ids]
        try:
            await cache.redis_client.delete(*keys)
        except Exception as e:
            logger.warning('Failed to invalidate user channel cache', telegram_id=telegram_id, error=e)

    @staticmethod
    async def get_required_channels() -> list[dict] | None:
        """Get the list of required channels from cache."""
        return await cache.get('required_channels:active')

    @staticmethod
    async def set_required_channels(channels: list[dict]) -> None:
        await cache.set('required_channels:active', channels, expire=ChannelSubCache.CHANNELS_TTL)

    @staticmethod
    async def invalidate_channels() -> None:
        await cache.delete('required_channels:active')
