import asyncio
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import String, and_, cast, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.server_squad import get_server_squad_by_uuid
from app.database.crud.subscription import (
    decrement_subscription_server_counts,
)
from app.database.crud.user import (
    create_user_no_commit,
    get_user_by_telegram_id,
)
from app.database.models import (
    ServerSquad,
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    User,
)
from app.external.remnawave_api import (
    RemnaWaveAPI,
    RemnaWaveAPIError,
    UserStatus,
)
from app.services.subscription_service import get_traffic_reset_strategy
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)
from app.utils.timezone import get_local_timezone


logger = structlog.get_logger(__name__)


def _get_user_traffic_bytes(panel_user: dict[str, Any]) -> int:
    """Извлекает usedTrafficBytes из панельного пользователя (совместимо с новым и старым API)"""
    # Новый формат: userTraffic.usedTrafficBytes
    user_traffic = panel_user.get('userTraffic')
    if user_traffic and isinstance(user_traffic, dict):
        return user_traffic.get('usedTrafficBytes', 0)
    # Старый формат: usedTrafficBytes напрямую
    return panel_user.get('usedTrafficBytes', 0)


def _get_lifetime_traffic_bytes(panel_user: dict[str, Any]) -> int:
    """Извлекает lifetimeUsedTrafficBytes из панельного пользователя (совместимо с новым и старым API)"""
    # Новый формат: userTraffic.lifetimeUsedTrafficBytes
    user_traffic = panel_user.get('userTraffic')
    if user_traffic and isinstance(user_traffic, dict):
        return user_traffic.get('lifetimeUsedTrafficBytes', 0)
    # Старый формат: lifetimeUsedTrafficBytes напрямую
    return panel_user.get('lifetimeUsedTrafficBytes', 0)


_UUID_MAP_MISSING = object()
_ATTR_NOT_CAPTURED = object()


class _UUIDMapMutation:
    """Tracks in-memory UUID map/user changes so they can be rolled back."""

    __slots__ = ('_map_original', '_user_original', 'uuid_map')

    def __init__(self, uuid_map: dict[str, 'User']):
        self.uuid_map = uuid_map
        self._map_original: dict[str, Any] = {}
        self._user_original: dict[User, tuple[Any, Any]] = {}

    def _capture_user_state(self, user: Optional['User']) -> None:
        if not user or user in self._user_original:
            return
        # В async-контексте ORM-атрибуты могут быть expired (например после
        # SAVEPOINT rollback). getattr не спасает — SQLAlchemy бросает
        # MissingGreenlet, а не AttributeError. Ловим и помечаем sentinel'ом.
        try:
            uuid_val = getattr(user, 'remnawave_uuid', None)
        except Exception:
            uuid_val = _ATTR_NOT_CAPTURED
        try:
            updated_val = getattr(user, 'updated_at', None)
        except Exception:
            updated_val = _ATTR_NOT_CAPTURED
        self._user_original[user] = (uuid_val, updated_val)

    def _capture_map_entry(self, key: str | None) -> None:
        if key is None or key in self._map_original:
            return
        self._map_original[key] = self.uuid_map.get(key, _UUID_MAP_MISSING)

    def set_user_uuid(self, user: Optional['User'], value: str | None) -> None:
        if not user:
            return
        self._capture_user_state(user)
        user.remnawave_uuid = value

    def set_user_updated_at(self, user: Optional['User'], value: datetime) -> None:
        if not user:
            return
        self._capture_user_state(user)
        user.updated_at = value

    def remove_map_entry(self, key: str | None) -> None:
        if key is None:
            return
        self._capture_map_entry(key)
        self.uuid_map.pop(key, None)

    def set_map_entry(self, key: str | None, value: Optional['User']) -> None:
        if key is None:
            return
        self._capture_map_entry(key)
        if value is None:
            self.uuid_map.pop(key, None)
        else:
            self.uuid_map[key] = value

    def has_changes(self) -> bool:
        return bool(self._map_original or self._user_original)

    def rollback(self) -> None:
        for user, (uuid_value, updated_at) in self._user_original.items():
            if uuid_value is not _ATTR_NOT_CAPTURED:
                user.remnawave_uuid = uuid_value
            if updated_at is not _ATTR_NOT_CAPTURED:
                user.updated_at = updated_at

        for key, original in self._map_original.items():
            if original is _UUID_MAP_MISSING:
                self.uuid_map.pop(key, None)
            else:
                self.uuid_map[key] = original


class RemnaWaveConfigurationError(Exception):
    """Raised when RemnaWave API configuration is missing."""


class RemnaWaveService:
    def __init__(self):
        auth_params = settings.get_remnawave_auth_params()
        base_url = (auth_params.get('base_url') or '').strip()
        api_key = (auth_params.get('api_key') or '').strip()

        self._config_error: str | None = None

        self._panel_timezone = get_local_timezone()
        self._utc_timezone = ZoneInfo('UTC')

        if not base_url:
            self._config_error = 'REMNAWAVE_API_URL не настроен'
        elif not api_key:
            self._config_error = 'REMNAWAVE_API_KEY не настроен'

        # Сохраняем параметры для создания новых экземпляров API клиента
        # (каждый вызов get_api_client создаёт свой экземпляр, чтобы
        # параллельные корутины не перезаписывали друг другу aiohttp-сессию)
        self._api_kwargs: dict | None = None
        if not self._config_error:
            self._api_kwargs = {
                'base_url': base_url,
                'api_key': api_key,
                'secret_key': auth_params.get('secret_key'),
                'username': auth_params.get('username'),
                'password': auth_params.get('password'),
                'caddy_token': auth_params.get('caddy_token'),
                'auth_type': auth_params.get('auth_type') or 'api_key',
            }

    @property
    def is_configured(self) -> bool:
        return self._config_error is None

    @property
    def configuration_error(self) -> str | None:
        return self._config_error

    def _ensure_configured(self) -> None:
        if not self.is_configured or self._api_kwargs is None:
            raise RemnaWaveConfigurationError(self._config_error or 'RemnaWave API не настроен')

    def _ensure_user_remnawave_uuid(
        self,
        user: 'User',
        panel_uuid: str | None,
        uuid_map: dict[str, 'User'],
    ) -> tuple[bool, _UUIDMapMutation | None]:
        """Обновляет UUID пользователя, если он изменился в панели."""

        if not panel_uuid:
            return False, None

        current_uuid = getattr(user, 'remnawave_uuid', None)
        if current_uuid == panel_uuid:
            return False, None

        mutation = _UUIDMapMutation(uuid_map)

        conflicting_user = uuid_map.get(panel_uuid)
        if conflicting_user and conflicting_user is not user:
            logger.warning(
                '♻️ Обнаружен конфликт UUID между пользователями и . Сбрасываем у старой записи.',
                panel_uuid=panel_uuid,
                getattr=getattr(conflicting_user, 'telegram_id', '?'),
                getattr_2=getattr(user, 'telegram_id', '?'),
            )
            mutation.set_user_uuid(conflicting_user, None)
            mutation.set_user_updated_at(conflicting_user, datetime.now(UTC))
            mutation.remove_map_entry(panel_uuid)

        if current_uuid:
            mutation.remove_map_entry(current_uuid)

        mutation.set_user_uuid(user, panel_uuid)
        mutation.set_user_updated_at(user, datetime.now(UTC))
        mutation.set_map_entry(panel_uuid, user)

        logger.info(
            '🔁 Обновлен RemnaWave UUID пользователя : →',
            getattr=getattr(user, 'telegram_id', '?'),
            current_uuid=current_uuid,
            panel_uuid=panel_uuid,
        )

        if mutation.has_changes():
            return True, mutation

        return True, None

    @asynccontextmanager
    async def get_api_client(self):
        self._ensure_configured()
        assert self._api_kwargs is not None
        api = RemnaWaveAPI(**self._api_kwargs)
        async with api:
            yield api

    def _now_utc(self) -> datetime:
        """Возвращает текущее время в UTC без привязки к часовому поясу."""
        return datetime.now(self._utc_timezone)

    def _local_to_utc(self, local_dt: datetime) -> datetime:
        """Конвертирует naive локальную дату (в таймзоне панели/бота) в naive UTC.

        Используется для корректного сравнения дат из БД с датами из RemnaWave.
        """
        if local_dt.tzinfo is not None:
            # Уже есть tzinfo - конвертируем напрямую
            return local_dt.astimezone(self._utc_timezone)
        # Naive datetime - интерпретируем как локальное время панели
        local_aware = local_dt.replace(tzinfo=self._panel_timezone)
        return local_aware.astimezone(self._utc_timezone)

    def _parse_remnawave_date(self, date_str: str) -> datetime:
        if not date_str:
            return self._now_utc() + timedelta(days=30)

        try:
            cleaned_date = date_str.strip()

            if cleaned_date.endswith('Z'):
                cleaned_date = cleaned_date[:-1] + '+00:00'

            if '+00:00+00:00' in cleaned_date:
                cleaned_date = cleaned_date.replace('+00:00+00:00', '+00:00')

            cleaned_date = re.sub(r'(\+\d{2}:\d{2})\+\d{2}:\d{2}$', r'\1', cleaned_date)

            parsed_date = datetime.fromisoformat(cleaned_date)

            # Панель RemnaWave всегда отдаёт время в UTC
            # Если есть tzinfo - конвертируем в UTC, иначе считаем что уже UTC
            if parsed_date.tzinfo is not None:
                utc_normalized = parsed_date.astimezone(self._utc_timezone)
            else:
                utc_normalized = parsed_date.replace(tzinfo=UTC)

            logger.debug('Успешно распарсена дата: (UTC)', date_str=date_str, utc_normalized=utc_normalized)
            return utc_normalized

        except Exception as e:
            logger.warning('⚠️ Не удалось распарсить дату . Используем дефолтную дату.', date_str=date_str, error=e)
            return self._now_utc() + timedelta(days=30)

    def _safe_expire_at_for_panel(self, expire_at: datetime | None) -> datetime:
        """Гарантирует, что дата окончания не в прошлом для панели.

        Принимает naive UTC datetime, возвращает naive datetime в таймзоне панели.
        """

        now = self._now_utc()
        minimum_expire = now + timedelta(minutes=1)

        if not expire_at:
            result = minimum_expire
        else:
            normalized_expire = expire_at

            if normalized_expire < minimum_expire:
                logger.debug(
                    '⚙️ Коррекция даты истечения до минимально допустимой для панели',
                    normalized_expire=normalized_expire,
                    minimum_expire=minimum_expire,
                )
                result = minimum_expire
            else:
                result = normalized_expire

        # Панель RemnaWave ожидает время в UTC
        return result

    def _safe_panel_expire_date(self, panel_user: dict[str, Any]) -> datetime:
        """Парсит дату окончания подписки пользователя панели для сравнения."""

        expire_at_value = panel_user.get('expireAt')

        if expire_at_value is None:
            return datetime.min.replace(tzinfo=UTC)

        expire_at_str = str(expire_at_value).strip()
        if not expire_at_str:
            return datetime.min.replace(tzinfo=UTC)

        return self._parse_remnawave_date(expire_at_str)

    def _is_preferred_panel_user(
        self,
        *,
        candidate: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        """Определяет, является ли новая запись предпочтительной для Telegram ID."""

        candidate_expire = self._safe_panel_expire_date(candidate)
        current_expire = self._safe_panel_expire_date(current)

        if candidate_expire > current_expire:
            return True
        if candidate_expire < current_expire:
            return False

        candidate_status = (candidate.get('status') or '').upper()
        current_status = (current.get('status') or '').upper()

        active_statuses = {'ACTIVE', 'TRIAL'}
        if candidate_status in active_statuses and current_status not in active_statuses:
            return True

        return False

    def _deduplicate_panel_users_by_telegram_id(
        self,
        panel_users: list[dict[str, Any]],
    ) -> dict[Any, dict[str, Any]]:
        """Возвращает уникальных пользователей панели по Telegram ID."""

        unique_users: dict[Any, dict[str, Any]] = {}

        for panel_user in panel_users:
            telegram_id = panel_user.get('telegramId')
            if telegram_id is None:
                continue

            existing_user = unique_users.get(telegram_id)
            if existing_user is None or self._is_preferred_panel_user(
                candidate=panel_user,
                current=existing_user,
            ):
                unique_users[telegram_id] = panel_user

        return unique_users

    def _extract_user_data_from_description(self, description: str) -> tuple[str | None, str | None, str | None]:
        """
        Извлекает имя, фамилию и username из описания пользователя в панели Remnawave.

        Args:
            description: Описание пользователя из панели

        Returns:
            Tuple[first_name, last_name, username] - извлеченные данные
        """
        logger.debug('📥 Парсинг описания пользователя', description=description)

        if not description:
            logger.debug('❌ Пустое описание пользователя')
            return None, None, None

        # Ищем строки в формате "Bot user: ..."
        import re

        # Паттерн для извлечения данных из "Bot user: Name @username" или "Bot user: Name"
        # Также поддерживаем просто "Name @username" без префикса
        bot_user_patterns = [
            r'Bot user:\s*(.+)',  # С префиксом
            r'^([\w\s]+(?:@[\w_]+)?)$',  # Без префикса
        ]

        user_info = None
        for pattern in bot_user_patterns:
            match = re.search(pattern, description)
            if match:
                user_info = match.group(1).strip()
                logger.debug('🔍 Найдена информация о пользователе', user_info=user_info)
                break

        if not user_info:
            logger.debug('❌ Не удалось найти информацию о пользователе в описании')
            return None, None, None

        # Паттерн для извлечения username (@username в конце)
        username_pattern = r'\s+(@[\w_]+)$'
        username_match = re.search(username_pattern, user_info)

        if username_match:
            username_with_at = username_match.group(1)
            username = username_with_at.removeprefix('@')  # Убираем символ @
            # Убираем username из основной информации
            name_part = user_info[: username_match.start()].strip()
            logger.debug(
                '📱 Найден username: (обработанный: ), остаток',
                username_with_at=username_with_at,
                username=username,
                name_part=name_part,
            )
        else:
            username = None
            name_part = user_info
            logger.debug('📱 Username не найден, имя', name_part=name_part)

        # Разделяем имя и фамилию
        if name_part and not name_part.startswith('@'):
            # Если есть имя (не начинается с @), используем его
            name_parts = name_part.split()
            logger.debug('🔤 Части имени', name_parts=name_parts)

            if len(name_parts) >= 2:
                # Первое слово - имя, остальные - фамилия
                first_name = name_parts[0]
                last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else None
                logger.debug('👤 Имя: Фамилия', first_name=first_name, last_name=last_name)
            elif len(name_parts) == 1 and not name_parts[0].startswith('@'):
                # Только имя
                first_name = name_parts[0]
                last_name = None
                logger.debug('👤 Только имя', first_name=first_name)
            else:
                first_name = None
                last_name = None
                logger.debug('👤 Имя не определено')
        else:
            first_name = None
            last_name = None
            logger.debug('👤 Имя не определено (начинается с @)')

        logger.debug(
            '✅ Результат парсинга: first_name=, last_name=, username',
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        return first_name, last_name, username

    async def _get_or_create_bot_user_from_panel(
        self,
        db: AsyncSession,
        panel_user: dict[str, Any],
    ) -> tuple[User | None, bool]:
        """Возвращает пользователя бота, создавая его при необходимости.

        При конфликте уникальности telegram_id повторно загружает пользователя
        из базы данных и сообщает, что запись не была создана заново.
        """

        telegram_id = panel_user.get('telegramId')
        if telegram_id is None:
            return None, False

        # Извлекаем настоящее имя пользователя из описания
        description = panel_user.get('description') or ''
        first_name_from_desc, last_name_from_desc, username_from_desc = self._extract_user_data_from_description(
            description
        )

        # Используем извлеченное имя или дефолтное значение
        fallback_first_name = f'User {telegram_id}'
        full_first_name = fallback_first_name
        full_last_name = None

        if (first_name_from_desc and last_name_from_desc) or first_name_from_desc:
            full_first_name = first_name_from_desc
            full_last_name = last_name_from_desc

        username = username_from_desc or panel_user.get('username')

        try:
            create_kwargs = dict(
                db=db,
                telegram_id=telegram_id,
                username=username,
                first_name=full_first_name,
                last_name=full_last_name,
                language='ru',
            )

            # Используем SAVEPOINT чтобы при IntegrityError откатить только
            # вложенную транзакцию, а не всю сессию. Полный rollback помечает
            # ВСЕ объекты сессии как expired, что вызывает MissingGreenlet
            # при последующем sync-доступе к атрибутам ORM-объектов.
            async with db.begin_nested():
                db_user = await create_user_no_commit(**create_kwargs)
            return db_user, True
        except IntegrityError as create_error:
            logger.info(
                '♻️ Пользователь с telegram_id уже существует. Используем существующую запись.', telegram_id=telegram_id
            )

            try:
                existing_user = await get_user_by_telegram_id(db, telegram_id)
                if existing_user is None:
                    logger.error(
                        '❌ Не удалось найти существующего пользователя с telegram_id', telegram_id=telegram_id
                    )
                    return None, False

                logger.debug(
                    'Используется существующий пользователь после конфликта уникальности',
                    telegram_id=telegram_id,
                    create_error=create_error,
                )
                return existing_user, False
            except Exception as load_error:
                logger.error(
                    '❌ Ошибка загрузки существующего пользователя', telegram_id=telegram_id, load_error=load_error
                )
                return None, False
        except Exception as general_error:
            # SAVEPOINT (begin_nested) уже откатил частичную работу.
            # Полный rollback не нужен — он бы пометил все объекты сессии expired.
            logger.error(
                '❌ Общая ошибка создания/загрузки пользователя', telegram_id=telegram_id, general_error=general_error
            )
            return None, False

    async def get_system_statistics(self) -> dict[str, Any]:
        try:
            async with self.get_api_client() as api:
                logger.info('Получение системной статистики RemnaWave...')

                try:
                    system_stats = await api.get_system_stats()
                    logger.info('Системная статистика получена')
                except Exception as e:
                    logger.error('Ошибка получения системной статистики', error=e)
                    system_stats = {}

                try:
                    bandwidth_stats = await api.get_bandwidth_stats()
                    logger.info('Статистика трафика получена')
                except Exception as e:
                    logger.error('Ошибка получения статистики трафика', error=e)
                    bandwidth_stats = {}

                try:
                    realtime_usage = await api.get_nodes_realtime_usage()
                    logger.info('Реалтайм статистика получена')
                except Exception as e:
                    logger.error('Ошибка получения реалтайм статистики', error=e)
                    realtime_usage = []

                try:
                    nodes_stats = await api.get_nodes_statistics()
                except Exception as e:
                    logger.error('Ошибка получения статистики нод', error=e)
                    nodes_stats = {}

                total_download = sum(node.get('downloadBytes', 0) for node in realtime_usage)
                total_upload = sum(node.get('uploadBytes', 0) for node in realtime_usage)
                total_realtime_traffic = total_download + total_upload

                total_user_traffic = int(system_stats.get('users', {}).get('totalTrafficBytes', '0'))

                nodes_weekly_data = []
                if nodes_stats.get('lastSevenDays'):
                    nodes_by_name = {}
                    for day_data in nodes_stats['lastSevenDays']:
                        node_name = day_data['nodeName']
                        if node_name not in nodes_by_name:
                            nodes_by_name[node_name] = {'name': node_name, 'total_bytes': 0, 'days_data': []}

                        daily_bytes = int(day_data['totalBytes'])
                        nodes_by_name[node_name]['total_bytes'] += daily_bytes
                        nodes_by_name[node_name]['days_data'].append({'date': day_data['date'], 'bytes': daily_bytes})

                    nodes_weekly_data = list(nodes_by_name.values())
                    nodes_weekly_data.sort(key=lambda x: x['total_bytes'], reverse=True)

                uptime_seconds = 0
                uptime_value = system_stats.get('uptime')
                try:
                    uptime_seconds = int(float(uptime_value)) if uptime_value is not None else 0
                except (TypeError, ValueError):
                    logger.warning('Не удалось преобразовать uptime в число, используем 0', uptime_value=uptime_value)

                result = {
                    'system': {
                        'users_online': system_stats.get('onlineStats', {}).get('onlineNow', 0),
                        'total_users': system_stats.get('users', {}).get('totalUsers', 0),
                        'active_connections': system_stats.get('onlineStats', {}).get('onlineNow', 0),
                        'nodes_online': system_stats.get('nodes', {}).get('totalOnline', 0),
                        'users_last_day': system_stats.get('onlineStats', {}).get('lastDay', 0),
                        'users_last_week': system_stats.get('onlineStats', {}).get('lastWeek', 0),
                        'users_never_online': system_stats.get('onlineStats', {}).get('neverOnline', 0),
                        'total_user_traffic': total_user_traffic,
                    },
                    'users_by_status': system_stats.get('users', {}).get('statusCounts', {}),
                    'server_info': {
                        'cpu_cores': system_stats.get('cpu', {}).get('cores', 0),
                        'memory_total': system_stats.get('memory', {}).get('total', 0),
                        'memory_used': system_stats.get('memory', {}).get('used', 0),
                        'memory_free': system_stats.get('memory', {}).get('free', 0),
                        'uptime_seconds': uptime_seconds,
                    },
                    'bandwidth': {
                        'realtime_download': total_download,
                        'realtime_upload': total_upload,
                        'realtime_total': total_realtime_traffic,
                    },
                    'traffic_periods': {
                        'last_2_days': {
                            'current': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLastTwoDays', {}).get('current', '0 B')
                            ),
                            'previous': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLastTwoDays', {}).get('previous', '0 B')
                            ),
                            'difference': bandwidth_stats.get('bandwidthLastTwoDays', {}).get('difference', '0 B'),
                        },
                        'last_7_days': {
                            'current': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLastSevenDays', {}).get('current', '0 B')
                            ),
                            'previous': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLastSevenDays', {}).get('previous', '0 B')
                            ),
                            'difference': bandwidth_stats.get('bandwidthLastSevenDays', {}).get('difference', '0 B'),
                        },
                        'last_30_days': {
                            'current': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLast30Days', {}).get('current', '0 B')
                            ),
                            'previous': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthLast30Days', {}).get('previous', '0 B')
                            ),
                            'difference': bandwidth_stats.get('bandwidthLast30Days', {}).get('difference', '0 B'),
                        },
                        'current_month': {
                            'current': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthCalendarMonth', {}).get('current', '0 B')
                            ),
                            'previous': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthCalendarMonth', {}).get('previous', '0 B')
                            ),
                            'difference': bandwidth_stats.get('bandwidthCalendarMonth', {}).get('difference', '0 B'),
                        },
                        'current_year': {
                            'current': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthCurrentYear', {}).get('current', '0 B')
                            ),
                            'previous': self._parse_bandwidth_string(
                                bandwidth_stats.get('bandwidthCurrentYear', {}).get('previous', '0 B')
                            ),
                            'difference': bandwidth_stats.get('bandwidthCurrentYear', {}).get('difference', '0 B'),
                        },
                    },
                    'nodes_realtime': realtime_usage,
                    'nodes_weekly': nodes_weekly_data,
                    'last_updated': datetime.now(UTC),
                }

                logger.info(
                    'Статистика сформирована: пользователи=, общий трафик',
                    result=result['system']['total_users'],
                    total_user_traffic=total_user_traffic,
                )
                return result

        except RemnaWaveAPIError as e:
            logger.error('Ошибка Remnawave API при получении статистики', error=e)
            return {'error': str(e)}
        except Exception as e:
            logger.error('Общая ошибка получения системной статистики', error=e)
            return {'error': f'Внутренняя ошибка сервера: {e!s}'}

    def _parse_bandwidth_string(self, bandwidth_str: str) -> int:
        try:
            if not bandwidth_str or bandwidth_str == '0 B' or bandwidth_str == '0':
                return 0

            bandwidth_str = bandwidth_str.replace(' ', '').upper()

            units = {
                'B': 1,
                'KB': 1024,
                'MB': 1024**2,
                'GB': 1024**3,
                'TB': 1024**4,
                'PB': 1024**5,
                'KIB': 1024,
                'MIB': 1024**2,
                'GIB': 1024**3,
                'TIB': 1024**4,
                'PIB': 1024**5,
                'KBPS': 1024,
                'MBPS': 1024**2,
                'GBPS': 1024**3,
                'TBPS': 1024**4,
            }

            match = re.match(r'([0-9.,]+)([A-Z]+)', bandwidth_str)
            if match:
                value_str = match.group(1).replace(',', '.')
                value = float(value_str)
                unit = match.group(2)

                if unit in units:
                    result = int(value * units[unit])
                    logger.debug('Парсинг = байт', bandwidth_str=bandwidth_str, value=value, unit=unit, result=result)
                    return result
                logger.warning('Неизвестная единица измерения', unit=unit)

            logger.warning('Не удалось распарсить строку трафика', bandwidth_str=bandwidth_str)
            return 0

        except Exception as e:
            logger.error('Ошибка парсинга строки трафика', bandwidth_str=bandwidth_str, error=e)
            return 0

    async def get_all_nodes(self) -> list[dict[str, Any]]:
        try:
            async with self.get_api_client() as api:
                nodes = await api.get_all_nodes()

                result = []
                for node in nodes:
                    result.append(
                        {
                            'uuid': node.uuid,
                            'name': node.name,
                            'address': node.address,
                            'country_code': node.country_code,
                            'is_connected': node.is_connected,
                            'is_disabled': node.is_disabled,
                            'is_node_online': node.is_node_online,
                            'is_xray_running': node.is_xray_running,
                            'users_online': node.users_online,
                            'traffic_used_bytes': node.traffic_used_bytes,
                            'traffic_limit_bytes': node.traffic_limit_bytes,
                            'xray_uptime': node.xray_uptime,
                            'versions': node.versions,
                            'system': node.system,
                            'active_plugin_uuid': node.active_plugin_uuid,
                        }
                    )

                logger.info('✅ Получено нод из Remnawave', result_count=len(result))
                return result

        except Exception as e:
            logger.error('Ошибка получения нод из Remnawave', error=e)
            return []

    async def test_connection(self) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.get_system_stats()
                logger.info('✅ Соединение с Remnawave API работает')
                return True

        except Exception as e:
            logger.error('❌ Ошибка соединения с Remnawave API', error=e)
            return False

    async def get_node_details(self, node_uuid: str) -> dict[str, Any] | None:
        try:
            async with self.get_api_client() as api:
                node = await api.get_node_by_uuid(node_uuid)

                if not node:
                    return None

                return {
                    'uuid': node.uuid,
                    'name': node.name,
                    'address': node.address,
                    'country_code': node.country_code,
                    'is_connected': node.is_connected,
                    'is_disabled': node.is_disabled,
                    'is_node_online': node.is_node_online,
                    'is_xray_running': node.is_xray_running,
                    'users_online': node.users_online,
                    'traffic_used_bytes': node.traffic_used_bytes or 0,
                    'traffic_limit_bytes': node.traffic_limit_bytes or 0,
                    'last_status_change': node.last_status_change,
                    'last_status_message': node.last_status_message,
                    'xray_uptime': node.xray_uptime,
                    'is_traffic_tracking_active': node.is_traffic_tracking_active,
                    'traffic_reset_day': node.traffic_reset_day,
                    'notify_percent': node.notify_percent,
                    'consumption_multiplier': node.consumption_multiplier,
                    'created_at': node.created_at,
                    'updated_at': node.updated_at,
                    'provider_uuid': node.provider_uuid,
                    'versions': node.versions,
                    'system': node.system,
                    'active_plugin_uuid': node.active_plugin_uuid,
                }

        except Exception as e:
            logger.error('Ошибка получения информации о ноде', node_uuid=node_uuid, error=e)
            return None

    async def manage_node(self, node_uuid: str, action: str) -> bool:
        try:
            async with self.get_api_client() as api:
                if action == 'enable':
                    await api.enable_node(node_uuid)
                elif action == 'disable':
                    await api.disable_node(node_uuid)
                elif action == 'restart':
                    await api.restart_node(node_uuid)
                else:
                    return False

                logger.info('✅ Действие выполнено для ноды', action=action, node_uuid=node_uuid)
                return True

        except Exception as e:
            logger.error('Ошибка управления нодой', node_uuid=node_uuid, error=e)
            return False

    async def restart_all_nodes(self, force_restart: bool = False) -> bool:
        try:
            async with self.get_api_client() as api:
                result = await api.restart_all_nodes(force_restart=force_restart)

                if result:
                    logger.info('✅ Команда перезагрузки всех нод отправлена')

                return result

        except Exception as e:
            logger.error('Ошибка перезагрузки всех нод', error=e)
            return False

    async def update_squad_inbounds(self, squad_uuid: str, inbound_uuids: list[str]) -> bool:
        try:
            async with self.get_api_client() as api:
                data = {'uuid': squad_uuid, 'inbounds': inbound_uuids}
                await api._make_request('PATCH', '/api/internal-squads', data)
                return True
        except Exception as e:
            logger.error('Error updating squad inbounds', error=e)
            return False

    async def get_all_squads(self) -> list[dict[str, Any]]:
        try:
            async with self.get_api_client() as api:
                squads = await api.get_internal_squads()

                result = []
                for squad in squads:
                    inbounds = [
                        asdict(inbound) if is_dataclass(inbound) else inbound for inbound in squad.inbounds or []
                    ]
                    result.append(
                        {
                            'uuid': squad.uuid,
                            'name': squad.name,
                            'members_count': squad.members_count,
                            'inbounds_count': squad.inbounds_count,
                            'inbounds': inbounds,
                        }
                    )

                logger.info('✅ Получено сквадов из Remnawave', result_count=len(result))
                return result

        except Exception as e:
            logger.error('Ошибка получения сквадов из Remnawave', error=e)
            return []

    async def create_squad(self, name: str, inbounds: list[str]) -> str | None:
        try:
            async with self.get_api_client() as api:
                squad = await api.create_internal_squad(name, inbounds)

                logger.info('✅ Создан новый сквад', name=name)
                return squad.uuid

        except Exception as e:
            logger.error('Ошибка создания сквада', name=name, error=e)
            return None

    async def update_squad(self, uuid: str, name: str = None, inbounds: list[str] = None) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.update_internal_squad(uuid, name, inbounds)

                logger.info('✅ Обновлен сквад', uuid=uuid)
                return True

        except Exception as e:
            logger.error('Ошибка обновления сквада', uuid=uuid, error=e)
            return False

    async def delete_squad(self, uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                result = await api.delete_internal_squad(uuid)

                if result:
                    logger.info('✅ Удален сквад', uuid=uuid)

                return result

        except Exception as e:
            logger.error('Ошибка удаления сквада', uuid=uuid, error=e)
            return False

    async def migrate_squad_users(
        self,
        db: AsyncSession,
        source_uuid: str,
        target_uuid: str,
    ) -> dict[str, Any]:
        """Переносит активных подписок с одного сквада на другой."""

        if source_uuid == target_uuid:
            return {
                'success': False,
                'error': 'same_squad',
                'message': 'Источник и назначение совпадают',
            }

        source_uuid = source_uuid.strip()
        target_uuid = target_uuid.strip()

        source_server = await get_server_squad_by_uuid(db, source_uuid)
        target_server = await get_server_squad_by_uuid(db, target_uuid)

        if not source_server or not target_server:
            return {
                'success': False,
                'error': 'not_found',
                'message': 'Сквады не найдены',
            }

        subscription_query = (
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                Subscription.status.in_(
                    [
                        SubscriptionStatus.ACTIVE.value,
                        SubscriptionStatus.TRIAL.value,
                    ]
                ),
                cast(Subscription.connected_squads, String).like(f'%"{source_uuid}"%'),
            )
        )

        result = await db.execute(subscription_query)
        subscriptions = result.scalars().unique().all()

        total_candidates = len(subscriptions)
        if not subscriptions:
            logger.info(
                '🚚 Переезд сквада → : подходящих подписок не найдено', source_uuid=source_uuid, target_uuid=target_uuid
            )
            return {
                'success': True,
                'total': 0,
                'updated': 0,
                'panel_updated': 0,
                'panel_failed': 0,
            }

        exit_stack = AsyncExitStack()
        panel_updated = 0
        panel_failed = 0
        updated_subscriptions = 0
        source_decrement = 0
        target_increment = 0

        try:
            needs_panel_update = any(
                (
                    subscription.remnawave_uuid
                    if settings.is_multi_tariff_enabled()
                    else (subscription.user and subscription.user.remnawave_uuid)
                )
                for subscription in subscriptions
            )

            api = None
            if needs_panel_update:
                api = await exit_stack.enter_async_context(self.get_api_client())

            for subscription in subscriptions:
                current_squads = list(subscription.connected_squads or [])
                if source_uuid not in current_squads:
                    continue

                had_target_before = target_uuid in current_squads
                new_squads = [squad_uuid for squad_uuid in current_squads if squad_uuid != source_uuid]
                if not had_target_before:
                    new_squads.append(target_uuid)

                _uuid = (
                    getattr(subscription, 'remnawave_uuid', None)
                    if settings.is_multi_tariff_enabled()
                    else (subscription.user.remnawave_uuid if subscription.user else None)
                )
                if _uuid:
                    if api is None:
                        panel_failed += 1
                        logger.error(
                            '❌ RemnaWave API недоступен для обновления пользователя',
                            telegram_id=subscription.user.telegram_id if subscription.user else None,
                        )
                        continue

                    try:
                        await api.update_user(
                            uuid=_uuid,
                            active_internal_squads=new_squads,
                        )
                        panel_updated += 1
                    except Exception as error:
                        panel_failed += 1
                        logger.error(
                            '❌ Ошибка обновления сквадов пользователя',
                            telegram_id=subscription.user.telegram_id,
                            error=error,
                        )
                        continue

                subscription.connected_squads = new_squads
                subscription.updated_at = datetime.now(UTC)

                source_decrement += 1
                if not had_target_before:
                    target_increment += 1

                updated_subscriptions += 1

                link_result = await db.execute(
                    select(SubscriptionServer)
                    .where(
                        and_(
                            SubscriptionServer.subscription_id == subscription.id,
                            SubscriptionServer.server_squad_id == source_server.id,
                        )
                    )
                    .limit(1)
                )
                link = link_result.scalars().first()

                if link:
                    if had_target_before:
                        await db.execute(
                            delete(SubscriptionServer).where(
                                and_(
                                    SubscriptionServer.subscription_id == subscription.id,
                                    SubscriptionServer.server_squad_id == source_server.id,
                                )
                            )
                        )
                    else:
                        link.server_squad_id = target_server.id
                elif not had_target_before:
                    db.add(
                        SubscriptionServer(
                            subscription_id=subscription.id,
                            server_squad_id=target_server.id,
                            paid_price_kopeks=0,
                        )
                    )

            if updated_subscriptions:
                # Update in consistent ID order to prevent deadlocks
                counter_updates = {}
                if source_decrement:
                    counter_updates[source_server.id] = func.greatest(ServerSquad.current_users - source_decrement, 0)
                if target_increment:
                    counter_updates[target_server.id] = ServerSquad.current_users + target_increment
                for sid in sorted(counter_updates):
                    await db.execute(
                        update(ServerSquad).where(ServerSquad.id == sid).values(current_users=counter_updates[sid])
                    )

                await db.commit()
            else:
                await db.rollback()

            logger.info(
                '🚚 Завершен переезд сквада → : обновлено подписок (не обновлены в панели)',
                source_uuid=source_uuid,
                target_uuid=target_uuid,
                updated_subscriptions=updated_subscriptions,
                panel_failed=panel_failed,
            )

            return {
                'success': True,
                'total': total_candidates,
                'updated': updated_subscriptions,
                'panel_updated': panel_updated,
                'panel_failed': panel_failed,
                'source_removed': source_decrement,
                'target_added': target_increment,
            }

        except RemnaWaveConfigurationError:
            await db.rollback()
            raise
        except Exception as error:
            await db.rollback()
            logger.error('❌ Ошибка переезда сквада →', source_uuid=source_uuid, target_uuid=target_uuid, error=error)
            return {
                'success': False,
                'error': 'unexpected',
                'message': str(error),
            }
        finally:
            await exit_stack.aclose()

    async def sync_users_from_panel(self, db: AsyncSession, sync_type: str = 'all') -> dict[str, int]:
        # In multi-tariff mode, match panel users to subscriptions by remnawave_uuid
        if settings.is_multi_tariff_enabled():
            return await self._sync_users_from_panel_multi(db, sync_type)

        try:
            stats = {'created': 0, 'updated': 0, 'errors': 0, 'deleted': 0}

            logger.info('🔄 Начинаем синхронизацию типа', sync_type=sync_type)

            async with self.get_api_client() as api:
                panel_users = []
                start = 0
                size = 500  # Увеличен размер батча для ускорения загрузки

                while True:
                    logger.info('📥 Загружаем пользователей: start=, size', start=start, size=size)

                    # enrich_happ_links=False - happ_crypto_link уже возвращается API в поле happ.cryptoLink
                    # Не делаем дополнительные HTTP-запросы для каждого пользователя
                    response = await api.get_all_users(start=start, size=size, enrich_happ_links=False)
                    users_batch = response['users']
                    total_users = response['total']

                    logger.info(
                        '📊 Получено пользователей из', users_batch_count=len(users_batch), total_users=total_users
                    )

                    for user_obj in users_batch:
                        user_dict = {
                            'uuid': user_obj.uuid,
                            'shortUuid': user_obj.short_uuid,
                            'username': user_obj.username,
                            'status': user_obj.status.value,
                            'telegramId': user_obj.telegram_id,
                            'email': user_obj.email,  # Email для синхронизации email-only пользователей
                            'expireAt': user_obj.expire_at.isoformat(),
                            'trafficLimitBytes': user_obj.traffic_limit_bytes,
                            'usedTrafficBytes': user_obj.used_traffic_bytes,
                            'hwidDeviceLimit': user_obj.hwid_device_limit,
                            'subscriptionUrl': user_obj.subscription_url,
                            'subscriptionCryptoLink': user_obj.happ_crypto_link,
                            'activeInternalSquads': user_obj.active_internal_squads,
                        }
                        panel_users.append(user_dict)

                    if len(users_batch) < size:
                        break

                    start += size

                    if start > total_users:
                        break

                logger.info('✅ Всего загружено пользователей из панели', panel_users_count=len(panel_users))

            # Загрузка пользователей с их подписками за один запрос (bulk loading)
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.database.models import Subscription, User

            # Получаем всех пользователей с их подписками за один запрос
            bot_users_result = await db.execute(
                select(User).options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
            )
            bot_users = bot_users_result.scalars().all()
            # Filter out email-only users (telegram_id=None) to avoid None key issues
            bot_users_by_telegram_id = {user.telegram_id: user for user in bot_users if user.telegram_id is not None}
            bot_users_by_uuid = {
                user.remnawave_uuid: user for user in bot_users if getattr(user, 'remnawave_uuid', None)
            }
            # Index users by email for email-only sync
            bot_users_by_email = {user.email.lower(): user for user in bot_users if user.email and user.email_verified}
            # Also index email-only users by their remnawave_uuid for sync
            email_users_count = sum(1 for u in bot_users if u.telegram_id is None)
            if email_users_count > 0:
                logger.info('📧 Email-only пользователей (без telegram_id)', email_users_count=email_users_count)

            logger.info('📊 Пользователей в боте', bot_users_count=len(bot_users))

            panel_users_with_tg = [user for user in panel_users if user.get('telegramId') is not None]

            logger.info('📊 Пользователей в панели с Telegram ID', panel_users_with_tg_count=len(panel_users_with_tg))

            unique_panel_users_map = self._deduplicate_panel_users_by_telegram_id(panel_users_with_tg)
            unique_panel_users = list(unique_panel_users_map.values())
            duplicates_count = len(panel_users_with_tg) - len(unique_panel_users)

            if duplicates_count:
                logger.info(
                    '♻️ Обнаружено дубликатов пользователей по Telegram ID. Используем самые свежие записи.',
                    duplicates_count=duplicates_count,
                )

            panel_telegram_ids = set(unique_panel_users_map.keys())

            # Email-only пользователи из панели (без telegram_id, но с email)
            panel_users_email_only = [
                user for user in panel_users if user.get('telegramId') is None and user.get('email')
            ]
            if panel_users_email_only:
                logger.info(
                    '📧 Пользователей в панели с Email (без Telegram)',
                    panel_users_email_only_count=len(panel_users_email_only),
                )

            # Для ускорения - подготовим данные о подписках
            # Соберем все существующие подписки за один запрос
            existing_subscriptions_result = await db.execute(
                select(Subscription).join(User).options(selectinload(Subscription.user))
            )
            existing_subscriptions = existing_subscriptions_result.scalars().all()

            # Создадим словарь для быстрого доступа к подпискам
            {sub.user_id: sub for sub in existing_subscriptions}

            # Для оптимизации коммитим изменения каждые N пользователей
            batch_size = 50
            pending_uuid_mutations: list[_UUIDMapMutation] = []

            for i, panel_user in enumerate(unique_panel_users):
                uuid_mutation: _UUIDMapMutation | None = None
                try:
                    telegram_id = panel_user.get('telegramId')
                    if not telegram_id:
                        continue

                    if (i + 1) % 10 == 0:
                        logger.info(
                            '🔄 Обрабатываем пользователя /',
                            i=i + 1,
                            unique_panel_users_count=len(unique_panel_users),
                            telegram_id=telegram_id,
                        )

                    db_user = bot_users_by_telegram_id.get(telegram_id)

                    if not db_user:
                        if sync_type in ['new_only', 'all']:
                            logger.info('🆕 Создание пользователя для telegram_id', telegram_id=telegram_id)

                            db_user, is_created = await self._get_or_create_bot_user_from_panel(db, panel_user)

                            if not db_user:
                                logger.error(
                                    '❌ Не удалось создать или получить пользователя для telegram_id',
                                    telegram_id=telegram_id,
                                )
                                stats['errors'] += 1
                                continue

                            bot_users_by_telegram_id[telegram_id] = db_user

                            # При синхронизации не обновляем имя и username пользователя
                            # только сохраняем изменения, если были обновлены другие поля (подписка и т.д.)
                            updated_fields = []
                            # Если были обновлены другие поля (подписка, статус и т.д.), сохраняем изменения
                            if updated_fields:
                                logger.info(
                                    '🔄 Обновлены поля для пользователя',
                                    updated_fields=updated_fields,
                                    telegram_id=telegram_id,
                                )
                                await db.flush()  # Сохраняем изменения без коммита

                            _, uuid_mutation = self._ensure_user_remnawave_uuid(
                                db_user,
                                panel_user.get('uuid'),
                                bot_users_by_uuid,
                            )

                            if is_created:
                                await self._create_subscription_from_panel_data(db, db_user, panel_user)
                                stats['created'] += 1
                                logger.info('✅ Создан пользователь с подпиской', telegram_id=telegram_id)
                            else:
                                # Обновляем данные существующего пользователя
                                # Но теперь мы уже загрузили подписку с пользователем, нет необходимости перезагружать
                                await self._update_subscription_from_panel_data(db, db_user, panel_user)
                                stats['updated'] += 1
                                logger.info('♻️ Обновлена подписка существующего пользователя', telegram_id=telegram_id)

                    elif sync_type in ['update_only', 'all']:
                        logger.debug('🔄 Обновление пользователя', telegram_id=telegram_id)

                        # Refresh expired ORM-объекты перед sync-доступом.
                        # После SAVEPOINT rollback или других операций атрибуты
                        # могут быть expired, что вызывает MissingGreenlet в sync-коде.
                        from sqlalchemy import inspect as sa_inspect

                        user_state = sa_inspect(db_user)
                        if user_state.expired_attributes:
                            await db.refresh(db_user)

                        # Обновляем UUID ДО операций с подпиской
                        _, uuid_mutation = self._ensure_user_remnawave_uuid(
                            db_user,
                            panel_user.get('uuid'),
                            bot_users_by_uuid,
                        )

                        # Используем async запрос вместо доступа к relationship,
                        # чтобы избежать lazy-load в async контексте
                        if settings.is_multi_tariff_enabled():
                            from app.database.crud.subscription import get_active_subscriptions_by_user_id as _get_subs

                            _subs = await _get_subs(db, db_user.id)
                            # Match by remnawave_uuid from panel
                            existing_sub = next((s for s in _subs if s.remnawave_uuid == panel_user.get('uuid')), None)
                            if not existing_sub and _subs:
                                # No UUID match — fall back to best non-daily subscription
                                _non_daily = [s for s in _subs if not getattr(s, 'is_daily_tariff', False)]
                                _pool = _non_daily or _subs
                                existing_sub = max(_pool, key=lambda s: s.days_left)
                        else:
                            from app.database.crud.subscription import get_subscription_by_user_id as _get_sub

                            existing_sub = await _get_sub(db, db_user.id)
                        if existing_sub:
                            await self._update_subscription_from_panel_data(db, db_user, panel_user)
                        else:
                            await self._create_subscription_from_panel_data(db, db_user, panel_user)

                        stats['updated'] += 1
                        logger.debug('✅ Обновлён пользователь', telegram_id=telegram_id)

                except Exception as user_error:
                    logger.error(
                        '❌ Ошибка обработки пользователя',
                        telegram_id=telegram_id,
                        user_error=user_error,
                        exc_info=True,
                    )
                    stats['errors'] += 1
                    if uuid_mutation:
                        uuid_mutation.rollback()
                    if pending_uuid_mutations:
                        for mutation in reversed(pending_uuid_mutations):
                            mutation.rollback()
                        pending_uuid_mutations.clear()
                    try:
                        await db.rollback()  # Выполняем rollback при ошибке
                    except Exception:
                        pass
                    # After rollback all ORM objects in the session are expired.
                    # Accessing their attributes triggers a lazy load which fails
                    # in async context (greenlet_spawn error).  Break the loop to
                    # prevent cascading failures for every remaining user.
                    logger.warning(
                        '⚠️ Сессия повреждена после rollback, прерываем обработку (обработано / пользователей)',
                        i=i + 1,
                        unique_panel_users_count=len(unique_panel_users),
                    )
                    break

                else:
                    if uuid_mutation and uuid_mutation.has_changes():
                        pending_uuid_mutations.append(uuid_mutation)

                # Коммитим изменения каждые N пользователей для ускорения
                if (i + 1) % batch_size == 0:
                    try:
                        await db.commit()
                        logger.debug('📦 Коммит изменений после обработки пользователей', i=i + 1)
                        pending_uuid_mutations.clear()
                    except Exception as commit_error:
                        logger.error(
                            '❌ Ошибка коммита после обработки пользователей', i=i + 1, commit_error=commit_error
                        )
                        await db.rollback()
                        for mutation in reversed(pending_uuid_mutations):
                            mutation.rollback()
                        pending_uuid_mutations.clear()
                        stats['errors'] += batch_size  # Учитываем ошибки за всю группу

            # Коммитим оставшиеся изменения
            try:
                await db.commit()
                pending_uuid_mutations.clear()
            except Exception as final_commit_error:
                logger.error('❌ Ошибка финального коммита', final_commit_error=final_commit_error)
                await db.rollback()
                for mutation in reversed(pending_uuid_mutations):
                    mutation.rollback()
                pending_uuid_mutations.clear()

            # Обработка email-only пользователей из панели
            if panel_users_email_only and sync_type in ['new_only', 'all']:
                logger.info(
                    '📧 Обработка email-only пользователей из панели...',
                    panel_users_email_only_count=len(panel_users_email_only),
                )

                for panel_user in panel_users_email_only:
                    try:
                        panel_email = panel_user.get('email', '').lower()
                        panel_uuid = panel_user.get('uuid')

                        if not panel_email:
                            continue

                        # Ищем пользователя по email в боте
                        db_user = bot_users_by_email.get(panel_email)

                        # Если не нашли по email, ищем по UUID
                        if not db_user and panel_uuid:
                            db_user = bot_users_by_uuid.get(panel_uuid)

                        if db_user:
                            # Обновляем существующего пользователя
                            # Обновляем remnawave_uuid если нет
                            if panel_uuid and not db_user.remnawave_uuid:
                                db_user.remnawave_uuid = panel_uuid

                            # Используем async запрос вместо доступа к relationship
                            if settings.is_multi_tariff_enabled():
                                from app.database.crud.subscription import (
                                    get_active_subscriptions_by_user_id as _get_subs_email,
                                )

                                _subs_e = await _get_subs_email(db, db_user.id)
                                existing_sub = next(
                                    (s for s in _subs_e if s.remnawave_uuid == panel_user.get('uuid')),
                                    None,
                                )
                                if not existing_sub and _subs_e:
                                    # No UUID match — fall back to best non-daily subscription
                                    _non_daily_e = [s for s in _subs_e if not getattr(s, 'is_daily_tariff', False)]
                                    _pool_e = _non_daily_e or _subs_e
                                    existing_sub = max(_pool_e, key=lambda s: s.days_left)
                            else:
                                from app.database.crud.subscription import get_subscription_by_user_id as _get_sub_email

                                existing_sub = await _get_sub_email(db, db_user.id)
                            if existing_sub:
                                await self._update_subscription_from_panel_data(db, db_user, panel_user)
                            else:
                                await self._create_subscription_from_panel_data(db, db_user, panel_user)

                            stats['updated'] += 1
                            logger.info('📧 Обновлен email-пользователь', panel_email=panel_email)
                        else:
                            # Email-only пользователи не создаются автоматически при синхронизации,
                            # они должны сначала зарегистрироваться через cabinet
                            logger.debug('📧 Email-пользователь не найден в боте, пропускаем', panel_email=panel_email)

                    except Exception as email_user_error:
                        logger.error('❌ Ошибка обработки email-пользователя', email_user_error=email_user_error)
                        stats['errors'] += 1

                try:
                    await db.commit()
                except Exception as email_commit_error:
                    logger.error('❌ Ошибка коммита email-пользователей', email_commit_error=email_commit_error)
                    await db.rollback()

            if sync_type == 'all':
                logger.info('🗑️ Деактивация подписок пользователей, отсутствующих в панели...')

                batch_size = 50
                processed_count = 0
                cleanup_uuid_mutations: list[_UUIDMapMutation] = []

                # Собираем список пользователей для деактивации
                users_to_deactivate = [
                    (telegram_id, db_user)
                    for telegram_id, db_user in bot_users_by_telegram_id.items()
                    if telegram_id not in panel_telegram_ids
                    and any(True for _ in (getattr(db_user, 'subscriptions', None) or []))
                ]

                if users_to_deactivate:
                    logger.info(
                        '📊 Найдено пользователей для деактивации', users_to_deactivate_count=len(users_to_deactivate)
                    )

                # Используем один API клиент для всех операций сброса HWID
                hwid_api_cm = None
                try:
                    hwid_api_cm = self.get_api_client()
                    await hwid_api_cm.__aenter__()
                except Exception as api_init_error:
                    logger.warning('⚠️ Не удалось создать API клиент для сброса HWID', api_init_error=api_init_error)
                    hwid_api_cm = None

                try:
                    for telegram_id, db_user in users_to_deactivate:
                        cleanup_mutation: _UUIDMapMutation | None = None
                        try:
                            user_subscriptions = getattr(db_user, 'subscriptions', None) or []

                            # Skip if all subscriptions were recently updated by webhook
                            from app.database.crud.subscription import is_recently_updated_by_webhook

                            all_recently_updated = all(
                                is_recently_updated_by_webhook(subscription) for subscription in user_subscriptions
                            )
                            if user_subscriptions and all_recently_updated:
                                logger.debug(
                                    'Пропуск деактивации подписок: все обновлены вебхуком недавно',
                                    telegram_id=telegram_id,
                                )
                                continue

                            logger.info('🗑️ Деактивация подписок пользователя (нет в панели)', telegram_id=telegram_id)

                            # NOTE: Не сбрасываем HWID здесь — пользователь уже удалён из панели,
                            # API вернёт 404, UUID очищается ниже (cleanup_mutation)

                            for subscription in user_subscriptions:
                                if is_recently_updated_by_webhook(subscription):
                                    logger.debug(
                                        'Пропуск деактивации подписки: обновлена вебхуком недавно',
                                        subscription_id=subscription.id,
                                    )
                                    continue

                                try:
                                    from sqlalchemy import delete

                                    from app.database.models import SubscriptionServer

                                    await decrement_subscription_server_counts(db, subscription)

                                    await db.execute(
                                        delete(SubscriptionServer).where(
                                            SubscriptionServer.subscription_id == subscription.id
                                        )
                                    )
                                    logger.info(
                                        '🗑️ Удалены серверы подписки для',
                                        telegram_id=telegram_id,
                                        subscription_id=subscription.id,
                                    )
                                except Exception as servers_error:
                                    logger.warning(
                                        '⚠️ Не удалось удалить серверы подписки',
                                        servers_error=servers_error,
                                        subscription_id=subscription.id,
                                    )

                                from app.database.models import SubscriptionStatus

                                # Проверяем, была ли это платная подписка
                                was_paid = not subscription.is_trial or getattr(
                                    db_user, 'has_had_paid_subscription', False
                                )

                                subscription.status = SubscriptionStatus.DISABLED.value

                                if was_paid:
                                    # Для платных подписок - НЕ сбрасываем is_trial и end_date!
                                    # Сохраняем оригинальные значения чтобы можно было восстановить
                                    logger.warning(
                                        '⚠️ ПЛАТНАЯ подписка пользователя отключена (нет в панели), но is_trial= и end_date= СОХРАНЕНЫ',
                                        telegram_id=telegram_id,
                                        subscription_id=subscription.id,
                                        is_trial=subscription.is_trial,
                                        end_date=subscription.end_date,
                                    )
                                else:
                                    # Для триальных подписок - сбрасываем как раньше
                                    subscription.is_trial = True
                                    subscription.end_date = datetime.now(UTC)
                                    subscription.traffic_limit_gb = 0
                                    subscription.traffic_used_gb = 0.0
                                    subscription.device_limit = 1

                                subscription.connected_squads = []
                                subscription.autopay_enabled = False
                                subscription.remnawave_short_uuid = None
                                subscription.subscription_url = ''
                                subscription.subscription_crypto_link = ''

                            old_uuid = getattr(db_user, 'remnawave_uuid', None)
                            cleanup_mutation = _UUIDMapMutation(bot_users_by_uuid)
                            if old_uuid:
                                cleanup_mutation.remove_map_entry(old_uuid)
                            cleanup_mutation.set_user_uuid(db_user, None)
                            cleanup_mutation.set_user_updated_at(db_user, datetime.now(UTC))

                            stats['deleted'] += 1
                            logger.info(
                                '✅ Деактивированы подписки пользователя (сохранен баланс)', telegram_id=telegram_id
                            )

                            processed_count += 1

                        except Exception as delete_error:
                            logger.error(
                                '❌ Ошибка деактивации подписки', telegram_id=telegram_id, delete_error=delete_error
                            )
                            stats['errors'] += 1
                            if cleanup_mutation:
                                cleanup_mutation.rollback()
                            if cleanup_uuid_mutations:
                                for mutation in reversed(cleanup_uuid_mutations):
                                    mutation.rollback()
                                cleanup_uuid_mutations.clear()
                            try:
                                await db.rollback()
                            except:
                                pass
                        else:
                            if cleanup_mutation and cleanup_mutation.has_changes():
                                cleanup_uuid_mutations.append(cleanup_mutation)

                            # Коммитим изменения каждые N пользователей
                            if processed_count % batch_size == 0:
                                try:
                                    await db.commit()
                                    logger.debug(
                                        '📦 Коммит изменений после деактивации подписок',
                                        processed_count=processed_count,
                                    )
                                    cleanup_uuid_mutations.clear()
                                except Exception as commit_error:
                                    logger.error(
                                        '❌ Ошибка коммита после деактивации подписок',
                                        processed_count=processed_count,
                                        commit_error=commit_error,
                                    )
                                    await db.rollback()
                                    for mutation in reversed(cleanup_uuid_mutations):
                                        mutation.rollback()
                                    cleanup_uuid_mutations.clear()
                                    stats['errors'] += batch_size
                                    break  # Прерываем цикл при ошибке коммита

                    # Коммитим оставшиеся изменения
                    try:
                        await db.commit()
                        cleanup_uuid_mutations.clear()
                    except Exception as final_commit_error:
                        logger.error(
                            '❌ Ошибка финального коммита при деактивации', final_commit_error=final_commit_error
                        )
                        await db.rollback()
                        for mutation in reversed(cleanup_uuid_mutations):
                            mutation.rollback()
                        cleanup_uuid_mutations.clear()

                finally:
                    # Закрываем API клиент
                    if hwid_api_cm:
                        try:
                            await hwid_api_cm.__aexit__(None, None, None)
                        except Exception:
                            pass

            logger.info(
                '🎯 Синхронизация завершена: создано обновлено деактивировано ошибок',
                stats=stats['created'],
                stats_2=stats['updated'],
                stats_3=stats['deleted'],
                stats_4=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('❌ Критическая ошибка синхронизации пользователей', error=e)
            return {'created': 0, 'updated': 0, 'errors': 1, 'deleted': 0}

    async def _sync_users_from_panel_multi(self, db: AsyncSession, sync_type: str) -> dict[str, int]:
        """Multi-tariff sync: match panel users to subscriptions by remnawave_uuid."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.database.models import Subscription

        stats = {'created': 0, 'updated': 0, 'errors': 0, 'deleted': 0}
        try:
            logger.info('🔄 [multi-tariff] Начинаем синхронизацию типа', sync_type=sync_type)

            # Load all panel users
            async with self.get_api_client() as api:
                panel_users = []
                start = 0
                size = 500

                while True:
                    response = await api.get_all_users(start=start, size=size, enrich_happ_links=False)
                    users_batch = response['users']
                    total_users = response['total']
                    for user_obj in users_batch:
                        panel_users.append(
                            {
                                'uuid': user_obj.uuid,
                                'shortUuid': user_obj.short_uuid,
                                'username': user_obj.username,
                                'status': user_obj.status.value,
                                'telegramId': user_obj.telegram_id,
                                'email': user_obj.email,
                                'expireAt': user_obj.expire_at.isoformat(),
                                'usedTrafficBytes': user_obj.used_traffic_bytes,
                                'trafficLimitBytes': user_obj.traffic_limit_bytes,
                                'hwidDeviceLimit': user_obj.hwid_device_limit,
                                'subscriptionUrl': user_obj.subscription_url,
                                'subscriptionCryptoLink': user_obj.happ_crypto_link,
                                'activeInternalSquads': user_obj.active_internal_squads,
                            }
                        )
                    if len(users_batch) < size:
                        break
                    start += size
                    if start > total_users:
                        break

            logger.info('✅ [multi-tariff] Загружено из панели', panel_users_count=len(panel_users))

            # Load all subscriptions with remnawave_uuid
            subs_result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
                .where(Subscription.remnawave_uuid.isnot(None))
            )
            all_subs = subs_result.scalars().all()
            subs_by_uuid = {sub.remnawave_uuid: sub for sub in all_subs}

            # Fallback: build user-level UUID → user map for legacy migration
            from app.database.models import User

            users_result = await db.execute(
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .where(User.remnawave_uuid.isnot(None))
            )
            users_by_uuid = {u.remnawave_uuid: u for u in users_result.scalars().all() if u.remnawave_uuid}

            # Load all bot users for matching unlinked panel users
            all_users_result = await db.execute(select(User).options(selectinload(User.subscriptions)))
            _all_users = all_users_result.scalars().all()
            bot_users_by_tg = {u.telegram_id: u for u in _all_users if u.telegram_id}
            bot_users_by_email = {u.email.lower(): u for u in _all_users if u.email and u.email_verified}

            logger.info(
                '📊 [multi-tariff] Подписок с remnawave_uuid',
                subs_count=len(subs_by_uuid),
                users_legacy_count=len(users_by_uuid),
                bot_users_by_tg=len(bot_users_by_tg),
                bot_users_by_email=len(bot_users_by_email),
            )

            # Match and update
            for panel_user in panel_users:
                panel_uuid = panel_user.get('uuid')
                if not panel_uuid:
                    continue

                subscription = subs_by_uuid.get(panel_uuid)
                if not subscription:
                    # Fallback: check if this UUID belongs to a user (legacy single-tariff)
                    # and auto-link it to the user's best active subscription
                    legacy_user = users_by_uuid.get(panel_uuid)
                    if legacy_user:
                        user_subs = getattr(legacy_user, 'subscriptions', []) or []
                        active = [s for s in user_subs if s.status in ('active', 'trial')]
                        if active:
                            non_daily = [s for s in active if not getattr(s, 'is_daily_tariff', False)]
                            pool = non_daily or active
                            best = max(pool, key=lambda s: s.days_left)
                            if not best.remnawave_uuid:
                                best.remnawave_uuid = panel_uuid
                                subs_by_uuid[panel_uuid] = best
                                subscription = best
                                logger.info(
                                    '🔗 [multi-tariff] Привязан legacy UUID к подписке',
                                    panel_uuid=panel_uuid,
                                    subscription_id=best.id,
                                    user_id=legacy_user.id,
                                )

                if not subscription:
                    # Try to match panel user to a bot user and create subscription
                    _panel_tg = panel_user.get('telegramId')
                    _panel_email = (panel_user.get('email') or '').lower().strip()
                    _bot_user = None
                    if _panel_tg:
                        _bot_user = bot_users_by_tg.get(_panel_tg)
                    if not _bot_user and _panel_email:
                        _bot_user = bot_users_by_email.get(_panel_email)

                    if not _bot_user:
                        logger.debug(
                            '⚠️ [multi-tariff] Panel user has no matching bot user',
                            panel_uuid=panel_uuid,
                            username=panel_user.get('username'),
                        )
                        continue

                    # Check MAX_ACTIVE_SUBSCRIPTIONS
                    _user_subs = getattr(_bot_user, 'subscriptions', []) or []
                    _active_count = sum(1 for s in _user_subs if s.status in ('active', 'trial'))
                    if _active_count >= settings.get_max_active_subscriptions():
                        logger.debug(
                            '⚠️ [multi-tariff] User at max subscriptions, skipping',
                            user_id=_bot_user.id,
                            active_count=_active_count,
                        )
                        continue

                    # Check if subscription with this UUID already exists for this user
                    if any(s.remnawave_uuid == panel_uuid for s in _user_subs):
                        continue

                    try:
                        from app.database.crud.subscription import generate_unique_short_id

                        _expire_at = self._parse_remnawave_date(panel_user.get('expireAt', ''))
                        _now = self._now_utc()
                        _panel_status = panel_user.get('status', 'ACTIVE')
                        if _panel_status == 'ACTIVE' and _expire_at > _now:
                            _sub_status = SubscriptionStatus.ACTIVE
                        elif _expire_at <= _now:
                            _sub_status = SubscriptionStatus.EXPIRED
                        else:
                            _sub_status = SubscriptionStatus.DISABLED

                        _traffic_limit_bytes = panel_user.get('trafficLimitBytes', 0) or 0
                        _used_bytes = panel_user.get('usedTrafficBytes', 0) or 0
                        _squads = panel_user.get('activeInternalSquads', []) or []
                        _squad_uuids = []
                        if isinstance(_squads, list):
                            for _sq in _squads:
                                if isinstance(_sq, dict) and 'uuid' in _sq:
                                    _squad_uuids.append(_sq['uuid'])
                                elif isinstance(_sq, str):
                                    _squad_uuids.append(_sq)

                        _short_id = await generate_unique_short_id(db)

                        new_sub = Subscription(
                            user_id=_bot_user.id,
                            status=_sub_status.value,
                            is_trial=False,
                            end_date=_expire_at,
                            traffic_limit_gb=_traffic_limit_bytes // (1024**3) if _traffic_limit_bytes > 0 else 0,
                            traffic_used_gb=_used_bytes / (1024**3),
                            device_limit=panel_user.get('hwidDeviceLimit', 1) or 1,
                            connected_squads=_squad_uuids,
                            remnawave_uuid=panel_uuid,
                            remnawave_short_id=_short_id,
                            remnawave_short_uuid=panel_user.get('shortUuid'),
                            subscription_url=panel_user.get('subscriptionUrl', ''),
                            subscription_crypto_link=panel_user.get('subscriptionCryptoLink', ''),
                        )
                        db.add(new_sub)
                        subs_by_uuid[panel_uuid] = new_sub
                        # Keep in-memory state consistent for subsequent iterations
                        if hasattr(_bot_user, 'subscriptions') and isinstance(_bot_user.subscriptions, list):
                            _bot_user.subscriptions.append(new_sub)
                        stats['created'] += 1
                        logger.info(
                            '✅ [multi-tariff] Создана подписка из панели',
                            panel_uuid=panel_uuid,
                            user_id=_bot_user.id,
                        )
                    except Exception as create_err:
                        logger.error(
                            '❌ [multi-tariff] Ошибка создания подписки из панели',
                            panel_uuid=panel_uuid,
                            error=create_err,
                        )
                        stats['errors'] += 1
                    continue

                try:
                    # Update traffic
                    used_traffic_bytes = panel_user.get('usedTrafficBytes', 0) or 0
                    traffic_used_gb = used_traffic_bytes / (1024**3)
                    if abs(subscription.traffic_used_gb - traffic_used_gb) > 0.01:
                        subscription.traffic_used_gb = traffic_used_gb

                    # traffic_limit_gb: bot is source of truth, do not overwrite from panel

                    # Update subscription URL
                    sub_url = panel_user.get('subscriptionUrl')
                    if sub_url and subscription.subscription_url != sub_url:
                        subscription.subscription_url = sub_url

                    crypto_link = panel_user.get('subscriptionCryptoLink')
                    if crypto_link and subscription.subscription_crypto_link != crypto_link:
                        subscription.subscription_crypto_link = crypto_link

                    stats['updated'] += 1
                except Exception as e:
                    logger.error(
                        '❌ [multi-tariff] Ошибка обновления подписки',
                        subscription_id=subscription.id,
                        error=e,
                    )
                    stats['errors'] += 1

            await db.commit()

            logger.info(
                '🎯 [multi-tariff] Синхронизация завершена',
                updated=stats['updated'],
                errors=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('❌ [multi-tariff] Критическая ошибка синхронизации', error=e)
            return {'created': 0, 'updated': 0, 'errors': 1, 'deleted': 0}

    async def _create_subscription_from_panel_data(self, db: AsyncSession, user, panel_user):
        try:
            from app.database.crud.subscription import create_subscription_no_commit
            from app.database.models import SubscriptionStatus

            expire_at_str = panel_user.get('expireAt', '')
            expire_at = self._parse_remnawave_date(expire_at_str)

            panel_status = panel_user.get('status', 'ACTIVE')
            current_time = self._now_utc()

            if panel_status == 'ACTIVE' and expire_at > current_time:
                status = SubscriptionStatus.ACTIVE
            elif expire_at <= current_time:
                status = SubscriptionStatus.EXPIRED
            else:
                status = SubscriptionStatus.DISABLED

            traffic_limit_bytes = panel_user.get('trafficLimitBytes', 0)
            traffic_limit_gb = traffic_limit_bytes // (1024**3) if traffic_limit_bytes > 0 else 0

            used_traffic_bytes = _get_user_traffic_bytes(panel_user)
            traffic_used_gb = used_traffic_bytes / (1024**3)

            active_squads = panel_user.get('activeInternalSquads', [])
            squad_uuids = []
            if isinstance(active_squads, list):
                for squad in active_squads:
                    if isinstance(squad, dict) and 'uuid' in squad:
                        squad_uuids.append(squad['uuid'])
                    elif isinstance(squad, str):
                        squad_uuids.append(squad)

            subscription_data = {
                'user_id': user.id,
                'status': status.value,
                'is_trial': False,
                'end_date': expire_at,
                'traffic_limit_gb': traffic_limit_gb,
                'traffic_used_gb': traffic_used_gb,
                'device_limit': panel_user.get('hwidDeviceLimit', 1) or 1,
                'connected_squads': squad_uuids,
                'remnawave_short_uuid': panel_user.get('shortUuid'),
                'subscription_url': panel_user.get('subscriptionUrl', ''),
                'subscription_crypto_link': (
                    panel_user.get('subscriptionCryptoLink') or (panel_user.get('happ') or {}).get('cryptoLink', '')
                ),
            }

            await create_subscription_no_commit(db, **subscription_data)
            logger.info(
                '✅ Подготовлена подписка для пользователя до', telegram_id=user.telegram_id, expire_at=expire_at
            )

        except Exception as e:
            logger.error('❌ Ошибка создания подписки для пользователя', telegram_id=user.telegram_id, error=e)
            try:
                from app.database.crud.subscription import create_subscription_no_commit
                from app.database.models import SubscriptionStatus

                await create_subscription_no_commit(
                    db=db,
                    user_id=user.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    end_date=self._now_utc() + timedelta(days=30),
                    traffic_limit_gb=0,
                    traffic_used_gb=0.0,
                    device_limit=1,
                    connected_squads=[],
                    remnawave_short_uuid=panel_user.get('shortUuid'),
                    subscription_url=panel_user.get('subscriptionUrl', ''),
                    subscription_crypto_link=(
                        panel_user.get('subscriptionCryptoLink') or (panel_user.get('happ') or {}).get('cryptoLink', '')
                    ),
                )
                logger.info('✅ Подготовлена базовая подписка для пользователя', telegram_id=user.telegram_id)
            except Exception as basic_error:
                logger.error('❌ Ошибка создания базовой подписки', basic_error=basic_error)

    async def _update_subscription_from_panel_data(self, db: AsyncSession, user, panel_user):
        try:
            from app.database.crud.subscription import get_subscription_by_user_id, is_recently_updated_by_webhook
            from app.database.models import SubscriptionStatus

            # Всегда используем async CRUD запрос для получения подписки
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id as _get_subs_upd

                _subs_upd = await _get_subs_upd(db, user.id)
                # Strict match by panel_user UUID — never fallback to another subscription
                subscription = next((s for s in _subs_upd if s.remnawave_uuid == panel_user.get('uuid')), None)
            else:
                subscription = await get_subscription_by_user_id(db, user.id)

            if not subscription:
                await self._create_subscription_from_panel_data(db, user, panel_user)
                return

            # Skip if recently updated by webhook (prevent stale data overwrite)
            if is_recently_updated_by_webhook(subscription):
                logger.debug(
                    'Пропуск синхронизации подписки : обновлена вебхуком недавно', subscription_id=subscription.id
                )
                return

            panel_status = panel_user.get('status', 'ACTIVE')
            expire_at_str = panel_user.get('expireAt', '')

            if expire_at_str:
                # expire_at приходит в UTC (naive) из _parse_remnawave_date
                expire_at = self._parse_remnawave_date(expire_at_str)

                # Обновляем end_date только если пользователь ACTIVE в панели.
                # Для EXPIRED/DISABLED панель может содержать искусственную дату
                # (установленную _safe_expire_at_for_panel при sync_users_to_panel),
                # которая не должна перезаписывать реальную дату окончания подписки.
                if panel_status == 'ACTIVE':
                    # Конвертируем локальную дату из БД в UTC для корректного сравнения
                    local_end_date_utc = self._local_to_utc(subscription.end_date)

                    # Панель авторитетна для ACTIVE подписок — обновляем end_date
                    # в обоих направлениях (как вперёд, так и назад)
                    time_diff = abs((local_end_date_utc - expire_at).total_seconds())
                    if time_diff > 60:
                        # Конвертируем UTC обратно в локальное время для сохранения в БД
                        new_end_date_local = expire_at.replace(tzinfo=self._utc_timezone).astimezone(
                            self._panel_timezone
                        )
                        direction = '→' if expire_at > local_end_date_utc else '←'
                        logger.info(
                            '✅ Sync: обновлена end_date для user -> (разница: с, направление: )',
                            value=getattr(user, 'telegram_id', '?'),
                            end_date=subscription.end_date,
                            new_end_date_local=new_end_date_local,
                            time_diff=round(time_diff, 0),
                            direction=direction,
                        )
                        subscription.end_date = new_end_date_local
                    else:
                        logger.debug(
                            '⏭️ Sync: пропускаем обновление end_date для user разница слишком мала (с < 60с)',
                            value=getattr(user, 'telegram_id', '?'),
                            time_diff=round(time_diff, 0),
                        )
                else:
                    logger.debug(
                        '⏭️ Sync: пропускаем обновление end_date для user панель не ACTIVE (статус: )',
                        value=getattr(user, 'telegram_id', '?'),
                        panel_status=panel_status,
                    )

            current_time = self._now_utc()
            # Конвертируем end_date в UTC для корректного сравнения с current_time
            end_date_utc = self._local_to_utc(subscription.end_date)

            if panel_status == 'ACTIVE' and end_date_utc > current_time:
                new_status = SubscriptionStatus.ACTIVE.value
            elif panel_status == 'LIMITED':
                new_status = SubscriptionStatus.LIMITED.value
            elif panel_status == 'DISABLED':
                new_status = SubscriptionStatus.DISABLED.value
            elif end_date_utc <= current_time:
                # КРИТИЧНО: НЕ деактивируем если текущий статус ACTIVE
                # Это защищает от race condition когда sync использует старую end_date из памяти,
                # а реальная end_date уже обновлена продлением
                if subscription.status == SubscriptionStatus.ACTIVE.value:
                    logger.warning(
                        '⚠️ Sync: пропускаем деактивацию подписки user статус ACTIVE, end_date ( UTC: ) <= now . Деактивация будет выполнена через middleware с буфером.',
                        value=getattr(user, 'telegram_id', '?'),
                        end_date=subscription.end_date,
                        end_date_utc=end_date_utc,
                        current_time=current_time,
                    )
                    new_status = subscription.status  # Сохраняем текущий статус
                else:
                    new_status = SubscriptionStatus.EXPIRED.value
            else:
                new_status = subscription.status

            if subscription.status != new_status:
                subscription.status = new_status
                logger.debug('Обновлен статус подписки', new_status=new_status)

            used_traffic_bytes = _get_user_traffic_bytes(panel_user)
            traffic_used_gb = used_traffic_bytes / (1024**3)

            if abs(subscription.traffic_used_gb - traffic_used_gb) > 0.01:
                subscription.traffic_used_gb = traffic_used_gb
                logger.debug('Обновлен использованный трафик', traffic_used_gb=traffic_used_gb)

            # traffic_limit_gb, device_limit: bot is source of truth, do not overwrite from panel

            new_short_uuid = panel_user.get('shortUuid')
            if new_short_uuid and subscription.remnawave_short_uuid != new_short_uuid:
                old_short_uuid = subscription.remnawave_short_uuid
                subscription.remnawave_short_uuid = new_short_uuid
                logger.debug(
                    'Обновлен short UUID подписки пользователя : →',
                    getattr=getattr(user, 'telegram_id', '?'),
                    old_short_uuid=old_short_uuid,
                    new_short_uuid=new_short_uuid,
                )

            panel_url = panel_user.get('subscriptionUrl', '')
            if panel_url and subscription.subscription_url != panel_url:
                subscription.subscription_url = panel_url

            panel_crypto_link = panel_user.get('subscriptionCryptoLink') or (panel_user.get('happ') or {}).get(
                'cryptoLink', ''
            )
            if panel_crypto_link and subscription.subscription_crypto_link != panel_crypto_link:
                subscription.subscription_crypto_link = panel_crypto_link

            # connected_squads: bot is source of truth (tariff.allowed_squads), do not overwrite from panel

            # Коммитим изменения позже, в основном цикле, чтобы уменьшить количество транзакций
            logger.debug('✅ Обновлена подписка для пользователя', telegram_id=user.telegram_id)

        except Exception as e:
            logger.error('❌ Ошибка обновления подписки для пользователя', telegram_id=user.telegram_id, error=e)
            # Не делаем rollback, так как это может повлиять на другие операции
            # Ошибку прокидываем выше для корректной обработки в основном цикле
            raise

    async def sync_users_to_panel(self, db: AsyncSession) -> dict[str, int]:
        from app.database.crud.subscription import get_subscriptions_batch

        try:
            stats = {'created': 0, 'updated': 0, 'errors': 0}

            batch_size = 500
            offset = 0
            concurrent_limit = 5

            async with self.get_api_client() as api:
                semaphore = asyncio.Semaphore(concurrent_limit)

                while True:
                    # Получаем подписки напрямую (не через users)
                    subscriptions = await get_subscriptions_batch(db, offset=offset, limit=batch_size)

                    if not subscriptions:
                        break

                    # Фильтруем подписки у которых есть пользователь
                    valid_subscriptions = [s for s in subscriptions if s.user]

                    if not valid_subscriptions:
                        if len(subscriptions) < batch_size:
                            break
                        offset += batch_size
                        continue

                    # Подготавливаем задачи для параллельного выполнения
                    async def process_subscription(sub):
                        async with semaphore:
                            try:
                                user = sub.user
                                hwid_limit = resolve_hwid_device_limit_for_payload(sub)
                                expire_at = self._safe_expire_at_for_panel(sub.end_date)

                                # Определяем статус для панели
                                is_subscription_active = sub.status in (
                                    SubscriptionStatus.ACTIVE.value,
                                    SubscriptionStatus.TRIAL.value,
                                ) and sub.end_date > datetime.now(UTC)
                                status = UserStatus.ACTIVE if is_subscription_active else UserStatus.DISABLED

                                username = settings.format_remnawave_username(
                                    full_name=user.full_name,
                                    username=user.username,
                                    telegram_id=user.telegram_id,
                                    email=user.email,
                                    user_id=user.id,
                                )
                                # Append permanent short_id suffix in multi-tariff mode
                                if settings.is_multi_tariff_enabled() and sub.remnawave_short_id:
                                    username = f'{username}_{sub.remnawave_short_id}'

                                create_kwargs = dict(
                                    username=username,
                                    expire_at=expire_at,
                                    status=status,
                                    traffic_limit_bytes=sub.traffic_limit_gb * (1024**3)
                                    if sub.traffic_limit_gb > 0
                                    else 0,
                                    traffic_limit_strategy=get_traffic_reset_strategy(sub.tariff),
                                    telegram_id=user.telegram_id,
                                    email=user.email,
                                    description=settings.format_remnawave_user_description(
                                        full_name=user.full_name,
                                        username=user.username,
                                        telegram_id=user.telegram_id,
                                        email=user.email,
                                    ),
                                    active_internal_squads=sub.connected_squads,
                                )

                                if hwid_limit is not None:
                                    create_kwargs['hwid_device_limit'] = hwid_limit

                                # Внешний сквад: синхронизируем из тарифа (если задан)
                                # Не отправляем null — RemnaWave API не принимает null для externalSquadUuid (A039)
                                if sub.tariff and sub.tariff.external_squad_uuid:
                                    create_kwargs['external_squad_uuid'] = sub.tariff.external_squad_uuid

                                # Определяем UUID для обновления
                                panel_uuid = (
                                    sub.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
                                )

                                # Если нет UUID в базе, ищем пользователя по telegram_id в панели
                                if not panel_uuid and user.telegram_id:
                                    existing_users = await api.get_user_by_telegram_id(user.telegram_id)
                                    if existing_users:
                                        if settings.is_multi_tariff_enabled():
                                            if sub.remnawave_short_id:
                                                _suffix = f'_{sub.remnawave_short_id}'
                                                _matched = next(
                                                    (
                                                        eu
                                                        for eu in existing_users
                                                        if eu.username and eu.username.endswith(_suffix)
                                                    ),
                                                    None,
                                                )
                                                if _matched:
                                                    panel_uuid = _matched.uuid
                                            # else: no short_id — can't match safely, skip
                                        else:
                                            panel_uuid = existing_users[0].uuid
                                        if panel_uuid:
                                            logger.debug(
                                                'Найден пользователь в панели',
                                                telegram_id=user.telegram_id,
                                                panel_uuid=panel_uuid,
                                            )

                                # Fallback: поиск по email (для OAuth юзеров без telegram_id)
                                if not panel_uuid and user.email:
                                    existing_users = await api.get_user_by_email(user.email)
                                    if existing_users:
                                        if settings.is_multi_tariff_enabled():
                                            if sub.remnawave_short_id:
                                                _suffix = f'_{sub.remnawave_short_id}'
                                                _matched = next(
                                                    (
                                                        eu
                                                        for eu in existing_users
                                                        if eu.username and eu.username.endswith(_suffix)
                                                    ),
                                                    None,
                                                )
                                                if _matched:
                                                    panel_uuid = _matched.uuid
                                            # else: no short_id — can't match safely, skip
                                        else:
                                            panel_uuid = existing_users[0].uuid
                                        if panel_uuid:
                                            logger.debug(
                                                'Найден пользователь в панели по email',
                                                email=user.email,
                                                panel_uuid=panel_uuid,
                                            )

                                if panel_uuid:
                                    update_kwargs = dict(
                                        uuid=panel_uuid,
                                        status=status,
                                        expire_at=expire_at,
                                        traffic_limit_bytes=create_kwargs['traffic_limit_bytes'],
                                        traffic_limit_strategy=get_traffic_reset_strategy(sub.tariff),
                                        email=user.email,
                                        description=create_kwargs['description'],
                                        active_internal_squads=sub.connected_squads,
                                    )

                                    if hwid_limit is not None:
                                        update_kwargs['hwid_device_limit'] = hwid_limit

                                    # Внешний сквад: синхронизируем из тарифа (если задан)
                                    # Не отправляем null — RemnaWave API не принимает null для externalSquadUuid (A039)
                                    if sub.tariff and sub.tariff.external_squad_uuid:
                                        update_kwargs['external_squad_uuid'] = sub.tariff.external_squad_uuid

                                    try:
                                        await api.update_user(**update_kwargs)
                                        # Сохраняем UUID если его не было
                                        if settings.is_multi_tariff_enabled():
                                            if not sub.remnawave_uuid:
                                                sub.remnawave_uuid = panel_uuid
                                        elif not user.remnawave_uuid:
                                            user.remnawave_uuid = panel_uuid
                                        return ('updated', sub, None)
                                    except RemnaWaveAPIError as api_error:
                                        if api_error.status_code == 404:
                                            new_user = await api.create_user(**create_kwargs)
                                            return ('created', sub, new_user)
                                        raise
                                else:
                                    new_user = await api.create_user(**create_kwargs)
                                    return ('created', sub, new_user)

                            except Exception as e:
                                logger.error(
                                    'Ошибка синхронизации пользователя в панель',
                                    telegram_id=sub.user.telegram_id if sub.user else 'N/A',
                                    error=e,
                                )
                                return ('error', sub, None)

                    # Выполняем параллельно
                    tasks = [process_subscription(s) for s in valid_subscriptions]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Обрабатываем результаты
                    for result in results:
                        if isinstance(result, Exception):
                            stats['errors'] += 1
                            continue

                        action, sub, new_user = result
                        if action == 'created':
                            if new_user and sub.user:
                                if settings.is_multi_tariff_enabled():
                                    sub.remnawave_uuid = new_user.uuid
                                else:
                                    sub.user.remnawave_uuid = new_user.uuid
                                sub.remnawave_short_uuid = new_user.short_uuid
                            stats['created'] += 1
                        elif action == 'updated':
                            stats['updated'] += 1
                        else:
                            stats['errors'] += 1

                    try:
                        await db.commit()
                    except Exception as commit_error:
                        logger.error('Ошибка фиксации транзакции при синхронизации в панель', commit_error=commit_error)
                        await db.rollback()
                        stats['errors'] += len(valid_subscriptions)

                    logger.info(
                        '📦 Обработано подписок: создано обновлено ошибок',
                        offset=offset + len(subscriptions),
                        stats=stats['created'],
                        stats_2=stats['updated'],
                        stats_3=stats['errors'],
                    )

                    if len(subscriptions) < batch_size:
                        break

                    offset += batch_size

            logger.info(
                '✅ Синхронизация в панель завершена: создано обновлено ошибок',
                stats=stats['created'],
                stats_2=stats['updated'],
                stats_3=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('Ошибка синхронизации пользователей в панель', error=e)
            return {'created': 0, 'updated': 0, 'errors': 1}

    async def get_user_traffic_stats(self, telegram_id: int) -> dict[str, Any] | None:
        try:
            async with self.get_api_client() as api:
                users = await api.get_user_by_telegram_id(telegram_id)

                if not users:
                    return None

                user = users[0]

                return {
                    'used_traffic_bytes': user.used_traffic_bytes,
                    'used_traffic_gb': user.used_traffic_bytes / (1024**3),
                    'lifetime_used_traffic_bytes': user.lifetime_used_traffic_bytes,
                    'lifetime_used_traffic_gb': user.lifetime_used_traffic_bytes / (1024**3),
                    'traffic_limit_bytes': user.traffic_limit_bytes,
                    'traffic_limit_gb': user.traffic_limit_bytes / (1024**3) if user.traffic_limit_bytes > 0 else 0,
                    'subscription_url': user.subscription_url,
                }

        except Exception as e:
            logger.error('Ошибка получения статистики трафика для пользователя', telegram_id=telegram_id, error=e)
            return None

    async def get_user_traffic_stats_by_uuid(self, remnawave_uuid: str) -> dict[str, Any] | None:
        """
        Получить статистику трафика по RemnaWave UUID.

        Используется для email-пользователей у которых нет telegram_id.
        """
        try:
            async with self.get_api_client() as api:
                user = await api.get_user_by_uuid(remnawave_uuid)

                if not user:
                    return None

                return {
                    'used_traffic_bytes': user.used_traffic_bytes,
                    'used_traffic_gb': user.used_traffic_bytes / (1024**3),
                    'lifetime_used_traffic_bytes': user.lifetime_used_traffic_bytes,
                    'lifetime_used_traffic_gb': user.lifetime_used_traffic_bytes / (1024**3),
                    'traffic_limit_bytes': user.traffic_limit_bytes,
                    'traffic_limit_gb': user.traffic_limit_bytes / (1024**3) if user.traffic_limit_bytes > 0 else 0,
                    'subscription_url': user.subscription_url,
                }

        except Exception as e:
            logger.error('Ошибка получения статистики трафика по UUID', remnawave_uuid=remnawave_uuid, error=e)
            return None

    async def get_telegram_id_by_email(self, user_identifier: str) -> int | None:
        """
        Получить telegram_id пользователя по email или username из панели RemnaWave.

        Args:
            user_identifier: Email или username пользователя

        Returns:
            telegram_id если найден, иначе None
        """
        if not self.is_configured:
            logger.warning('RemnaWave API не настроен для поиска пользователя')
            return None

        try:
            async with self.get_api_client() as api:
                # Сначала пробуем найти по username (часто username == email)
                try:
                    user = await api.get_user_by_username(user_identifier)
                    if user and user.telegram_id:
                        logger.info(
                            'Найден пользователь по username telegram_id',
                            user_identifier=user_identifier,
                            telegram_id=user.telegram_id,
                        )
                        return user.telegram_id
                except Exception as e:
                    logger.debug('Пользователь не найден по username', user_identifier=user_identifier, error=e)

                # Если не нашли по username, ищем по email среди всех пользователей (с пагинацией)
                try:
                    page_size = 500
                    start = 0
                    while True:
                        page_response = await api.get_all_users(start=start, size=page_size)
                        users_list = page_response.get('users', [])
                        total = page_response.get('total', 0)

                        for panel_user in users_list:
                            panel_email = panel_user.email if hasattr(panel_user, 'email') else None
                            if panel_email and panel_email.lower() == user_identifier.lower():
                                panel_telegram_id = (
                                    panel_user.telegram_id if hasattr(panel_user, 'telegram_id') else None
                                )
                                if panel_telegram_id:
                                    logger.info(
                                        'Найден пользователь по email telegram_id',
                                        user_identifier=user_identifier,
                                        panel_telegram_id=panel_telegram_id,
                                    )
                                    return panel_telegram_id

                        start += len(users_list)
                        if start >= total or not users_list:
                            break
                except Exception as e:
                    logger.warning('Ошибка поиска пользователя по email', user_identifier=user_identifier, error=e)

                logger.warning('Пользователь с идентификатором не найден в панели', user_identifier=user_identifier)
                return None

        except Exception as e:
            logger.error('Ошибка получения telegram_id для', user_identifier=user_identifier, error=e)
            return None

    async def test_api_connection(self) -> dict[str, Any]:
        if not self.is_configured:
            return {
                'status': 'not_configured',
                'message': self.configuration_error or 'RemnaWave API не настроен',
                'api_url': settings.REMNAWAVE_API_URL,
            }
        try:
            async with self.get_api_client() as api:
                system_stats = await api.get_system_stats()

                return {
                    'status': 'connected',
                    'message': 'Подключение успешно',
                    'api_url': settings.REMNAWAVE_API_URL,
                    'system_info': system_stats,
                }

        except RemnaWaveAPIError as e:
            return {
                'status': 'error',
                'message': f'Ошибка API: {e.message}',
                'status_code': e.status_code,
                'api_url': settings.REMNAWAVE_API_URL,
            }
        except RemnaWaveConfigurationError as e:
            return {
                'status': 'not_configured',
                'message': str(e),
                'api_url': settings.REMNAWAVE_API_URL,
            }
        except Exception as e:
            return {'status': 'error', 'message': f'Ошибка подключения: {e!s}', 'api_url': settings.REMNAWAVE_API_URL}

    async def get_nodes_realtime_usage(self) -> list[dict[str, Any]]:
        try:
            async with self.get_api_client() as api:
                usage_data = await api.get_nodes_realtime_usage()
                return usage_data

        except Exception as e:
            logger.error('Ошибка получения актуального использования нод', error=e)
            return []

    async def get_squad_details(self, squad_uuid: str) -> dict | None:
        try:
            async with self.get_api_client() as api:
                squad = await api.get_internal_squad_by_uuid(squad_uuid)
                if squad:
                    inbounds = [
                        asdict(inbound) if is_dataclass(inbound) else inbound for inbound in squad.inbounds or []
                    ]
                    return {
                        'uuid': squad.uuid,
                        'name': squad.name,
                        'members_count': squad.members_count,
                        'inbounds_count': squad.inbounds_count,
                        'inbounds': inbounds,
                    }
                return None
        except Exception as e:
            logger.error('Error getting squad details', error=e)
            return None

    async def add_all_users_to_squad(self, squad_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                response = await api._make_request('POST', f'/api/internal-squads/{squad_uuid}/bulk-actions/add-users')
                return response.get('response', {}).get('eventSent', False)
        except Exception as e:
            logger.error('Error adding users to squad', error=e)
            return False

    async def remove_all_users_from_squad(self, squad_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                response = await api._make_request(
                    'DELETE', f'/api/internal-squads/{squad_uuid}/bulk-actions/remove-users'
                )
                return response.get('response', {}).get('eventSent', False)
        except Exception as e:
            logger.error('Error removing users from squad', error=e)
            return False

    async def get_all_inbounds(self) -> list[dict]:
        try:
            async with self.get_api_client() as api:
                response = await api._make_request('GET', '/api/config-profiles/inbounds')
                inbounds_data = response.get('response', {}).get('inbounds', [])

                return [
                    {
                        'uuid': inbound['uuid'],
                        'tag': inbound['tag'],
                        'type': inbound['type'],
                        'network': inbound.get('network'),
                        'security': inbound.get('security'),
                        'port': inbound.get('port'),
                    }
                    for inbound in inbounds_data
                ]
        except Exception as e:
            logger.error('Error getting all inbounds', error=e)
            return []

    async def rename_squad(self, squad_uuid: str, new_name: str) -> bool:
        try:
            async with self.get_api_client() as api:
                data = {'uuid': squad_uuid, 'name': new_name}
                await api._make_request('PATCH', '/api/internal-squads', data)
                return True
        except Exception as e:
            logger.error('Error renaming squad', error=e)
            return False

    async def get_node_user_usage_by_range(self, node_uuid: str, start_date, end_date) -> list[dict[str, Any]]:
        try:
            async with self.get_api_client() as api:
                start_str = start_date.isoformat().replace('+00:00', 'Z')
                end_str = end_date.isoformat().replace('+00:00', 'Z')

                params = {'start': start_str, 'end': end_str}

                usage_data = await api._make_request(
                    'GET', f'/api/bandwidth-stats/nodes/{node_uuid}/users/legacy', params=params
                )

                return usage_data.get('response', [])

        except Exception as e:
            logger.error('Ошибка получения статистики использования ноды', node_uuid=node_uuid, error=e)
            return []

    async def get_node_statistics(self, node_uuid: str) -> dict[str, Any] | None:
        try:
            node = await self.get_node_details(node_uuid)
            if not node:
                return None

            realtime_stats = await self.get_nodes_realtime_usage()

            node_realtime = None
            for stats in realtime_stats:
                if stats.get('nodeUuid') == node_uuid:
                    node_realtime = stats
                    break

            end_date = datetime.now(UTC)
            start_date = end_date - timedelta(days=7)

            usage_history = await self.get_node_user_usage_by_range(node_uuid, start_date, end_date)

            return {
                'node': node,
                'realtime': node_realtime,
                'usage_history': usage_history,
                'last_updated': datetime.now(UTC).isoformat(),
            }

        except Exception as e:
            logger.error('Ошибка получения статистики ноды', node_uuid=node_uuid, error=e)

    async def validate_user_data_before_sync(self, panel_user) -> bool:
        try:
            if not panel_user.telegram_id:
                logger.debug('Нет telegram_id для пользователя', uuid=panel_user.uuid)
                return False

            if not panel_user.uuid:
                logger.debug('Нет UUID для пользователя', telegram_id=panel_user.telegram_id)
                return False

            if panel_user.telegram_id <= 0:
                logger.debug('Некорректный telegram_id', telegram_id=panel_user.telegram_id)
                return False

            return True

        except Exception as e:
            logger.error('Ошибка валидации данных пользователя', error=e)
            return False

    async def force_cleanup_user_data(self, db: AsyncSession, user: User) -> bool:
        """
        ОПАСНАЯ ФУНКЦИЯ: Полностью сбрасывает данные подписки пользователя.
        Баланс и has_had_paid_subscription СОХРАНЯЮТСЯ (оплаченные средства).
        Используйте только для полной очистки пользователя.
        """
        try:
            # Предупреждение для платных пользователей
            user_subscriptions = getattr(user, 'subscriptions', None) or []
            was_paid = (
                user.has_had_paid_subscription
                or any(not sub.is_trial for sub in user_subscriptions)
                or user.balance_kopeks > 0
            )
            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            if was_paid:
                logger.warning(
                    '⚠️ ВНИМАНИЕ: force_cleanup_user_data вызвана для ПЛАТНОГО пользователя ! has_had_paid_subscription=, balance=, is_trial',
                    user_id_display=user_id_display,
                    has_had_paid_subscription=user.has_had_paid_subscription,
                    balance_kopeks=user.balance_kopeks,
                    is_trial=[sub.is_trial for sub in user_subscriptions] if user_subscriptions else 'N/A',
                )

            logger.info('🗑️ ПРИНУДИТЕЛЬНАЯ полная очистка данных пользователя', user_id_display=user_id_display)

            # Reset devices for all subscription UUIDs in multi-tariff, or user UUID in single-tariff
            _uuids_to_reset = set()
            if settings.is_multi_tariff_enabled():
                user_subs = getattr(user, 'subscriptions', []) or []
                for sub in user_subs:
                    _sub_uuid = getattr(sub, 'remnawave_uuid', None)
                    if _sub_uuid:
                        _uuids_to_reset.add(_sub_uuid)
            if not _uuids_to_reset and user.remnawave_uuid:
                _uuids_to_reset.add(user.remnawave_uuid)

            for _uuid in _uuids_to_reset:
                try:
                    async with self.get_api_client() as api:
                        await api.reset_user_devices(_uuid)
                except Exception as e:
                    logger.warning('Failed to reset devices for UUID', uuid=_uuid, error=e)

            try:
                from sqlalchemy import delete

                from app.database.models import (
                    ReferralEarning,
                    SubscriptionServer,
                    SubscriptionStatus,
                    Transaction,
                )

                for sub in user_subscriptions:
                    await decrement_subscription_server_counts(db, sub)

                    await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id))
                if user_subscriptions:
                    logger.info('🗑️ Удалены серверы подписок для', user_id_display=user_id_display)

                await db.execute(delete(Transaction).where(Transaction.user_id == user.id))
                logger.info('🗑️ Удалены транзакции для', user_id_display=user_id_display)

                await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user.id))
                await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user.id))
                logger.info('🗑️ Удалены реферальные доходы для', user_id_display=user_id_display)

                # PromoCodeUse НЕ удаляем — история промокодов постоянна,
                # иначе пользователь может повторно активировать промокоды

            except Exception as records_error:
                logger.error('❌ Ошибка удаления связанных записей', records_error=records_error)

            try:
                if user.balance_kopeks > 0:
                    logger.warning(
                        '⚠️ force_cleanup: СОХРАНЯЕМ баланс пользователя (оплаченные средства)',
                        user_id_display=user_id_display,
                        balance_kopeks=user.balance_kopeks,
                    )
                user.remnawave_uuid = None
                user.updated_at = self._now_utc()

                for sub in user_subscriptions:
                    sub.status = SubscriptionStatus.DISABLED.value
                    sub.is_trial = True
                    sub.end_date = self._now_utc()
                    sub.traffic_limit_gb = 0
                    sub.traffic_used_gb = 0.0
                    sub.device_limit = 1
                    sub.connected_squads = []
                    sub.autopay_enabled = False
                    sub.autopay_days_before = settings.DEFAULT_AUTOPAY_DAYS_BEFORE
                    sub.remnawave_short_uuid = None
                    sub.subscription_url = ''
                    sub.subscription_crypto_link = ''
                    sub.updated_at = self._now_utc()

                await db.commit()

                logger.info('✅ ПРИНУДИТЕЛЬНО очищены ВСЕ данные пользователя', user_id_display=user_id_display)
                return True

            except Exception as cleanup_error:
                logger.error('❌ Ошибка финальной очистки пользователя', cleanup_error=cleanup_error)
                await db.rollback()
                return False

        except Exception as e:
            logger.error(
                '❌ Критическая ошибка принудительной очистки пользователя', telegram_id=user.telegram_id, error=e
            )
            await db.rollback()
            return False

    async def cleanup_orphaned_subscriptions(self, db: AsyncSession) -> dict[str, int]:
        try:
            stats = {'deactivated': 0, 'errors': 0, 'checked': 0}

            logger.info('🧹 Начинаем усиленную очистку неактуальных подписок...')

            async with self.get_api_client() as api:
                panel_users_data = await api._make_request('GET', '/api/users')
                panel_users = panel_users_data['response']['users']

            panel_telegram_ids = set()
            for panel_user in panel_users:
                telegram_id = panel_user.get('telegramId')
                if telegram_id:
                    panel_telegram_ids.add(telegram_id)

            logger.info('📊 Найдено пользователей в панели', panel_telegram_ids_count=len(panel_telegram_ids))

            from app.database.crud.subscription import get_all_subscriptions
            from app.database.models import SubscriptionStatus

            page = 1
            limit = 100

            while True:
                subscriptions, total_count = await get_all_subscriptions(db, page, limit)

                if not subscriptions:
                    break

                for subscription in subscriptions:
                    try:
                        stats['checked'] += 1
                        user = subscription.user

                        if subscription.status in (
                            SubscriptionStatus.DISABLED.value,
                            SubscriptionStatus.LIMITED.value,
                        ):
                            continue

                        # Email-only users have no telegram_id — cannot be matched by panel_telegram_ids
                        if not user.telegram_id:
                            continue

                        if user.telegram_id not in panel_telegram_ids:
                            logger.info(
                                '🗑️ ПОЛНАЯ деактивация подписки пользователя (отсутствует в панели)',
                                telegram_id=user.telegram_id,
                            )

                            cleanup_success = await self.force_cleanup_user_data(db, user)

                            if cleanup_success:
                                stats['deactivated'] += 1
                            else:
                                stats['errors'] += 1

                    except Exception as sub_error:
                        logger.error(
                            '❌ Ошибка обработки подписки', subscription_id=subscription.id, sub_error=sub_error
                        )
                        stats['errors'] += 1

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                '🧹 Усиленная очистка завершена: проверено деактивировано ошибок',
                stats=stats['checked'],
                stats_2=stats['deactivated'],
                stats_3=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('❌ Критическая ошибка усиленной очистки подписок', error=e)
            return {'deactivated': 0, 'errors': 1, 'checked': 0}

    async def sync_subscription_statuses(self, db: AsyncSession) -> dict[str, int]:
        try:
            stats = {'updated': 0, 'errors': 0, 'checked': 0}

            logger.info('🔄 Начинаем синхронизацию статусов подписок...')

            async with self.get_api_client() as api:
                panel_users_data = await api._make_request('GET', '/api/users')
                panel_users = panel_users_data['response']['users']

            panel_users_dict = {}
            for panel_user in panel_users:
                telegram_id = panel_user.get('telegramId')
                if telegram_id:
                    panel_users_dict[telegram_id] = panel_user

            logger.info(
                '📊 Найдено пользователей в панели для синхронизации', panel_users_dict_count=len(panel_users_dict)
            )

            from app.database.crud.subscription import get_all_subscriptions
            from app.database.models import SubscriptionStatus

            page = 1
            limit = 100

            while True:
                subscriptions, total_count = await get_all_subscriptions(db, page, limit)

                if not subscriptions:
                    break

                for subscription in subscriptions:
                    try:
                        stats['checked'] += 1
                        user = subscription.user

                        # Skip email-only users (no telegram_id for panel lookup)
                        if not user.telegram_id:
                            logger.debug('Пропускаем email-пользователя при синхронизации с панелью', user_id=user.id)
                            continue

                        panel_user = panel_users_dict.get(user.telegram_id)

                        if panel_user:
                            await self._update_subscription_from_panel_data(db, user, panel_user)
                            stats['updated'] += 1
                        elif subscription.status != SubscriptionStatus.DISABLED.value:
                            from app.database.crud.subscription import (
                                deactivate_subscription,
                                is_recently_updated_by_webhook,
                            )

                            if is_recently_updated_by_webhook(subscription):
                                logger.debug(
                                    'Пропуск деактивации подписки : обновлена вебхуком недавно',
                                    subscription_id=subscription.id,
                                )
                            else:
                                logger.info(
                                    '🗑️ Деактивируем подписку пользователя (нет в панели)', telegram_id=user.telegram_id
                                )
                                await deactivate_subscription(db, subscription)
                                stats['updated'] += 1

                    except Exception as sub_error:
                        logger.error(
                            '❌ Ошибка синхронизации подписки', subscription_id=subscription.id, sub_error=sub_error
                        )
                        stats['errors'] += 1

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                '🔄 Синхронизация статусов завершена: проверено обновлено ошибок',
                stats=stats['checked'],
                stats_2=stats['updated'],
                stats_3=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('❌ Критическая ошибка синхронизации статусов', error=e)
            return {'updated': 0, 'errors': 1, 'checked': 0}

    async def validate_and_fix_subscriptions(self, db: AsyncSession) -> dict[str, int]:
        try:
            stats = {'fixed': 0, 'errors': 0, 'checked': 0, 'issues_found': 0}

            logger.info('🔍 Начинаем валидацию подписок...')

            from app.database.crud.subscription import get_all_subscriptions
            from app.database.models import SubscriptionStatus

            page = 1
            limit = 100

            while True:
                subscriptions, total_count = await get_all_subscriptions(db, page, limit)

                if not subscriptions:
                    break

                for subscription in subscriptions:
                    try:
                        stats['checked'] += 1
                        user = subscription.user
                        issues_fixed = 0

                        from app.database.crud.subscription import is_recently_updated_by_webhook

                        current_time = self._now_utc()
                        # Конвертируем end_date в UTC для корректного сравнения
                        end_date_utc = self._local_to_utc(subscription.end_date)
                        # Добавляем буфер 5 минут для защиты от race condition при продлении
                        expiry_buffer = timedelta(minutes=5)

                        # Суточные подписки управляются DailySubscriptionService — не экспайрим
                        tariff = getattr(subscription, 'tariff', None)
                        is_active_daily = (
                            tariff is not None
                            and getattr(tariff, 'is_daily', False)
                            and not getattr(subscription, 'is_daily_paused', False)
                        )

                        if (
                            end_date_utc + expiry_buffer <= current_time
                            and subscription.status == SubscriptionStatus.ACTIVE.value
                            and not is_recently_updated_by_webhook(subscription)
                            and not is_active_daily
                        ):
                            time_since_expiry = current_time - end_date_utc
                            logger.warning(
                                '🔧 fix_data_issues: деактивируем подписку (user=), просрочена на',
                                subscription_id=subscription.id,
                                telegram_id=user.telegram_id,
                                time_since_expiry=time_since_expiry,
                            )
                            subscription.status = SubscriptionStatus.EXPIRED.value
                            issues_fixed += 1

                        _lookup_uuid = (
                            getattr(subscription, 'remnawave_uuid', None)
                            if settings.is_multi_tariff_enabled()
                            else None
                        ) or getattr(user, 'remnawave_uuid', None)
                        if not subscription.remnawave_short_uuid and _lookup_uuid:
                            try:
                                async with self.get_api_client() as api:
                                    rw_user = await api.get_user_by_uuid(_lookup_uuid)
                                    if rw_user:
                                        subscription.remnawave_short_uuid = rw_user.short_uuid
                                        subscription.subscription_url = rw_user.subscription_url
                                        subscription.subscription_crypto_link = rw_user.happ_crypto_link
                                        logger.info(
                                            '🔧 Восстановлены данные Remnawave для', telegram_id=user.telegram_id
                                        )
                                        issues_fixed += 1
                            except Exception as rw_error:
                                logger.warning(
                                    '⚠️ Не удалось получить данные Remnawave для',
                                    telegram_id=user.telegram_id,
                                    rw_error=rw_error,
                                )

                        if subscription.traffic_limit_gb < 0:
                            subscription.traffic_limit_gb = 0
                            logger.info('🔧 Исправлен некорректный лимит трафика для', telegram_id=user.telegram_id)
                            issues_fixed += 1

                        if subscription.traffic_used_gb < 0:
                            subscription.traffic_used_gb = 0.0
                            logger.info(
                                '🔧 Исправлено некорректное использование трафика для', telegram_id=user.telegram_id
                            )
                            issues_fixed += 1

                        if subscription.device_limit <= 0:
                            subscription.device_limit = 1
                            logger.info('🔧 Исправлен лимит устройств для', telegram_id=user.telegram_id)
                            issues_fixed += 1

                        if subscription.connected_squads is None:
                            subscription.connected_squads = []
                            logger.info('🔧 Инициализирован список сквадов для', telegram_id=user.telegram_id)
                            issues_fixed += 1

                        if issues_fixed > 0:
                            stats['issues_found'] += issues_fixed
                            stats['fixed'] += 1
                            await db.commit()

                    except Exception as sub_error:
                        logger.error(
                            '❌ Ошибка валидации подписки', subscription_id=subscription.id, sub_error=sub_error
                        )
                        stats['errors'] += 1
                        await db.rollback()

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                '🔍 Валидация завершена: проверено исправлено подписок найдено проблем ошибок',
                stats=stats['checked'],
                stats_2=stats['fixed'],
                stats_3=stats['issues_found'],
                stats_4=stats['errors'],
            )
            return stats

        except Exception as e:
            logger.error('❌ Критическая ошибка валидации', error=e)
            return {'fixed': 0, 'errors': 1, 'checked': 0, 'issues_found': 0}

    async def get_sync_recommendations(self, db: AsyncSession) -> dict[str, Any]:
        try:
            recommendations = {
                'should_sync': False,
                'sync_type': 'none',
                'reasons': [],
                'priority': 'low',
                'estimated_time': '1-2 минуты',
            }

            from app.database.crud.user import get_users_list

            bot_users = await get_users_list(db, offset=0, limit=10000)

            users_without_uuid = sum(
                1
                for user in bot_users
                if not user.remnawave_uuid and any(True for _ in (getattr(user, 'subscriptions', None) or []))
            )

            from app.database.crud.subscription import get_expired_subscriptions

            expired_subscriptions = await get_expired_subscriptions(db)
            active_expired = sum(1 for sub in expired_subscriptions if sub.status == 'active')

            if users_without_uuid > 10:
                recommendations['should_sync'] = True
                recommendations['sync_type'] = 'all'
                recommendations['priority'] = 'high'
                recommendations['reasons'].append(f'Найдено {users_without_uuid} пользователей без связи с Remnawave')
                recommendations['estimated_time'] = '3-5 минут'

            if active_expired > 5:
                recommendations['should_sync'] = True
                if recommendations['sync_type'] == 'none':
                    recommendations['sync_type'] = 'update_only'
                recommendations['priority'] = (
                    'medium' if recommendations['priority'] == 'low' else recommendations['priority']
                )
                recommendations['reasons'].append(f'Найдено {active_expired} активных подписок с истекшим сроком')

            if not recommendations['should_sync']:
                recommendations['sync_type'] = 'update_only'
                recommendations['reasons'].append('Рекомендуется регулярная синхронизация данных')
                recommendations['estimated_time'] = '1-2 минуты'

            return recommendations

        except Exception as e:
            logger.error('❌ Ошибка получения рекомендаций', error=e)
            return {
                'should_sync': True,
                'sync_type': 'all',
                'reasons': ['Ошибка анализа - рекомендуется полная синхронизация'],
                'priority': 'medium',
                'estimated_time': '3-5 минут',
            }

    async def monitor_panel_status(self, bot) -> dict[str, Any]:
        try:
            from app.utils.cache import cache

            previous_status = await cache.get('remnawave_panel_status') or 'unknown'

            status_result = await self.check_panel_health()
            current_status = status_result.get('status', 'offline')

            if current_status != previous_status and previous_status != 'unknown':
                await self._send_status_change_notification(bot, previous_status, current_status, status_result)

            await cache.set('remnawave_panel_status', current_status, expire=300)

            return status_result

        except Exception as e:
            logger.error('Ошибка мониторинга статуса панели Remnawave', error=e)
            return {'status': 'error', 'error': str(e)}

    async def _send_status_change_notification(
        self, bot, old_status: str, new_status: str, status_data: dict[str, Any]
    ):
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(bot)

            details = {
                'api_url': status_data.get('api_url'),
                'response_time': status_data.get('response_time'),
                'last_check': status_data.get('last_check'),
                'users_online': status_data.get('users_online'),
                'nodes_online': status_data.get('nodes_online'),
                'total_nodes': status_data.get('total_nodes'),
                'old_status': old_status,
            }

            if new_status == 'offline':
                details['error'] = status_data.get('api_error')
            elif new_status == 'degraded':
                issues = []
                if status_data.get('response_time', 0) > 10:
                    issues.append(f'Медленный отклик API ({status_data.get("response_time")}с)')
                if status_data.get('nodes_health') == 'unhealthy':
                    issues.append(
                        f'Проблемы с нодами ({status_data.get("nodes_online")}/{status_data.get("total_nodes")} онлайн)'
                    )
                details['issues'] = issues

            await notification_service.send_remnawave_panel_status_notification(new_status, details)

            logger.info(
                'Отправлено уведомление об изменении статуса панели', old_status=old_status, new_status=new_status
            )

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении статуса', error=e)

    async def send_manual_status_notification(self, bot, status: str, message: str = ''):
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(bot)

            details = {
                'api_url': settings.REMNAWAVE_API_URL,
                'last_check': datetime.now(UTC),
                'manual_message': message,
            }

            if status == 'maintenance':
                details['maintenance_reason'] = message or 'Плановое обслуживание'

            await notification_service.send_remnawave_panel_status_notification(status, details)

            logger.info('Отправлено ручное уведомление о статусе панели', status=status)
            return True

        except Exception as e:
            logger.error('Ошибка отправки ручного уведомления', error=e)
            return False

    async def get_panel_status_summary(self) -> dict[str, Any]:
        try:
            status_data = await self.check_panel_health()

            status_descriptions = {
                'online': '🟢 Панель работает нормально',
                'offline': '🔴 Панель недоступна',
                'degraded': '🟡 Панель работает со сбоями',
                'maintenance': '🔧 Панель на обслуживании',
            }

            status = status_data.get('status', 'offline')

            summary = {
                'status': status,
                'description': status_descriptions.get(status, '❓ Статус неизвестен'),
                'response_time': status_data.get('response_time', 0),
                'api_available': status_data.get('api_available', False),
                'nodes_status': f'{status_data.get("nodes_online", 0)}/{status_data.get("total_nodes", 0)} нод онлайн',
                'users_online': status_data.get('users_online', 0),
                'last_check': status_data.get('last_check'),
                'has_issues': status in ['offline', 'degraded'],
            }

            if status == 'offline':
                summary['recommendation'] = 'Проверьте подключение к серверу и работоспособность панели'
            elif status == 'degraded':
                summary['recommendation'] = 'Рекомендуется проверить состояние нод и производительность сервера'
            else:
                summary['recommendation'] = 'Все системы работают нормально'

            return summary

        except Exception as e:
            logger.error('Ошибка получения сводки статуса панели', error=e)
            return {
                'status': 'error',
                'description': '❌ Ошибка проверки статуса',
                'response_time': 0,
                'api_available': False,
                'nodes_status': 'неизвестно',
                'users_online': 0,
                'last_check': datetime.now(UTC),
                'has_issues': True,
                'recommendation': 'Обратитесь к системному администратору',
                'error': str(e),
            }

    async def check_panel_health(self) -> dict[str, Any]:
        attempts = settings.get_maintenance_retry_attempts()
        attempts = max(1, attempts)

        last_result: dict[str, Any] | None = None
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                start_time = datetime.now(UTC)

                async with self.get_api_client() as api:
                    try:
                        system_stats = await api.get_system_stats()
                        api_available = True
                        api_error = None
                    except Exception as e:
                        api_available = False
                        api_error = str(e)
                        system_stats = {}

                    try:
                        nodes = await api.get_all_nodes()
                        nodes_online = sum(1 for node in nodes if node.is_connected and node.is_node_online)
                        total_nodes = len(nodes)
                        nodes_health = 'healthy' if nodes_online > 0 else 'unhealthy'
                    except Exception:
                        nodes_online = 0
                        total_nodes = 0
                        nodes_health = 'unknown'

                    end_time = datetime.now(UTC)
                    response_time = (end_time - start_time).total_seconds()

                    if not api_available:
                        status = 'offline'
                    elif response_time > 10 or nodes_health == 'unhealthy':
                        status = 'degraded'
                    else:
                        status = 'online'

                    result = {
                        'status': status,
                        'api_available': api_available,
                        'api_error': api_error,
                        'response_time': round(response_time, 2),
                        'nodes_online': nodes_online,
                        'total_nodes': total_nodes,
                        'nodes_health': nodes_health,
                        'users_online': system_stats.get('onlineStats', {}).get('onlineNow', 0),
                        'total_users': system_stats.get('users', {}).get('totalUsers', 0),
                        'last_check': end_time,
                        'api_url': settings.REMNAWAVE_API_URL,
                        'attempts_used': attempt,
                    }

                if result['api_available']:
                    if attempt > 1:
                        logger.info('Панель Remnawave ответила с попытки', attempt=attempt)
                    return result

                last_result = result

                if attempt < attempts:
                    logger.warning(
                        'Панель Remnawave недоступна (попытка /)',
                        attempt=attempt,
                        attempts=attempts,
                        result=result.get('api_error') or 'неизвестная ошибка',
                    )
                    await asyncio.sleep(1)

            except Exception as error:
                last_error = error
                if attempt < attempts:
                    logger.warning(
                        'Ошибка проверки здоровья панели (попытка /)', attempt=attempt, attempts=attempts, error=error
                    )
                    await asyncio.sleep(1)
                    continue

                logger.error('Ошибка проверки здоровья панели', error=error)

        if last_result is not None:
            return last_result

        error_message = str(last_error) if last_error else 'Неизвестная ошибка'
        return {
            'status': 'offline',
            'api_available': False,
            'api_error': error_message,
            'response_time': 0,
            'nodes_online': 0,
            'total_nodes': 0,
            'nodes_health': 'unknown',
            'last_check': datetime.now(UTC),
            'api_url': settings.REMNAWAVE_API_URL,
            'attempts_used': attempts,
        }


# Singleton instance for backward compatibility
remnawave_service = RemnaWaveService()
