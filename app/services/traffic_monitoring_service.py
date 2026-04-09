"""
Сервис для мониторинга трафика пользователей v2
Быстрая проверка текущего трафика + суточная проверка
"""

import asyncio
import html
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_remnawave_uuid
from app.database.database import AsyncSessionLocal
from app.services.admin_notification_service import AdminNotificationService
from app.services.remnawave_service import RemnaWaveService
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

# Ключи для хранения snapshot в Redis
TRAFFIC_SNAPSHOT_KEY = 'traffic:snapshot'
TRAFFIC_SNAPSHOT_TIME_KEY = 'traffic:snapshot:time'
TRAFFIC_NOTIFICATION_CACHE_KEY = 'traffic:notifications'


@dataclass
class TrafficViolation:
    """Информация о превышении трафика"""

    user_uuid: str
    telegram_id: int | None
    full_name: str | None
    username: str | None
    used_traffic_gb: float
    threshold_gb: float
    last_node_uuid: str | None
    last_node_name: str | None
    check_type: str  # "fast" или "daily"


class TrafficMonitoringServiceV2:
    """
    Улучшенный сервис мониторинга трафика
    - Батчевая загрузка пользователей
    - Параллельная обработка
    - Быстрая проверка (каждые N минут) с дельтой
    - Суточная проверка
    - Фильтрация по нодам
    - Хранение snapshot в Redis (персистентность при перезапуске)
    """

    def __init__(self):
        self.remnawave_service = RemnaWaveService()
        self._nodes_cache: dict[str, str] = {}  # {node_uuid: node_name}
        # Fallback на память если Redis недоступен
        self._memory_snapshot: dict[str, float] = {}
        self._memory_snapshot_time: datetime | None = None
        self._memory_notification_cache: dict[str, datetime] = {}

    # ============== Настройки ==============

    def is_fast_check_enabled(self) -> bool:
        # Поддержка старого параметра TRAFFIC_MONITORING_ENABLED
        return settings.TRAFFIC_FAST_CHECK_ENABLED or settings.TRAFFIC_MONITORING_ENABLED

    def is_daily_check_enabled(self) -> bool:
        return settings.TRAFFIC_DAILY_CHECK_ENABLED

    def get_fast_check_interval_seconds(self) -> int:
        # Если используется старый параметр — конвертируем часы в секунды
        if settings.TRAFFIC_MONITORING_ENABLED and not settings.TRAFFIC_FAST_CHECK_ENABLED:
            return settings.TRAFFIC_MONITORING_INTERVAL_HOURS * 3600
        return settings.TRAFFIC_FAST_CHECK_INTERVAL_MINUTES * 60

    def get_fast_check_threshold_gb(self) -> float:
        # Если используется старый параметр — используем старый порог
        if settings.TRAFFIC_MONITORING_ENABLED and not settings.TRAFFIC_FAST_CHECK_ENABLED:
            return settings.TRAFFIC_THRESHOLD_GB_PER_DAY
        return settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB

    def get_daily_threshold_gb(self) -> float:
        return settings.TRAFFIC_DAILY_THRESHOLD_GB

    def get_batch_size(self) -> int:
        return settings.TRAFFIC_CHECK_BATCH_SIZE

    def get_concurrency(self) -> int:
        return settings.TRAFFIC_CHECK_CONCURRENCY

    def get_notification_cooldown_seconds(self) -> int:
        return settings.TRAFFIC_NOTIFICATION_COOLDOWN_MINUTES * 60

    def get_monitored_nodes(self) -> list[str]:
        return settings.get_traffic_monitored_nodes()

    def get_ignored_nodes(self) -> list[str]:
        return settings.get_traffic_ignored_nodes()

    def get_excluded_user_uuids(self) -> list[str]:
        return settings.get_traffic_excluded_user_uuids()

    def get_daily_check_time(self) -> time | None:
        return settings.get_traffic_daily_check_time()

    def get_snapshot_ttl_seconds(self) -> int:
        """TTL для snapshot в Redis (по умолчанию 24 часа)"""
        return getattr(settings, 'TRAFFIC_SNAPSHOT_TTL_HOURS', 24) * 3600

    # ============== Redis операции для snapshot ==============

    async def _save_snapshot_to_redis(self, snapshot: dict[str, float]) -> bool:
        """Сохраняет snapshot трафика в Redis"""
        try:
            # Сохраняем snapshot как JSON
            snapshot_data = {uuid: bytes_val for uuid, bytes_val in snapshot.items()}
            ttl = self.get_snapshot_ttl_seconds()

            success = await cache.set(TRAFFIC_SNAPSHOT_KEY, snapshot_data, expire=ttl)
            if success:
                # Сохраняем время создания snapshot
                await cache.set(TRAFFIC_SNAPSHOT_TIME_KEY, datetime.now(UTC).isoformat(), expire=ttl)
                logger.info(
                    '📦 Snapshot сохранён в Redis: пользователей, TTL ч',
                    snapshot_count=len(snapshot),
                    value=ttl // 3600,
                )
            else:
                logger.warning('⚠️ Не удалось сохранить snapshot в Redis')
            return success
        except Exception as e:
            logger.error('❌ Ошибка сохранения snapshot в Redis', error=e)
            return False

    async def _load_snapshot_from_redis(self) -> dict[str, float] | None:
        """Загружает snapshot трафика из Redis"""
        try:
            snapshot_data = await cache.get(TRAFFIC_SNAPSHOT_KEY)
            # ВАЖНО: пустой словарь {} - это валидный snapshot!
            if snapshot_data is not None and isinstance(snapshot_data, dict):
                # Конвертируем обратно в float
                result = {uuid: float(bytes_val) for uuid, bytes_val in snapshot_data.items()}
                logger.debug('📦 Snapshot загружен из Redis: пользователей', result_count=len(result))
                return result
            return None
        except Exception as e:
            logger.error('❌ Ошибка загрузки snapshot из Redis', error=e)
            return None

    async def _get_snapshot_time_from_redis(self) -> datetime | None:
        """Получает время создания snapshot из Redis"""
        try:
            time_str = await cache.get(TRAFFIC_SNAPSHOT_TIME_KEY)
            if time_str:
                dt = datetime.fromisoformat(time_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            return None
        except Exception as e:
            logger.error('❌ Ошибка получения времени snapshot', error=e)
            return None

    async def _save_notification_to_redis(self, user_uuid: str) -> bool:
        """Сохраняет время уведомления в Redis"""
        try:
            key = cache_key(TRAFFIC_NOTIFICATION_CACHE_KEY, user_uuid)
            ttl = 24 * 3600  # 24 часа
            return await cache.set(key, datetime.now(UTC).isoformat(), expire=ttl)
        except Exception as e:
            logger.error('❌ Ошибка сохранения уведомления в Redis', error=e)
            return False

    async def _get_notification_time_from_redis(self, user_uuid: str) -> datetime | None:
        """Получает время последнего уведомления из Redis"""
        try:
            key = cache_key(TRAFFIC_NOTIFICATION_CACHE_KEY, user_uuid)
            time_str = await cache.get(key)
            if time_str:
                dt = datetime.fromisoformat(time_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            return None
        except Exception as e:
            logger.error('❌ Ошибка получения времени уведомления', error=e)
            return None

    # ============== Работа с нодами ==============

    async def _load_nodes_cache(self):
        """Загружает названия нод в кеш"""
        try:
            nodes = await self.remnawave_service.get_all_nodes()
            self._nodes_cache = {node['uuid']: node['name'] for node in nodes if node.get('uuid') and node.get('name')}
            logger.debug('📋 Загружено нод в кеш', _nodes_cache_count=len(self._nodes_cache))
        except Exception as e:
            logger.error('❌ Ошибка загрузки нод в кеш', error=e)

    def get_node_name(self, node_uuid: str | None) -> str | None:
        """Возвращает название ноды по UUID из кеша"""
        if not node_uuid:
            return None
        return self._nodes_cache.get(node_uuid)

    # ============== Фильтрация по нодам ==============

    def should_monitor_node(self, node_uuid: str | None) -> bool:
        """Проверяет, нужно ли мониторить пользователя с этой ноды"""
        if not node_uuid:
            return True  # Если нода неизвестна, мониторим

        monitored = self.get_monitored_nodes()
        ignored = self.get_ignored_nodes()

        # Если есть список мониторинга — только они
        if monitored:
            return node_uuid in monitored

        # Если есть список игнорирования — все кроме них
        if ignored:
            return node_uuid not in ignored

        # Иначе мониторим всех
        return True

    # ============== Кулдаун уведомлений ==============

    async def should_send_notification(self, user_uuid: str) -> bool:
        """Проверяет, прошёл ли кулдаун для уведомления (Redis + fallback на память)"""
        # Пробуем Redis
        last_notification = await self._get_notification_time_from_redis(user_uuid)

        # Fallback на память
        if last_notification is None:
            last_notification = self._memory_notification_cache.get(user_uuid)

        if not last_notification:
            return True

        cooldown = self.get_notification_cooldown_seconds()
        return (datetime.now(UTC) - last_notification).total_seconds() > cooldown

    async def record_notification(self, user_uuid: str):
        """Записывает время отправки уведомления (Redis + fallback на память)"""
        # Сохраняем в Redis
        saved = await self._save_notification_to_redis(user_uuid)

        # Fallback на память
        if not saved:
            self._memory_notification_cache[user_uuid] = datetime.now(UTC)

    async def cleanup_notification_cache(self):
        """Очищает старые записи из памяти (Redis очищается автоматически через TTL)"""
        now = datetime.now(UTC)
        expired = [uuid for uuid, dt in self._memory_notification_cache.items() if (now - dt) > timedelta(hours=24)]
        for uuid in expired:
            del self._memory_notification_cache[uuid]
        if expired:
            logger.debug('🧹 Очищено записей из памяти уведомлений о трафике', expired_count=len(expired))

    # ============== Получение пользователей ==============

    async def get_all_users_with_traffic(self) -> list[dict]:
        """
        Получает всех пользователей с их трафиком через батчевые запросы
        Возвращает список словарей с информацией о пользователях
        """
        all_users = []
        batch_size = self.get_batch_size()
        offset = 0

        try:
            async with self.remnawave_service.get_api_client() as api:
                while True:
                    result = await api.get_all_users(start=offset, size=batch_size)
                    users = result.get('users', [])

                    if not users:
                        break

                    all_users.extend(users)
                    logger.debug('📊 Загружено пользователей...', all_users_count=len(all_users))

                    if len(users) < batch_size:
                        break

                    offset += batch_size

            logger.info('✅ Всего загружено пользователей из Remnawave', all_users_count=len(all_users))
            return all_users

        except Exception as e:
            logger.error('❌ Ошибка при получении пользователей', error=e)
            return []

    # ============== Быстрая проверка ==============

    async def has_snapshot(self) -> bool:
        """Проверяет, есть ли сохранённый snapshot (Redis + fallback на память)"""
        # Проверяем Redis (пустой словарь {} - это тоже валидный snapshot!)
        snapshot = await self._load_snapshot_from_redis()
        if snapshot is not None:
            return True

        # Fallback на память
        return self._memory_snapshot_time is not None

    async def get_snapshot_age_minutes(self) -> float:
        """Возвращает возраст snapshot в минутах (Redis + fallback на память)"""
        # Пробуем Redis
        snapshot_time = await self._get_snapshot_time_from_redis()

        # Fallback на память
        if snapshot_time is None:
            snapshot_time = self._memory_snapshot_time

        if not snapshot_time:
            return float('inf')
        return (datetime.now(UTC) - snapshot_time).total_seconds() / 60

    async def _get_current_snapshot(self) -> dict[str, float]:
        """Получает текущий snapshot (Redis + fallback на память)"""
        # Пробуем Redis
        snapshot = await self._load_snapshot_from_redis()
        if snapshot:
            return snapshot

        # Fallback на память
        return self._memory_snapshot.copy()

    async def _save_snapshot(self, snapshot: dict[str, float]) -> bool:
        """Сохраняет snapshot (Redis + fallback на память)"""
        # Пробуем Redis
        saved = await self._save_snapshot_to_redis(snapshot)

        if saved:
            # Очищаем память если Redis доступен
            self._memory_snapshot.clear()
            self._memory_snapshot_time = None
            return True

        # Fallback на память
        self._memory_snapshot = snapshot.copy()
        self._memory_snapshot_time = datetime.now(UTC)
        logger.warning('⚠️ Redis недоступен, snapshot сохранён в память')
        return True

    async def create_initial_snapshot(self) -> int:
        """
        Создаёт начальный snapshot при запуске бота.
        Если в Redis уже есть snapshot — использует его (персистентность).
        Возвращает количество пользователей в snapshot.
        """
        # Проверяем есть ли snapshot в Redis (пустой {} тоже валидный snapshot!)
        existing_snapshot = await self._load_snapshot_from_redis()
        if existing_snapshot is not None:
            age = await self.get_snapshot_age_minutes()
            logger.info(
                '📦 Найден существующий snapshot в Redis: пользователей, возраст мин',
                existing_snapshot_count=len(existing_snapshot),
                age=round(age, 1),
            )
            return len(existing_snapshot)

        logger.info('📸 Создание начального snapshot трафика...')
        start_time = datetime.now(UTC)

        users = await self.get_all_users_with_traffic()
        new_snapshot: dict[str, float] = {}

        for user in users:
            try:
                if not user.uuid:
                    continue

                user_traffic = user.user_traffic
                if not user_traffic:
                    continue

                current_bytes = user_traffic.used_traffic_bytes or 0
                new_snapshot[user.uuid] = current_bytes

            except Exception as e:
                logger.error('❌ Ошибка при создании snapshot для', uuid=user.uuid, error=e)

        # Сохраняем в Redis (с fallback на память)
        await self._save_snapshot(new_snapshot)

        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        logger.info(
            '✅ Snapshot создан за с: пользователей', elapsed=round(elapsed, 1), new_snapshot_count=len(new_snapshot)
        )

        return len(new_snapshot)

    async def run_fast_check(self, bot) -> list[TrafficViolation]:
        """
        Быстрая проверка трафика с дельтой

        Логика:
        1. Первый запуск — сохраняем snapshot, не отправляем уведомления
        2. Следующие запуски — сравниваем с snapshot, ищем превышения дельты
        3. После проверки обновляем snapshot (в Redis с fallback на память)
        """
        if not self.is_fast_check_enabled():
            return []

        start_time = datetime.now(UTC)
        is_first_run = not await self.has_snapshot()

        # Загружаем кеш нод для красивых названий в уведомлениях
        await self._load_nodes_cache()

        # Логируем фильтры
        monitored_nodes = self.get_monitored_nodes()
        ignored_nodes = self.get_ignored_nodes()
        excluded_user_uuids = self.get_excluded_user_uuids()

        if monitored_nodes:
            logger.info('🔍 Мониторим только ноды', monitored_nodes=monitored_nodes)
        elif ignored_nodes:
            logger.info('🚫 Игнорируем ноды', ignored_nodes=ignored_nodes)
        else:
            logger.info('📊 Мониторим все ноды')

        if excluded_user_uuids:
            logger.info('🚫 Исключены пользователи', excluded_user_uuids=excluded_user_uuids)

        if is_first_run:
            logger.info('🚀 Первый запуск быстрой проверки — создаём snapshot...')
        else:
            age = await self.get_snapshot_age_minutes()
            logger.info(
                '🚀 Быстрая проверка трафика (snapshot мин назад, порог ГБ)...',
                age=round(age, 1),
                get_fast_check_threshold_gb=self.get_fast_check_threshold_gb(),
            )

        violations: list[TrafficViolation] = []
        threshold_bytes = self.get_fast_check_threshold_gb() * (1024**3)

        users = await self.get_all_users_with_traffic()
        new_snapshot: dict[str, float] = {}

        # Загружаем предыдущий snapshot (из Redis или памяти)
        previous_snapshot = await self._get_current_snapshot()
        logger.info(
            '📦 Предыдущий snapshot: пользователей (is_first_run=)',
            previous_snapshot_count=len(previous_snapshot),
            is_first_run=is_first_run,
        )

        users_with_delta = 0

        for user in users:
            try:
                if not user.uuid:
                    continue

                # Получаем трафик из user_traffic
                user_traffic = user.user_traffic
                if not user_traffic:
                    continue

                current_bytes = user_traffic.used_traffic_bytes or 0
                new_snapshot[user.uuid] = current_bytes

                # Первый запуск — только сохраняем, не проверяем
                if is_first_run:
                    continue

                # Пользователя не было в предыдущем snapshot — пропускаем (новый пользователь)
                if user.uuid not in previous_snapshot:
                    logger.debug('Пользователь не найден в предыдущем snapshot, пропускаем', uuid=user.uuid[:8])
                    continue

                # Получаем предыдущее значение
                previous_bytes = previous_snapshot.get(user.uuid, 0)

                # Вычисляем дельту (может быть отрицательной при сбросе трафика)
                delta_bytes = current_bytes - previous_bytes
                if delta_bytes <= 0:
                    continue  # Трафик сбросился или не изменился

                users_with_delta += 1
                delta_gb = delta_bytes / (1024**3)

                # Проверяем превышение дельты
                if delta_bytes < threshold_bytes:
                    continue

                logger.info(
                    '⚠️ Превышение дельты: ... + ГБ (порог ГБ, previous= ГБ, current= ГБ)',
                    uuid=user.uuid[:8],
                    delta_gb=round(delta_gb, 2),
                    get_fast_check_threshold_gb=self.get_fast_check_threshold_gb(),
                    previous_bytes=round(previous_bytes / 1024**3, 2),
                    current_bytes=round(current_bytes / 1024**3, 2),
                )

                # Проверяем исключённых пользователей (служебные/тунельные)
                if user.uuid.lower() in excluded_user_uuids:
                    logger.info(
                        '⏭️ Пропускаем ... пользователь в списке исключений (служебный/тунельный)', uuid=user.uuid[:8]
                    )
                    continue

                # Проверяем фильтр по нодам
                last_node_uuid = user_traffic.last_connected_node_uuid
                if not self.should_monitor_node(last_node_uuid):
                    logger.warning(
                        '⏭️ Пропускаем нода не в списке мониторинга',
                        uuid=user.uuid[:8],
                        last_node_uuid=last_node_uuid or 'неизвестна',
                    )
                    continue

                # Создаём violation
                delta_gb = round(delta_bytes / (1024**3), 2)
                node_name = self.get_node_name(last_node_uuid)
                violation = TrafficViolation(
                    user_uuid=user.uuid,
                    telegram_id=user.telegram_id,
                    full_name=user.username,
                    username=None,
                    used_traffic_gb=delta_gb,  # Это дельта, не общий трафик!
                    threshold_gb=self.get_fast_check_threshold_gb(),
                    last_node_uuid=last_node_uuid,
                    last_node_name=node_name,
                    check_type='fast',
                )
                violations.append(violation)

            except Exception as e:
                logger.error('❌ Ошибка обработки пользователя', uuid=user.uuid, error=e)

        # Обновляем snapshot (в Redis с fallback на память)
        await self._save_snapshot(new_snapshot)
        logger.info('💾 Новый snapshot сохранён: пользователей', new_snapshot_count=len(new_snapshot))

        elapsed = (datetime.now(UTC) - start_time).total_seconds()

        if is_first_run:
            logger.info(
                '✅ Snapshot создан за с: пользователей. Следующая проверка покажет превышения.',
                elapsed=round(elapsed, 1),
                new_snapshot_count=len(new_snapshot),
            )
        else:
            logger.info(
                '✅ Быстрая проверка завершена за с: пользователей, с дельтой >0, превышений',
                elapsed=round(elapsed, 1),
                users_count=len(users),
                users_with_delta=users_with_delta,
                violations_count=len(violations),
            )
            # Отправляем уведомления только если это не первый запуск
            await self._send_violation_notifications(violations, bot)

        return violations

    # ============== Суточная проверка ==============

    async def run_daily_check(self, bot) -> list[TrafficViolation]:
        """
        Суточная проверка трафика за последние 24 часа
        Использует bandwidth-stats API
        """
        if not self.is_daily_check_enabled():
            return []

        logger.info('🚀 Запуск суточной проверки трафика...')
        start_time = datetime.now(UTC)

        # Загружаем кеш нод для красивых названий в уведомлениях
        await self._load_nodes_cache()

        violations: list[TrafficViolation] = []
        threshold_bytes = self.get_daily_threshold_gb() * (1024**3)

        # Получаем период за последние 24 часа
        now = datetime.now(UTC)
        start_date = (now - timedelta(hours=24)).strftime('%Y-%m-%d')
        end_date = now.strftime('%Y-%m-%d')

        users = await self.get_all_users_with_traffic()
        semaphore = asyncio.Semaphore(self.get_concurrency())

        async def check_user_daily_traffic(user) -> TrafficViolation | None:
            async with semaphore:
                try:
                    if not user.uuid:
                        return None

                    # Получаем статистику за период
                    async with self.remnawave_service.get_api_client() as api:
                        stats = await api.get_bandwidth_stats_user(user.uuid, start_date, end_date)

                    if not stats:
                        return None

                    # Суммируем трафик по нодам
                    total_bytes = 0
                    if isinstance(stats, list):
                        for item in stats:
                            total_bytes += item.get('total', 0)
                    elif isinstance(stats, dict):
                        total_bytes = stats.get('total', 0)

                    if total_bytes < threshold_bytes:
                        return None

                    # Проверяем фильтр по нодам
                    user_traffic = user.user_traffic
                    last_node_uuid = user_traffic.last_connected_node_uuid if user_traffic else None
                    if not self.should_monitor_node(last_node_uuid):
                        return None

                    used_gb = round(total_bytes / (1024**3), 2)
                    node_name = self.get_node_name(last_node_uuid)
                    return TrafficViolation(
                        user_uuid=user.uuid,
                        telegram_id=user.telegram_id,
                        full_name=user.username,
                        username=None,
                        used_traffic_gb=used_gb,
                        threshold_gb=self.get_daily_threshold_gb(),
                        last_node_uuid=last_node_uuid,
                        last_node_name=node_name,
                        check_type='daily',
                    )

                except Exception as e:
                    logger.error('❌ Ошибка суточной проверки для', uuid=user.uuid, error=e)
                    return None

        # Параллельная проверка
        tasks = [check_user_daily_traffic(user) for user in users if user.uuid]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, TrafficViolation):
                violations.append(result)

        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        logger.info(
            '✅ Суточная проверка завершена за с: пользователей, превышений',
            elapsed=round(elapsed, 1),
            users_count=len(users),
            violations_count=len(violations),
        )

        # Отправляем уведомления
        await self._send_violation_notifications(violations, bot)

        return violations

    # ============== Уведомления ==============

    async def _send_violation_notifications(self, violations: list[TrafficViolation], bot):
        """Отправляет уведомления о превышениях"""
        if not violations or not bot:
            return

        admin_service = AdminNotificationService(bot)
        topic_id = settings.SUSPICIOUS_NOTIFICATIONS_TOPIC_ID

        # Ограничиваем количество уведомлений за раз (защита от flood)
        max_notifications = 10
        if len(violations) > max_notifications:
            logger.warning(
                '⚠️ Слишком много превышений отправляем только первые',
                violations_count=len(violations),
                max_notifications=max_notifications,
            )
            violations = violations[:max_notifications]

        for i, violation in enumerate(violations):
            try:
                if not await self.should_send_notification(violation.user_uuid):
                    logger.info(
                        '⏭️ Кулдаун для ... пропускаем уведомление (кулдаун мин)',
                        user_uuid=violation.user_uuid[:8],
                        value=self.get_notification_cooldown_seconds() // 60,
                    )
                    continue

                # Получаем информацию о пользователе из БД
                user_info = ''
                async with AsyncSessionLocal() as db:
                    db_user = await get_user_by_remnawave_uuid(db, violation.user_uuid)
                    if db_user:
                        user_id_display = db_user.telegram_id or db_user.email or f'#{db_user.id}'
                        user_info = f'👤 <b>{html.escape(db_user.full_name or "Без имени")}</b>\n🆔 ID: <code>{user_id_display}</code>\n'
                        if db_user.username:
                            user_info += f'📱 Username: @{html.escape(db_user.username)}\n'

                if violation.check_type == 'fast':
                    check_type_emoji = '⚡'
                    check_type_name = 'Быстрая проверка'
                    traffic_label = 'За интервал'
                elif violation.check_type == 'daily':
                    check_type_emoji = '📅'
                    check_type_name = 'Суточная проверка'
                    traffic_label = 'За 24 часа'
                else:
                    check_type_emoji = '🔍'
                    check_type_name = 'Ручная проверка'
                    traffic_label = 'Использовано'

                message = (
                    f'⚠️ <b>Превышение трафика</b>\n\n'
                    f'{user_info}'
                    f'🔑 UUID: <code>{violation.user_uuid}</code>\n\n'
                    f'{check_type_emoji} <b>{check_type_name}</b>\n'
                    f'📊 {traffic_label}: <b>{violation.used_traffic_gb} ГБ</b>\n'
                    f'📈 Порог: <b>{violation.threshold_gb} ГБ</b>\n'
                    f'🚨 Превышение: <b>{violation.used_traffic_gb - violation.threshold_gb:.2f} ГБ</b>\n'
                )

                # Показываем название ноды и UUID
                if violation.last_node_name:
                    message += f'\n🖥 Сервер: <b>{violation.last_node_name}</b>'
                    if violation.last_node_uuid:
                        message += f'\n   <code>{violation.last_node_uuid}</code>'
                elif violation.last_node_uuid:
                    message += f'\n🖥 Сервер: <code>{violation.last_node_uuid}</code>'

                message += f'\n\n⏰ {datetime.now(UTC).strftime("%d.%m.%Y %H:%M:%S")} UTC'

                await admin_service.send_suspicious_traffic_notification(message, bot, topic_id)
                await self.record_notification(violation.user_uuid)

                logger.info('📨 Уведомление отправлено для', user_uuid=violation.user_uuid)

                # Задержка между отправками (защита от flood)
                if i < len(violations) - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error('❌ Ошибка отправки уведомления для', user_uuid=violation.user_uuid, error=e)


class TrafficMonitoringSchedulerV2:
    """
    Планировщик проверок трафика v2
    - Быстрая проверка каждые N минут
    - Суточная проверка в заданное время
    """

    def __init__(self, service: TrafficMonitoringServiceV2):
        self.service = service
        self.bot = None
        self._fast_check_task: asyncio.Task | None = None
        self._daily_check_task: asyncio.Task | None = None
        self._is_running = False

    def set_bot(self, bot):
        """Устанавливает экземпляр бота"""
        self.bot = bot

    async def start(self):
        """Запускает планировщик"""
        if self._is_running:
            logger.warning('Планировщик мониторинга трафика уже запущен')
            return

        if not self.bot:
            logger.error('Бот не установлен для планировщика мониторинга')
            return

        self._is_running = True

        # Создаём начальный snapshot при старте (без уведомлений!)
        if self.service.is_fast_check_enabled():
            await self.service.create_initial_snapshot()

        # Запускаем быструю проверку
        if self.service.is_fast_check_enabled():
            interval = self.service.get_fast_check_interval_seconds()
            logger.info('🚀 Запуск быстрой проверки трафика каждые мин', value=interval // 60)
            self._fast_check_task = asyncio.create_task(self._run_fast_check_loop(interval))

        # Запускаем суточную проверку
        if self.service.is_daily_check_enabled():
            check_time = self.service.get_daily_check_time()
            if check_time:
                logger.info('🚀 Запуск суточной проверки трафика в', check_time=check_time.strftime('%H:%M'))
                self._daily_check_task = asyncio.create_task(self._run_daily_check_loop(check_time))

    async def stop(self):
        """Останавливает планировщик"""
        self._is_running = False

        if self._fast_check_task:
            self._fast_check_task.cancel()
            try:
                await self._fast_check_task
            except asyncio.CancelledError:
                pass
            self._fast_check_task = None

        if self._daily_check_task:
            self._daily_check_task.cancel()
            try:
                await self._daily_check_task
            except asyncio.CancelledError:
                pass
            self._daily_check_task = None

        logger.info('ℹ️ Планировщик мониторинга трафика остановлен')

    async def _run_fast_check_loop(self, interval_seconds: int):
        """Цикл быстрой проверки"""
        # Сначала ждём интервал (snapshot уже создан в start())
        logger.info('⏳ Первая проверка через минут...', value=interval_seconds // 60)
        await asyncio.sleep(interval_seconds)

        while self._is_running:
            try:
                await self.service.cleanup_notification_cache()
                await self.service.run_fast_check(self.bot)
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('❌ Ошибка в цикле быстрой проверки', error=e)
                await asyncio.sleep(interval_seconds)

    async def _run_daily_check_loop(self, check_time: time):
        """Цикл суточной проверки"""
        while self._is_running:
            try:
                # Вычисляем время до следующей проверки
                now = datetime.now(UTC)
                next_run = datetime.combine(now.date(), check_time, tzinfo=UTC)
                if next_run <= now:
                    next_run += timedelta(days=1)

                delay = (next_run - now).total_seconds()
                logger.debug('⏰ Следующая суточная проверка через ч', delay=round(delay / 3600, 1))

                await asyncio.sleep(delay)

                if self._is_running:
                    await self.service.run_daily_check(self.bot)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('❌ Ошибка в цикле суточной проверки', error=e)
                await asyncio.sleep(3600)  # Ждём час при ошибке

    async def run_fast_check_now(self) -> list[TrafficViolation]:
        """Запускает быструю проверку немедленно"""
        return await self.service.run_fast_check(self.bot)

    async def run_daily_check_now(self) -> list[TrafficViolation]:
        """Запускает суточную проверку немедленно"""
        return await self.service.run_daily_check(self.bot)


# ============== Обратная совместимость ==============


class TrafficMonitoringService:
    """Обёртка для обратной совместимости со старым API"""

    def __init__(self):
        self._v2 = TrafficMonitoringServiceV2()
        self.remnawave_service = self._v2.remnawave_service

    def is_traffic_monitoring_enabled(self) -> bool:
        # Используем старый параметр или новые
        return (
            settings.TRAFFIC_MONITORING_ENABLED
            or settings.TRAFFIC_FAST_CHECK_ENABLED
            or settings.TRAFFIC_DAILY_CHECK_ENABLED
        )

    def get_traffic_threshold_gb(self) -> float:
        """Возвращает порог трафика"""
        if settings.TRAFFIC_FAST_CHECK_ENABLED:
            return settings.TRAFFIC_FAST_CHECK_THRESHOLD_GB
        return settings.TRAFFIC_THRESHOLD_GB_PER_DAY

    async def check_user_traffic_threshold(
        self, db: AsyncSession, user_uuid: str, user_telegram_id: int = None
    ) -> tuple:
        """Проверяет трафик одного пользователя (для обратной совместимости)"""
        try:
            threshold_gb = self.get_traffic_threshold_gb()
            threshold_bytes = threshold_gb * (1024**3)

            # Получаем пользователя из Remnawave
            async with self.remnawave_service.get_api_client() as api:
                user = await api.get_user_by_uuid(user_uuid)

            if not user or not user.user_traffic:
                return False, {'total_gb': 0, 'nodes': []}

            used_bytes = user.user_traffic.used_traffic_bytes or 0
            total_gb = round(used_bytes / (1024**3), 2)

            is_exceeded = used_bytes > threshold_bytes

            traffic_info = {'total_gb': total_gb, 'nodes': [], 'threshold_gb': threshold_gb}

            return is_exceeded, traffic_info

        except Exception as e:
            logger.error('Ошибка проверки трафика для', user_uuid=user_uuid, error=e)
            return False, {'total_gb': 0, 'nodes': []}

    async def process_suspicious_traffic(self, db: AsyncSession, user_uuid: str, traffic_info: dict, bot):
        """Отправляет уведомление о подозрительном трафике"""
        violation = TrafficViolation(
            user_uuid=user_uuid,
            telegram_id=None,
            full_name=None,
            username=None,
            used_traffic_gb=traffic_info.get('total_gb', 0),
            threshold_gb=traffic_info.get('threshold_gb', self.get_traffic_threshold_gb()),
            last_node_uuid=None,
            last_node_name=None,
            check_type='manual',
        )
        await self._v2._send_violation_notifications([violation], bot)

    async def check_all_users_traffic(self, db: AsyncSession, bot):
        """Старый метод — теперь вызывает быструю проверку"""
        await self._v2.run_fast_check(bot)


# Глобальные экземпляры (создаём до класса-обёртки)
traffic_monitoring_service_v2 = TrafficMonitoringServiceV2()
traffic_monitoring_scheduler_v2 = TrafficMonitoringSchedulerV2(traffic_monitoring_service_v2)


class TrafficMonitoringScheduler:
    """Обёртка для обратной совместимости — использует глобальные v2 экземпляры"""

    def __init__(self, traffic_service: TrafficMonitoringService = None):
        # Используем глобальные экземпляры!
        self._v2_service = traffic_monitoring_service_v2
        self._v2_scheduler = traffic_monitoring_scheduler_v2
        self.bot = None

    def set_bot(self, bot):
        self.bot = bot
        self._v2_scheduler.set_bot(bot)

    def is_enabled(self) -> bool:
        return self._v2_service.is_fast_check_enabled() or self._v2_service.is_daily_check_enabled()

    def get_interval_hours(self) -> int:
        """Для обратной совместимости — возвращает интервал быстрой проверки в часах"""
        return max(1, self._v2_service.get_fast_check_interval_seconds() // 3600)

    def get_status_info(self) -> str:
        """Возвращает информацию о статусе мониторинга"""
        info = []
        if self._v2_service.is_fast_check_enabled():
            interval_min = self._v2_service.get_fast_check_interval_seconds() // 60
            threshold = self._v2_service.get_fast_check_threshold_gb()
            info.append(f'Быстрая: каждые {interval_min} мин, порог {threshold} ГБ')
        if self._v2_service.is_daily_check_enabled():
            check_time = self._v2_service.get_daily_check_time()
            threshold = self._v2_service.get_daily_threshold_gb()
            time_str = check_time.strftime('%H:%M') if check_time else '00:00'
            info.append(f'Суточная: в {time_str}, порог {threshold} ГБ')
        return '; '.join(info) if info else 'Отключен'

    async def _should_send_notification(self, user_uuid: str) -> bool:
        """Для обратной совместимости"""
        return await self._v2_service.should_send_notification(user_uuid)

    async def _record_notification(self, user_uuid: str):
        """Для обратной совместимости"""
        await self._v2_service.record_notification(user_uuid)

    async def start_monitoring(self):
        await self._v2_scheduler.start()

    def stop_monitoring(self):
        asyncio.create_task(self._v2_scheduler.stop())


# Обратная совместимость
traffic_monitoring_service = TrafficMonitoringService()
traffic_monitoring_scheduler = TrafficMonitoringScheduler()
