import json
from typing import Any
from uuid import uuid4

import redis.asyncio as redis
import structlog

from app.config import settings
from app.utils.redis_client import create_redis


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
            self._redis_client = create_redis()
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
            effective_ttl = ttl if ttl is not None else settings.CART_TTL_SECONDS
            intent_ttl = settings.CART_AUTOPURCHASE_INTENT_TTL_SECONDS
            if effective_ttl <= 0 or (cart_data.get('return_to_cart') and intent_ttl <= 0):
                raise ValueError('Cart TTL must be positive')
            if cart_data.get('cart_mode') == 'tariff_purchase':
                cart_data.setdefault('tariff_checkout_id', uuid4().hex)
            cart_data['_cart_revision'] = uuid4().hex
            json_data = json.dumps(cart_data, ensure_ascii=False)
            # MULTI/EXEC publishes aliases and the intent in one server operation.
            # Separate SETEX calls could interleave two saves of the same source.
            pipeline = client.pipeline(transaction=True)
            pipeline.setex(f'user_cart:{user_id}', effective_ttl, json_data)
            subscription_id = cart_data.get('subscription_id')
            sub_id = None
            if subscription_id is not None:
                try:
                    sub_id = int(subscription_id)
                except (TypeError, ValueError):
                    pass  # Preserve legacy global-only storage for invalid IDs.
                else:
                    pipeline.setex(self._subscription_cart_key(user_id, sub_id), effective_ttl, json_data)
            if cart_data.get('return_to_cart'):
                pipeline.setex(self._topup_intent_key(user_id), intent_ttl, cart_data['_cart_revision'])
                if sub_id is not None:
                    pipeline.setex(
                        self._subscription_topup_intent_key(user_id, sub_id), intent_ttl, cart_data['_cart_revision']
                    )
            await pipeline.execute()
            logger.info(
                '🛒 Корзина пользователя сохранена в Redis',
                user_id=user_id,
                cart_mode=cart_data.get('cart_mode', 'unknown'),
                effective_ttl=effective_ttl,
            )
            return True
        except Exception as e:
            logger.error('🛒 Ошибка сохранения корзины пользователя', user_id=user_id, error=e)
            return False

    async def save_gift_cart_if_current(
        self, user_id: int, cart_data: dict[str, Any], original: dict[str, Any] | None
    ) -> bool:
        """Publish a confirmed gift and its intent only over the captured global snapshot.

        A fresh explicit gift confirmation may replace the original global cart;
        independent subscription aliases and permissions are never changed.
        """
        client = self._get_redis_client()
        if (
            client is None
            or cart_data.get('cart_mode') != 'gift_purchase'
            or cart_data.get('subscription_id') is not None
        ):
            return False
        try:
            cart_ttl = settings.CART_TTL_SECONDS
            intent_ttl = settings.CART_AUTOPURCHASE_INTENT_TTL_SECONDS
            if cart_ttl <= 0 or intent_ttl <= 0 or not cart_data.get('return_to_cart'):
                return False
            replacement = {**cart_data, '_cart_revision': uuid4().hex}
            script = """
                local raw = redis.call('GET', KEYS[1])
                if ARGV[1] == 'absent' then
                    if raw then return 0 end
                else
                    if not raw then return 0 end
                    if ARGV[3] == '' then
                        if raw ~= ARGV[2] then return 0 end
                    else
                        local ok, data = pcall(cjson.decode, raw)
                        if not ok or type(data) ~= 'table' or data['_cart_revision'] ~= ARGV[3] then
                            return 0
                        end
                    end
                end
                redis.call('SET', KEYS[1], ARGV[4], 'EX', ARGV[5])
                redis.call('SET', KEYS[2], ARGV[6], 'EX', ARGV[7])
                return 1
            """
            saved = bool(
                await client.eval(
                    script,
                    2,
                    f'user_cart:{user_id}',
                    self._topup_intent_key(user_id),
                    'absent' if original is None else 'present',
                    json.dumps(original, ensure_ascii=False),
                    (original or {}).get('_cart_revision') or '',
                    json.dumps(replacement, ensure_ascii=False),
                    cart_ttl,
                    replacement['_cart_revision'],
                    intent_ttl,
                )
            )
            if saved:
                cart_data.update(replacement)
            return saved
        except Exception as error:
            logger.error('Ошибка условного сохранения подарочной корзины', user_id=user_id, error=error)
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
                await client.delete(self._subscription_topup_intent_key(user_id, subscription_id))

            # Удаляем метку намерения пополнения вместе с корзиной, чтобы она не
            # «висела» после очистки и не давала повода для авто-покупки.
            await client.delete(self._topup_intent_key(user_id))

            if result:
                logger.debug('Корзина пользователя удалена из Redis', user_id=user_id)
            return bool(result)
        except Exception as e:
            logger.error('Ошибка удаления корзины пользователя', user_id=user_id, error=e)
            return False

    async def delete_processed_cart(self, user_id: int, cart_data: dict[str, Any]) -> bool:
        """Atomically delete only aliases and intent belonging to this saved revision.

        Old carts without a revision use exact serialized-payload comparison;
        changed legacy payloads are retained rather than risking another intent.
        """
        client = self._get_redis_client()
        if client is None:
            return False
        try:
            sub_id = int(cart_data.get('subscription_id') or 0)
            script = """
                local function matches(key)
                    local raw = redis.call('GET', key)
                    if not raw then return false end
                    if ARGV[2] == '' then return raw == ARGV[1] end
                    local ok, data = pcall(cjson.decode, raw)
                    return ok and type(data) == 'table' and data['_cart_revision'] == ARGV[2]
                end
                local removed = 0
                local global_matches = matches(KEYS[1])
                if global_matches then
                    removed = removed + redis.call('DEL', KEYS[1])
                end
                local intent = redis.call('GET', KEYS[3])
                if (ARGV[2] ~= '' and intent == ARGV[2]) or
                   (global_matches and ARGV[2] == '' and intent == '1') then
                    redis.call('DEL', KEYS[3])
                end
                if ARGV[3] == '1' then
                    if matches(KEYS[2]) then
                        removed = removed + redis.call('DEL', KEYS[2])
                    end
                    if ARGV[2] ~= '' and redis.call('GET', KEYS[4]) == ARGV[2] then
                        redis.call('DEL', KEYS[4])
                    end
                end
                return removed
            """
            result = await client.eval(
                script,
                4,
                f'user_cart:{user_id}',
                self._subscription_cart_key(user_id, sub_id),
                self._topup_intent_key(user_id),
                self._subscription_topup_intent_key(user_id, sub_id),
                json.dumps(cart_data, ensure_ascii=False),
                cart_data.get('_cart_revision') or '',
                '1' if sub_id else '0',
            )
            return bool(result)
        except Exception as error:
            logger.error('Ошибка условной очистки обработанной корзины', user_id=user_id, error=error)
            return False

    async def update_processed_global_cart(
        self,
        user_id: int,
        cart_data: dict[str, Any],
        updates: dict[str, Any],
        *,
        revoke_intent: bool = False,
    ) -> bool:
        """Update global checkout metadata only while its saved revision matches.

        Keep the existing cart/intent TTL for partial topups. A changed quote
        gets a new revision and revokes only its original global permission.
        """
        client = self._get_redis_client()
        if client is None or cart_data.get('subscription_id') is not None:
            return False
        if not updates.keys() <= {'total_price', 'missing_amount', 'return_to_cart'}:
            return False
        try:
            # A revision's permission applies only to its confirmed price.
            # Enforce this here as well as in callers: quote metadata must
            # never silently carry an old approval over to a different sum.
            revoke_intent = revoke_intent or updates.get('total_price', cart_data.get('total_price')) != cart_data.get(
                'total_price'
            )
            replacement = {**cart_data, **updates}
            if revoke_intent:
                replacement['return_to_cart'] = False
                replacement['_cart_revision'] = uuid4().hex
            script = """
                local raw = redis.call('GET', KEYS[1])
                if not raw then return 0 end
                if ARGV[2] == '' then
                    if raw ~= ARGV[1] then return 0 end
                else
                    local ok, data = pcall(cjson.decode, raw)
                    if not ok or type(data) ~= 'table' or data['_cart_revision'] ~= ARGV[2] then
                        return 0
                    end
                end
                redis.call('SET', KEYS[1], ARGV[3], 'KEEPTTL')
                if ARGV[4] == '1' then
                    local intent = redis.call('GET', KEYS[2])
                    if (ARGV[2] ~= '' and intent == ARGV[2]) or (ARGV[2] == '' and intent == '1') then
                        redis.call('DEL', KEYS[2])
                    end
                end
                return 1
            """
            return bool(
                await client.eval(
                    script,
                    2,
                    f'user_cart:{user_id}',
                    self._topup_intent_key(user_id),
                    json.dumps(cart_data, ensure_ascii=False),
                    cart_data.get('_cart_revision') or '',
                    json.dumps(replacement, ensure_ascii=False),
                    '1' if revoke_intent else '0',
                )
            )
        except Exception as error:
            logger.error('Ошибка условного обновления корзины', user_id=user_id, error=error)
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

    @staticmethod
    def _topup_intent_key(user_id: int) -> str:
        """Ключ «свежего намерения» пополнить ради сохранённой корзины.

        Ставится коротко-живущим (CART_AUTOPURCHASE_INTENT_TTL_SECONDS), когда
        пользователь явно вошёл в поток «недостаточно средств → корзина
        сохранена → выбрать оплату» (cart_data['return_to_cart'] == True).
        Тихая авто-покупка из корзины после пополнения проверяет наличие метки
        (has_topup_intent) и гасит её только при УСПЕШНОЙ покупке
        (clear_topup_intent) — чтобы обычное пополнение баланса без корзины не
        тратилось молча на подписку из старой корзины, а для подарков исполнялась
        только явная подходящая корзина подарка (gift_purchase).
        """
        return f'cart_topup_intent:{user_id}'

    @staticmethod
    def _subscription_topup_intent_key(user_id: int, subscription_id: int) -> str:
        return f'cart_topup_intent:{user_id}:sub:{subscription_id}'

    async def has_topup_intent(self, user_id: int, cart_data: dict[str, Any] | None = None) -> bool:
        """Check presence, or authorize a specific currently saved cart revision.

        Presence alone is a compatibility hint, never permission to debit a
        collected cart. Per-source markers retain independent confirmed carts.
        """
        client = self._get_redis_client()
        if client is None:
            return False
        try:
            if cart_data is None:
                if await client.exists(self._topup_intent_key(user_id)):
                    return True
                cursor = 0
                while True:
                    cursor, keys = await client.scan(
                        cursor=cursor, match=f'cart_topup_intent:{user_id}:sub:*', count=50
                    )
                    if keys:
                        return True
                    if not cursor or cursor == b'0':
                        return False
            revision = cart_data.get('_cart_revision')
            sub_id = int(cart_data.get('subscription_id') or 0)
            global_intent = await client.get(self._topup_intent_key(user_id))
            if isinstance(global_intent, bytes):
                global_intent = global_intent.decode()
            if not revision:
                # An old unbound marker cannot authorize arbitrary per-source carts.
                raw = await client.get(f'user_cart:{user_id}')
                if isinstance(raw, bytes):
                    raw = raw.decode()
                return (
                    bool(cart_data.get('return_to_cart'))
                    and global_intent == '1'
                    and raw == json.dumps(cart_data, ensure_ascii=False)
                )
            source_intent = await client.get(self._subscription_topup_intent_key(user_id, sub_id)) if sub_id else None
            if isinstance(source_intent, bytes):
                source_intent = source_intent.decode()
            if revision not in (source_intent, global_intent):
                return False
            key = self._subscription_cart_key(user_id, sub_id) if sub_id else f'user_cart:{user_id}'
            raw = await client.get(key)
            # Compatibility with legacy global-only storage of an explicit source.
            if raw is None and sub_id:
                raw = await client.get(f'user_cart:{user_id}')
            return bool(raw and json.loads(raw).get('_cart_revision') == revision)
        except Exception as e:
            logger.error('🛒 Ошибка чтения метки намерения пополнения', user_id=user_id, error=e)
            return False

    async def clear_topup_intent(self, user_id: int) -> None:
        """Погасить метку намерения (после успешной авто-покупки или очистки корзины)."""
        client = self._get_redis_client()
        if client is None:
            return
        try:
            await client.delete(self._topup_intent_key(user_id))
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=f'cart_topup_intent:{user_id}:sub:*', count=50)
                if keys:
                    await client.delete(*keys)
                if not cursor or cursor == b'0':
                    break
        except Exception as e:
            logger.error('🛒 Ошибка удаления метки намерения пополнения', user_id=user_id, error=e)

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
            cart_data['_cart_revision'] = uuid4().hex
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
            await client.delete(self._subscription_topup_intent_key(user_id, subscription_id))
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
