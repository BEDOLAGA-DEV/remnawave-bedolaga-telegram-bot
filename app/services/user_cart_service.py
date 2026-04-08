import json
from typing import Any

import redis.asyncio as redis
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class UserCartService:
    """
    Сервис для работы с корзиной пользователя через Redis.

    Использует ленивую инициализацию Redis-клиента для graceful fallback
    при недоступности Redis.
    """

    def __init__(self):
        self._redis_client: redis.Redis | None = None
        self._initialized: bool = False

    def _get_redis_client(self) -> redis.Redis | None:
        """Ленивая инициализация Redis клиента."""
        if self._initialized:
            return self._redis_client

        try:
            self._redis_client = redis.from_url(settings.REDIS_URL)
            self._initialized = True
            logger.debug('Redis клиент для корзины инициализирован')
        except Exception as e:
            logger.warning('Не удалось подключиться к Redis для корзины', error=e)
            self._redis_client = None
            self._initialized = True

        return self._redis_client

    async def save_user_cart(self, user_id: int, cart_data: dict[str, Any], ttl: int | None = None) -> bool:
        """
        Сохранить корзину пользователя в Redis.

        When ``cart_data`` contains a ``subscription_id``, the data is
        **also** written to the per-subscription key
        (``user_cart:{user_id}:sub:{subscription_id}``) so that multiple
        subscriptions of the same user do not overwrite each other's carts.
        The global key is still written for backward compatibility with code
        that reads only the global cart (UI handlers, menu indicators, etc.).

        Args:
            user_id: ID пользователя
            cart_data: Данные корзины (параметры подписки)
            ttl: Время жизни ключа в секундах (по умолчанию из settings.CART_TTL_SECONDS)

        Returns:
            bool: Успешность сохранения
        """
        client = self._get_redis_client()
        if client is None:
            logger.warning('🛒 Redis недоступен, корзина пользователя НЕ сохранена', user_id=user_id)
            return False

        try:
            key = f'user_cart:{user_id}'
            json_data = json.dumps(cart_data, ensure_ascii=False)
            effective_ttl = ttl if ttl is not None else settings.CART_TTL_SECONDS
            await client.setex(key, effective_ttl, json_data)
            cart_mode = cart_data.get('cart_mode', 'unknown')
            logger.info(
                '🛒 Корзина пользователя сохранена в Redis (mode=, ttl=s)',
                user_id=user_id,
                cart_mode=cart_mode,
                effective_ttl=effective_ttl,
            )

            # Dual-write to per-subscription key when subscription_id is known.
            # This ensures multi-subscription users don't lose carts when a
            # second subscription's cart overwrites the global key.
            subscription_id = cart_data.get('subscription_id')
            if subscription_id is not None:
                try:
                    sub_id = int(subscription_id)
                    sub_key = self._subscription_cart_key(user_id, sub_id)
                    await client.setex(sub_key, effective_ttl, json_data)
                    logger.debug(
                        'Корзина также сохранена по per-subscription ключу',
                        user_id=user_id,
                        subscription_id=sub_id,
                    )
                except (TypeError, ValueError):
                    pass  # Non-integer subscription_id -- skip per-sub key

            return True
        except Exception as e:
            logger.error('🛒 Ошибка сохранения корзины пользователя', user_id=user_id, error=e)
            return False

    async def get_user_cart(self, user_id: int) -> dict[str, Any] | None:
        """
        Получить корзину пользователя из Redis.

        Args:
            user_id: ID пользователя

        Returns:
            dict: Данные корзины или None
        """
        client = self._get_redis_client()
        if client is None:
            return None

        try:
            key = f'user_cart:{user_id}'
            json_data = await client.get(key)
            if json_data:
                cart_data = json.loads(json_data)
                logger.debug('Корзина пользователя загружена из Redis', user_id=user_id)
                return cart_data
            return None
        except Exception as e:
            logger.error('Ошибка получения корзины пользователя', user_id=user_id, error=e)
            return None

    async def delete_user_cart(self, user_id: int) -> bool:
        """
        Удалить корзину пользователя из Redis.

        Also deletes the corresponding per-subscription key if the global
        cart contained a ``subscription_id`` (cleanup after dual-write).

        Args:
            user_id: ID пользователя

        Returns:
            bool: Успешность удаления
        """
        client = self._get_redis_client()
        if client is None:
            return False

        try:
            key = f'user_cart:{user_id}'

            # Read the global cart first to find associated per-subscription key
            raw_data = await client.get(key)
            subscription_id: int | None = None
            if raw_data:
                try:
                    data = json.loads(raw_data)
                    sub_id_raw = data.get('subscription_id')
                    if sub_id_raw is not None:
                        subscription_id = int(sub_id_raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            result = await client.delete(key)

            # Also clean up the per-subscription key if it exists
            if subscription_id is not None:
                sub_key = self._subscription_cart_key(user_id, subscription_id)
                await client.delete(sub_key)

            if result:
                logger.debug('Корзина пользователя удалена из Redis', user_id=user_id)
            return bool(result)
        except Exception as e:
            logger.error('Ошибка удаления корзины пользователя', user_id=user_id, error=e)
            return False

    async def delete_global_cart_only(self, user_id: int) -> bool:
        """Delete ONLY the global ``user_cart:{user_id}`` key.

        Unlike ``delete_user_cart``, this does NOT cascade into the
        per-subscription key.  Used when the per-subscription key was
        already handled separately and we just need to remove the stale
        global entry.
        """
        client = self._get_redis_client()
        if client is None:
            return False
        try:
            key = f'user_cart:{user_id}'
            result = await client.delete(key)
            return bool(result)
        except Exception as e:
            logger.error('Ошибка удаления глобальной корзины пользователя', user_id=user_id, error=e)
            return False

    async def has_user_cart(self, user_id: int) -> bool:
        """
        Проверить наличие корзины у пользователя.

        Args:
            user_id: ID пользователя

        Returns:
            bool: Наличие корзины
        """
        client = self._get_redis_client()
        if client is None:
            logger.warning('🛒 Redis недоступен, проверка корзины пользователя невозможна', user_id=user_id)
            return False

        try:
            key = f'user_cart:{user_id}'
            exists = await client.exists(key)
            result = bool(exists)
            logger.info(
                '🛒 Проверка корзины пользователя', user_id=user_id, value='найдена' if result else 'не найдена'
            )
            return result
        except Exception as e:
            logger.error('🛒 Ошибка проверки наличия корзины пользователя', user_id=user_id, error=e)
            return False

    # ---- Per-subscription cart methods (multi-tariff safe) ----

    @staticmethod
    def _subscription_cart_key(user_id: int, subscription_id: int) -> str:
        return f'user_cart:{user_id}:sub:{subscription_id}'

    async def save_subscription_cart(
        self,
        user_id: int,
        subscription_id: int,
        cart_data: dict[str, Any],
        ttl: int | None = None,
    ) -> bool:
        """Save a per-subscription cart to Redis.

        Uses key ``user_cart:{user_id}:sub:{subscription_id}`` so that
        multiple subscriptions of the same user can have independent carts
        without overwriting each other.
        """
        client = self._get_redis_client()
        if client is None:
            logger.warning(
                'Redis недоступен, корзина подписки НЕ сохранена',
                user_id=user_id,
                subscription_id=subscription_id,
            )
            return False

        try:
            key = self._subscription_cart_key(user_id, subscription_id)
            json_data = json.dumps(cart_data, ensure_ascii=False)
            effective_ttl = ttl if ttl is not None else settings.CART_TTL_SECONDS
            await client.setex(key, effective_ttl, json_data)
            cart_mode = cart_data.get('cart_mode', 'unknown')
            logger.info(
                'Корзина подписки сохранена в Redis',
                user_id=user_id,
                subscription_id=subscription_id,
                cart_mode=cart_mode,
                effective_ttl=effective_ttl,
            )
            return True
        except Exception as e:
            logger.error(
                'Ошибка сохранения корзины подписки',
                user_id=user_id,
                subscription_id=subscription_id,
                error=e,
            )
            return False

    async def get_subscription_cart(self, user_id: int, subscription_id: int) -> dict[str, Any] | None:
        """Get per-subscription cart from Redis."""
        client = self._get_redis_client()
        if client is None:
            return None

        try:
            key = self._subscription_cart_key(user_id, subscription_id)
            json_data = await client.get(key)
            if json_data:
                cart_data = json.loads(json_data)
                logger.debug(
                    'Корзина подписки загружена из Redis',
                    user_id=user_id,
                    subscription_id=subscription_id,
                )
                return cart_data
            return None
        except Exception as e:
            logger.error(
                'Ошибка получения корзины подписки',
                user_id=user_id,
                subscription_id=subscription_id,
                error=e,
            )
            return None

    async def delete_subscription_cart(self, user_id: int, subscription_id: int) -> bool:
        """Delete per-subscription cart from Redis."""
        client = self._get_redis_client()
        if client is None:
            return False

        try:
            key = self._subscription_cart_key(user_id, subscription_id)
            result = await client.delete(key)
            if result:
                logger.debug(
                    'Корзина подписки удалена из Redis',
                    user_id=user_id,
                    subscription_id=subscription_id,
                )
            return bool(result)
        except Exception as e:
            logger.error(
                'Ошибка удаления корзины подписки',
                user_id=user_id,
                subscription_id=subscription_id,
                error=e,
            )
            return False

    async def get_all_subscription_carts(self, user_id: int) -> list[dict[str, Any]]:
        """Return all per-subscription carts for a given user.

        Scans Redis keys matching ``user_cart:{user_id}:sub:*`` and returns
        their parsed JSON payloads.  Falls back to the global cart (if it
        contains a ``subscription_id``) so that carts saved by the old code
        path are still picked up.
        """
        client = self._get_redis_client()
        if client is None:
            return []

        results: list[dict[str, Any]] = []
        prefix = f'user_cart:{user_id}:sub:'
        try:
            cursor: int | bytes = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=f'{prefix}*', count=50)
                for raw_key in keys:
                    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                    raw_data = await client.get(key)
                    if raw_data:
                        try:
                            results.append(json.loads(raw_data))
                        except (json.JSONDecodeError, TypeError):
                            logger.warning('Невалидный JSON в корзине подписки', key=key)
                if not cursor or cursor == 0 or cursor == b'0':
                    break
        except Exception as e:
            logger.error('Ошибка сканирования корзин подписок', user_id=user_id, error=e)

        # Fallback: include global cart if it has subscription_id and is not
        # already covered by a per-subscription key (backward compat).
        try:
            global_cart = await self.get_user_cart(user_id)
            if global_cart and global_cart.get('subscription_id') is not None:
                sub_id = global_cart['subscription_id']
                already_covered = any(c.get('subscription_id') == sub_id for c in results)
                if not already_covered:
                    results.append(global_cart)
        except Exception as e:
            logger.warning('Ошибка чтения глобальной корзины при сканировании', user_id=user_id, error=e)

        return results


# Глобальный экземпляр сервиса (инициализация Redis отложена)
user_cart_service = UserCartService()
