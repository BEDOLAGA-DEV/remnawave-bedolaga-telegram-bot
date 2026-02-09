import asyncio
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
    TrafficLimitStrategy,
    UserStatus,
)
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)
from app.utils.timezone import get_local_timezone


logger = logging.getLogger(__name__)


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


class _UUIDMapMutation:
    """Tracks in-memory UUID map/user changes so they can be rolled back."""

    __slots__ = ('_map_original', '_user_original', 'uuid_map')

    def __init__(self, uuid_map: dict[str, 'User']):
        self.uuid_map = uuid_map
        self._map_original: dict[str, Any] = {}
        self._user_original: dict[User, tuple[str | None, datetime | None]] = {}

    def _capture_user_state(self, user: Optional['User']) -> None:
        if not user or user in self._user_original:
            return
        self._user_original[user] = (
            getattr(user, 'remnawave_uuid', None),
            getattr(user, 'updated_at', None),
        )

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
            user.remnawave_uuid = uuid_value
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
                '♻️ Обнаружен конфликт UUID %s между пользователями %s и %s. Сбрасываем у старой записи.',
                panel_uuid,
                getattr(conflicting_user, 'telegram_id', '?'),
                getattr(user, 'telegram_id', '?'),
            )
            mutation.set_user_uuid(conflicting_user, None)
            mutation.set_user_updated_at(conflicting_user, datetime.utcnow())
            mutation.remove_map_entry(panel_uuid)

        if current_uuid:
            mutation.remove_map_entry(current_uuid)

        mutation.set_user_uuid(user, panel_uuid)
        mutation.set_user_updated_at(user, datetime.utcnow())
        mutation.set_map_entry(panel_uuid, user)

        logger.info(
            '🔁 Обновлен RemnaWave UUID пользователя %s: %s → %s',
            getattr(user, 'telegram_id', '?'),
            current_uuid,
            panel_uuid,
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
        return datetime.now(self._utc_timezone).replace(tzinfo=None)

    def _local_to_utc(self, local_dt: datetime) -> datetime:
        """Конвертирует naive локальную дату (в таймзоне панели/бота) в naive UTC.

        Используется для корректного сравнения дат из БД с датами из RemnaWave.
        """
        if local_dt.tzinfo is not None:
            # Уже есть tzinfo - конвертируем напрямую
            return local_dt.astimezone(self._utc_timezone).replace(tzinfo=None)
        # Naive datetime - интерпретируем как локальное время панели
        local_aware = local_dt.replace(tzinfo=self._panel_timezone)
        return local_aware.astimezone(self._utc_timezone).replace(tzinfo=None)

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
                utc_normalized = parsed_date.astimezone(self._utc_timezone).replace(tzinfo=None)
            else:
                utc_normalized = parsed_date

            logger.debug(f'Успешно распарсена дата: {date_str} -> {utc_normalized} (UTC)')
            return utc_normalized

        except Exception as e:
            logger.warning(f"⚠️ Не удалось распарсить дату '{date_str}': {e}. Используем дефолтную дату.")
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
            if normalized_expire.tzinfo is not None:
                normalized_expire = normalized_expire.replace(tzinfo=None)

            if normalized_expire < minimum_expire:
                logger.debug(
                    '⚙️ Коррекция даты истечения (%s) до минимально допустимой (%s) для панели',
                    normalized_expire,
                    minimum_expire,
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
            return datetime.min.replace(tzinfo=None)

        expire_at_str = str(expire_at_value).strip()
        if not expire_at_str:
            return datetime.min.replace(tzinfo=None)

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
        logger.debug(f"📥 Парсинг описания пользователя: '{description}'")

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
                logger.debug(f"🔍 Найдена информация о пользователе: '{user_info}'")
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
                f"📱 Найден username: '{username_with_at}' (обработанный: '{username}'), остаток: '{name_part}'"
            )
        else:
            username = None
            name_part = user_info
            logger.debug(f"📱 Username не найден, имя: '{name_part}'")

        # Разделяем имя и фамилию
        if name_part and not name_part.startswith('@'):
            # Если есть имя (не начинается с @), используем его
            name_parts = name_part.split()
            logger.debug(f'🔤 Части имени: {name_parts}')

            if len(name_parts) >= 2:
                # Первое слово - имя, остальные - фамилия
                first_name = name_parts[0]
                last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else None
                logger.debug(f"👤 Имя: '{first_name}', Фамилия: '{last_name}'")
            elif len(name_parts) == 1 and not name_parts[0].startswith('@'):
                # Только имя
                first_name = name_parts[0]
                last_name = None
                logger.debug(f"👤 Только имя: '{first_name}'")
            else:
                first_name = None
                last_name = None
                logger.debug('👤 Имя не определено')
        else:
            first_name = None
            last_name = None
            logger.debug('👤 Имя не определено (начинается с @)')

        logger.debug(
            f"✅ Результат парсинга: first_name='{first_name}', last_name='{last_name}', username='{username}'"
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

            db_user = await create_user_no_commit(**create_kwargs)
            return db_user, True
        except IntegrityError as create_error:
            logger.info(
                '♻️ Пользователь с telegram_id %s уже существует. Используем существующую запись.',
                telegram_id,
            )

            try:
                await db.rollback()
            except Exception:
                # create_user_no_commit уже выполняет rollback при необходимости
                pass

            try:
                existing_user = await get_user_by_telegram_id(db, telegram_id)
                if existing_user is None:
                    logger.error('❌ Не удалось найти существующего пользователя с telegram_id %s', telegram_id)
                    return None, False

                logger.debug(
                    'Используется существующий пользователь %s после конфликта уникальности: %s',
                    telegram_id,
                    create_error,
                )
                return existing_user, False
            except Exception as load_error:
                logger.error('❌ Ошибка загрузки существующего пользователя %s: %s', telegram_id, load_error)
                return None, False
        except Exception as general_error:
            logger.error('❌ Общая ошибка создания/загрузки пользователя %s: %s', telegram_id, general_error)
            try:
                await db.rollback()
            except:
                pass
            return None, False

    async def get_system_statistics(self) -> dict[str, Any]:
        try:
            async with self.get_api_client() as api:
                logger.info('Получение системной статистики RemnaWave...')

                try:
                    system_stats = await api.get_system_stats()
                    logger.info('Системная статистика получена')
                except Exception as e:
                    logger.error(f'Ошибка получения системной статистики: {e}')
                    system_stats = {}

                try:
                    bandwidth_stats = await api.get_bandwidth_stats()
                    logger.info('Статистика трафика получена')
                except Exception as e:
                    logger.error(f'Ошибка получения статистики трафика: {e}')
                    bandwidth_stats = {}

                try:
                    realtime_usage = await api.get_nodes_realtime_usage()
                    logger.info('Реалтайм статистика получена')
                except Exception as e:
                    logger.error(f'Ошибка получения реалтайм статистики: {e}')
                    realtime_usage = []

                try:
                    nodes_stats = await api.get_nodes_statistics()
                except Exception as e:
                    logger.error(f'Ошибка получения статистики нод: {e}')
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
                    logger.warning(f"Не удалось преобразовать uptime '{uptime_value}' в число, используем 0")

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
                        'cpu_physical_cores': system_stats.get('cpu', {}).get('physicalCores', 0),
                        'memory_total': system_stats.get('memory', {}).get('total', 0),
                        'memory_used': system_stats.get('memory', {}).get('used', 0),
                        'memory_free': system_stats.get('memory', {}).get('free', 0),
                        'memory_available': system_stats.get('memory', {}).get('available', 0),
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
                    'last_updated': datetime.now(),
                }

                logger.info(
                    f'Статистика сформирована: пользователи={result["system"]["total_users"]}, общий трафик={total_user_traffic}'
                )
                return result

        except RemnaWaveAPIError as e:
            logger.error(f'Ошибка Remnawave API при получении статистики: {e}')
            return {'error': str(e)}
        except Exception as e:
            logger.error(f'Общая ошибка получения системной статистики: {e}')
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
                    logger.debug(f"Парсинг '{bandwidth_str}': {value} {unit} = {result} байт")
                    return result
                logger.warning(f'Неизвестная единица измерения: {unit}')

            logger.warning(f"Не удалось распарсить строку трафика: '{bandwidth_str}'")
            return 0

        except Exception as e:
            logger.error(f"Ошибка парсинга строки трафика '{bandwidth_str}': {e}")
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
                        }
                    )

                logger.info(f'✅ Получено {len(result)} нод из Remnawave')
                return result

        except Exception as e:
            logger.error(f'Ошибка получения нод из Remnawave: {e}')
            return []

    async def test_connection(self) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.get_system_stats()
                logger.info('✅ Соединение с Remnawave API работает')
                return True

        except Exception as e:
            logger.error(f'❌ Ошибка соединения с Remnawave API: {e}')
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
                    'users_online': node.users_online or 0,
                    'traffic_used_bytes': node.traffic_used_bytes or 0,
                    'traffic_limit_bytes': node.traffic_limit_bytes or 0,
                    'last_status_change': node.last_status_change,
                    'last_status_message': node.last_status_message,
                    'xray_uptime': node.xray_uptime,
                    'is_traffic_tracking_active': node.is_traffic_tracking_active,
                    'traffic_reset_day': node.traffic_reset_day,
                    'notify_percent': node.notify_percent,
                    'consumption_multiplier': node.consumption_multiplier,
                    'cpu_count': node.cpu_count,
                    'cpu_model': node.cpu_model,
                    'total_ram': node.total_ram,
                    'created_at': node.created_at,
                    'updated_at': node.updated_at,
                    'provider_uuid': node.provider_uuid,
                }

        except Exception as e:
            logger.error(f'Ошибка получения информации о ноде {node_uuid}: {e}')
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

                logger.info(f'✅ Действие {action} выполнено для ноды {node_uuid}')
                return True

        except Exception as e:
            logger.error(f'Ошибка управления нодой {node_uuid}: {e}')
            return False

    async def restart_all_nodes(self) -> bool:
        try:
            async with self.get_api_client() as api:
                result = await api.restart_all_nodes()

                if result:
                    logger.info('✅ Команда перезагрузки всех нод отправлена')

                return result

        except Exception as e:
            logger.error(f'Ошибка перезагрузки всех нод: {e}')
            return False

    async def update_squad_inbounds(self, squad_uuid: str, inbound_uuids: list[str]) -> bool:
        try:
            async with self.get_api_client() as api:
                data = {'uuid': squad_uuid, 'inbounds': inbound_uuids}
                await api._make_request('PATCH', '/api/internal-squads', data)
                return True
        except Exception as e:
            logger.error(f'Error updating squad inbounds: {e}')
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

                logger.info(f'✅ Получено {len(result)} сквадов из Remnawave')
                return result

        except Exception as e:
            logger.error(f'Ошибка получения сквадов из Remnawave: {e}')
            return []

    async def create_squad(self, name: str, inbounds: list[str]) -> str | None:
        try:
            async with self.get_api_client() as api:
                squad = await api.create_internal_squad(name, inbounds)

                logger.info(f'✅ Создан новый сквад: {name}')
                return squad.uuid

        except Exception as e:
            logger.error(f'Ошибка создания сквада {name}: {e}')
            return None

    async def update_squad(self, uuid: str, name: str = None, inbounds: list[str] = None) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.update_internal_squad(uuid, name, inbounds)

                logger.info(f'✅ Обновлен сквад {uuid}')
                return True

        except Exception as e:
            logger.error(f'Ошибка обновления сквада {uuid}: {e}')
            return False

    async def delete_squad(self, uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                result = await api.delete_internal_squad(uuid)

                if result:
                    logger.info(f'✅ Удален сквад {uuid}')

                return result

        except Exception as e:
            logger.error(f'Ошибка удаления сквада {uuid}: {e}')
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
                '🚚 Переезд сквада %s → %s: подходящих подписок не найдено',
                source_uuid,
                target_uuid,
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
                subscription.user and subscription.user.remnawave_uuid for subscription in subscriptions
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

                if subscription.user and subscription.user.remnawave_uuid:
                    if api is None:
                        panel_failed += 1
                        logger.error(
                            '❌ RemnaWave API недоступен для обновления пользователя %s',
                            subscription.user.telegram_id,
                        )
                        continue

                    try:
                        await api.update_user(
                            uuid=subscription.user.remnawave_uuid,
                            active_internal_squads=new_squads,
                        )
                        panel_updated += 1
                    except Exception as error:
                        panel_failed += 1
                        logger.error(
                            '❌ Ошибка обновления сквадов пользователя %s: %s',
                            subscription.user.telegram_id,
                            error,
                        )
                        continue

                subscription.connected_squads = new_squads
                subscription.updated_at = datetime.utcnow()

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
                if source_decrement:
                    await db.execute(
                        update(ServerSquad)
                        .where(ServerSquad.id == source_server.id)
                        .values(
                            current_users=func.greatest(
                                ServerSquad.current_users - source_decrement,
                                0,
                            )
                        )
                    )
                if target_increment:
                    await db.execute(
                        update(ServerSquad)
                        .where(ServerSquad.id == target_server.id)
                        .values(current_users=ServerSquad.current_users + target_increment)
                    )

                await db.commit()
            else:
                await db.rollback()

            logger.info(
                '🚚 Завершен переезд сквада %s → %s: обновлено %s подписок (%s не обновлены в панели)',
                source_uuid,
                target_uuid,
                updated_subscriptions,
                panel_failed,
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
            logger.error(
                '❌ Ошибка переезда сквада %s → %s: %s',
                source_uuid,
                target_uuid,
                error,
            )
            return {
                'success': False,
                'error': 'unexpected',
                'message': str(error),
            }
        finally:
            await exit_stack.aclose()

    async def sync_users_from_panel(self, db: AsyncSession, sync_type: str = 'all') -> dict[str, int]:
        try:
            stats = {'created': 0, 'updated': 0, 'errors': 0, 'deleted': 0}

            logger.info(f'🔄 Начинаем синхронизацию типа: {sync_type}')

            async with self.get_api_client() as api:
                panel_users = []
                start = 0
                size = 500  # Увеличен размер батча для ускорения загрузки

                while True:
                    logger.info(f'📥 Загружаем пользователей: start={start}, size={size}')

                    # enrich_happ_links=False - happ_crypto_link уже возвращается API в поле happ.cryptoLink
                    # Не делаем дополнительные HTTP-запросы для каждого пользователя
                    response = await api.get_all_users(start=start, size=size, enrich_happ_links=False)
                    users_batch = response['users']
                    total_users = response['total']

                    logger.info(f'📊 Получено {len(users_batch)} пользователей из {total_users}')

                    for user_obj in users_batch:
                        user_dict = {
                            'uuid': user_obj.uuid,
                            'shortUuid': user_obj.short_uuid,
                            'username': user_obj.username,
                            'status': user_obj.status.value,
                            'telegramId': user_obj.telegram_id,
                            'email': user_obj.email,  # Email для синхронизации email-only пользователей
                            'expireAt': user_obj.expire_at.replace(tzinfo=None).isoformat(),
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

                logger.info(f'✅ Всего загружено пользователей из панели: {len(panel_users)}')

            # Загрузка пользователей с их подписками за один запрос (bulk loading)
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from app.database.models import Subscription, User

            # Получаем всех пользователей с их подписками за один запрос
            bot_users_result = await db.execute(select(User).options(selectinload(User.subscription)))
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
                logger.info(f'📧 Email-only пользователей (без telegram_id): {email_users_count}')

            logger.info(f'📊 Пользователей в боте: {len(bot_users)}')

            panel_users_with_tg = [user for user in panel_users if user.get('telegramId') is not None]

            logger.info(f'📊 Пользователей в панели с Telegram ID: {len(panel_users_with_tg)}')

            unique_panel_users_map = self._deduplicate_panel_users_by_telegram_id(panel_users_with_tg)
            unique_panel_users = list(unique_panel_users_map.values())
            duplicates_count = len(panel_users_with_tg) - len(unique_panel_users)

            if duplicates_count:
                logger.info(
                    '♻️ Обнаружено %s дубликатов пользователей по Telegram ID. Используем самые свежие записи.',
                    duplicates_count,
                )

            panel_telegram_ids = set(unique_panel_users_map.keys())

            # Email-only пользователи из панели (без telegram_id, но с email)
            panel_users_email_only = [
                user for user in panel_users if user.get('telegramId') is None and user.get('email')
            ]
            if panel_users_email_only:
                logger.info(f'📧 Пользователей в панели с Email (без Telegram): {len(panel_users_email_only)}')

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
                        logger.info(f'🔄 Обрабатываем пользователя {i + 1}/{len(unique_panel_users)}: {telegram_id}')

                    db_user = bot_users_by_telegram_id.get(telegram_id)

                    if not db_user:
                        if sync_type in ['new_only', 'all']:
                            logger.info(f'🆕 Создание пользователя для telegram_id {telegram_id}')

                            db_user, is_created = await self._get_or_create_bot_user_from_panel(db, panel_user)

                            if not db_user:
                                logger.error(
                                    '❌ Не удалось создать или получить пользователя для telegram_id %s',
                                    telegram_id,
                                )
                                stats['errors'] += 1
                                continue

                            bot_users_by_telegram_id[telegram_id] = db_user

                            # При синхронизации не обновляем имя и username пользователя
                            # только сохраняем изменения, если были обновлены другие поля (подписка и т.д.)
                            updated_fields = []
                            # Если были обновлены другие поля (подписка, статус и т.д.), сохраняем изменения
                            if updated_fields:
                                logger.info(f'🔄 Обновлены поля {updated_fields} для пользователя {telegram_id}')
                                await db.flush()  # Сохраняем изменения без коммита

                            _, uuid_mutation = self._ensure_user_remnawave_uuid(
                                db_user,
                                panel_user.get('uuid'),
                                bot_users_by_uuid,
                            )

                            if is_created:
                                await self._create_subscription_from_panel_data(db, db_user, panel_user)
                                stats['created'] += 1
                                logger.info(f'✅ Создан пользователь {telegram_id} с подпиской')
                            else:
                                # Обновляем данные существующего пользователя
                                # Но теперь мы уже загрузили подписку с пользователем, нет необходимости перезагружать
                                await self._update_subscription_from_panel_data(db, db_user, panel_user)
                                stats['updated'] += 1
                                logger.info(f'♻️ Обновлена подписка существующего пользователя {telegram_id}')

                    elif sync_type in ['update_only', 'all']:
                        logger.debug(f'🔄 Обновление пользователя {telegram_id}')

                        # При синхронизации не обновляем имя и username пользователя
                        # только сохраняем изменения, если были обновлены другие поля (подписка и т.д.)
                        updated_fields = []
                        # Если были обновлены другие поля (подписка, статус и т.д.), сохраняем изменения
                        if updated_fields:
                            logger.info(f'🔄 Обновлены поля {updated_fields} для пользователя {telegram_id}')
                            await db.flush()  # Сохраняем изменения без коммита

                        # Обновляем UUID ДО операций с подпиской, чтобы избежать
                        # greenlet_spawn ошибки при доступе к атрибутам после flush
                        _, uuid_mutation = self._ensure_user_remnawave_uuid(
                            db_user,
                            panel_user.get('uuid'),
                            bot_users_by_uuid,
                        )

                        # Используем async запрос вместо доступа к relationship,
                        # чтобы избежать lazy-load в async контексте
                        from app.database.crud.subscription import get_subscription_by_user_id as _get_sub

                        existing_sub = await _get_sub(db, db_user.id)
                        if existing_sub:
                            await self._update_subscription_from_panel_data(db, db_user, panel_user)
                        else:
                            await self._create_subscription_from_panel_data(db, db_user, panel_user)

                        stats['updated'] += 1
                        logger.debug(f'✅ Обновлён пользователь {telegram_id}')

                except Exception as user_error:
                    logger.error(f'❌ Ошибка обработки пользователя {telegram_id}: {user_error}')
                    stats['errors'] += 1
                    if uuid_mutation:
                        uuid_mutation.rollback()
                    if pending_uuid_mutations:
                        for mutation in reversed(pending_uuid_mutations):
                            mutation.rollback()
                        pending_uuid_mutations.clear()
                    try:
                        await db.rollback()  # Выполняем rollback при ошибке
                    except:
                        pass
                    continue

                else:
                    if uuid_mutation and uuid_mutation.has_changes():
                        pending_uuid_mutations.append(uuid_mutation)

                # Коммитим изменения каждые N пользователей для ускорения
                if (i + 1) % batch_size == 0:
                    try:
                        await db.commit()
                        logger.debug(f'📦 Коммит изменений после обработки {i + 1} пользователей')
                        pending_uuid_mutations.clear()
                    except Exception as commit_error:
                        logger.error(f'❌ Ошибка коммита после обработки {i + 1} пользователей: {commit_error}')
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
                logger.error(f'❌ Ошибка финального коммита: {final_commit_error}')
                await db.rollback()
                for mutation in reversed(pending_uuid_mutations):
                    mutation.rollback()
                pending_uuid_mutations.clear()

            # Обработка email-only пользователей из панели
            if panel_users_email_only and sync_type in ['new_only', 'all']:
                logger.info(f'📧 Обработка {len(panel_users_email_only)} email-only пользователей из панели...')

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

                            # Используем async запрос вместо доступа к relationship,
                            # чтобы избежать lazy-load (greenlet_spawn) в async контексте
                            from app.database.crud.subscription import get_subscription_by_user_id as _get_sub_email

                            existing_sub = await _get_sub_email(db, db_user.id)
                            if existing_sub:
                                await self._update_subscription_from_panel_data(db, db_user, panel_user)
                            else:
                                await self._create_subscription_from_panel_data(db, db_user, panel_user)

                            stats['updated'] += 1
                            logger.info(f'📧 Обновлен email-пользователь: {panel_email}')
                        else:
                            # Email-only пользователи не создаются автоматически при синхронизации,
                            # они должны сначала зарегистрироваться через cabinet
                            logger.debug(f'📧 Email-пользователь {panel_email} не найден в боте, пропускаем')

                    except Exception as email_user_error:
                        logger.error(f'❌ Ошибка обработки email-пользователя: {email_user_error}')
                        stats['errors'] += 1

                try:
                    await db.commit()
                except Exception as email_commit_error:
                    logger.error(f'❌ Ошибка коммита email-пользователей: {email_commit_error}')
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
                    and hasattr(db_user, 'subscription')
                    and db_user.subscription
                ]

                if users_to_deactivate:
                    logger.info(f'📊 Найдено {len(users_to_deactivate)} пользователей для деактивации')

                # Используем один API клиент для всех операций сброса HWID
                hwid_api_client = None
                hwid_api_cm = None
                try:
                    hwid_api_cm = self.get_api_client()
                    hwid_api_client = await hwid_api_cm.__aenter__()
                except Exception as api_init_error:
                    logger.warning(f'⚠️ Не удалось создать API клиент для сброса HWID: {api_init_error}')
                    hwid_api_client = None
                    hwid_api_cm = None

                try:
                    for telegram_id, db_user in users_to_deactivate:
                        cleanup_mutation: _UUIDMapMutation | None = None
                        try:
                            logger.info(f'🗑️ Деактивация подписки пользователя {telegram_id} (нет в панели)')

                            subscription = db_user.subscription

                            if db_user.remnawave_uuid and hwid_api_client:
                                try:
                                    devices_reset = await hwid_api_client.reset_user_devices(db_user.remnawave_uuid)
                                    if devices_reset:
                                        logger.info(f'🔧 Сброшены HWID устройства для пользователя {telegram_id}')
                                except Exception as hwid_error:
                                    logger.error(f'❌ Ошибка сброса HWID устройств для {telegram_id}: {hwid_error}')

                            try:
                                from sqlalchemy import delete

                                from app.database.models import SubscriptionServer

                                await decrement_subscription_server_counts(db, subscription)

                                await db.execute(
                                    delete(SubscriptionServer).where(
                                        SubscriptionServer.subscription_id == subscription.id
                                    )
                                )
                                logger.info(f'🗑️ Удалены серверы подписки для {telegram_id}')
                            except Exception as servers_error:
                                logger.warning(f'⚠️ Не удалось удалить серверы подписки: {servers_error}')

                            from app.database.models import SubscriptionStatus

                            # Проверяем, была ли это платная подписка
                            was_paid = not subscription.is_trial or getattr(db_user, 'has_had_paid_subscription', False)

                            subscription.status = SubscriptionStatus.DISABLED.value

                            if was_paid:
                                # Для платных подписок - НЕ сбрасываем is_trial и end_date!
                                # Сохраняем оригинальные значения чтобы можно было восстановить
                                logger.warning(
                                    f'⚠️ ПЛАТНАЯ подписка пользователя {telegram_id} отключена (нет в панели), '
                                    f'но is_trial={subscription.is_trial} и end_date={subscription.end_date} СОХРАНЕНЫ'
                                )
                            else:
                                # Для триальных подписок - сбрасываем как раньше
                                subscription.is_trial = True
                                subscription.end_date = datetime.utcnow()
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
                            cleanup_mutation.set_user_updated_at(db_user, datetime.utcnow())

                            stats['deleted'] += 1
                            logger.info(f'✅ Деактивирована подписка пользователя {telegram_id} (сохранен баланс)')

                            processed_count += 1

                        except Exception as delete_error:
                            logger.error(f'❌ Ошибка деактивации подписки {telegram_id}: {delete_error}')
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
                                    logger.debug(f'📦 Коммит изменений после деактивации {processed_count} подписок')
                                    cleanup_uuid_mutations.clear()
                                except Exception as commit_error:
                                    logger.error(
                                        f'❌ Ошибка коммита после деактивации {processed_count} подписок: {commit_error}'
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
                        logger.error(f'❌ Ошибка финального коммита при деактивации: {final_commit_error}')
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
                f'🎯 Синхронизация завершена: создано {stats["created"]}, обновлено {stats["updated"]}, деактивировано {stats["deleted"]}, ошибок {stats["errors"]}'
            )
            return stats

        except Exception as e:
            logger.error(f'❌ Критическая ошибка синхронизации пользователей: {e}')
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
            logger.info(f'✅ Подготовлена подписка для пользователя {user.telegram_id} до {expire_at}')

        except Exception as e:
            logger.error(f'❌ Ошибка создания подписки для пользователя {user.telegram_id}: {e}')
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
                logger.info(f'✅ Подготовлена базовая подписка для пользователя {user.telegram_id}')
            except Exception as basic_error:
                logger.error(f'❌ Ошибка создания базовой подписки: {basic_error}')

    async def _update_subscription_from_panel_data(self, db: AsyncSession, user, panel_user):
        try:
            from app.database.crud.subscription import get_subscription_by_user_id
            from app.database.models import SubscriptionStatus

            # Всегда используем async CRUD запрос для получения подписки,
            # чтобы избежать lazy-load (greenlet_spawn) в async контексте
            subscription = await get_subscription_by_user_id(db, user.id)

            if not subscription:
                await self._create_subscription_from_panel_data(db, user, panel_user)
                return

            panel_status = panel_user.get('status', 'ACTIVE')
            expire_at_str = panel_user.get('expireAt', '')

            if expire_at_str:
                # expire_at приходит в UTC (naive) из _parse_remnawave_date
                expire_at = self._parse_remnawave_date(expire_at_str)

                # Конвертируем локальную дату из БД в UTC для корректного сравнения
                # subscription.end_date хранится в локальной таймзоне (MSK)
                local_end_date_utc = self._local_to_utc(subscription.end_date)

                # КРИТИЧНО: НЕ перезаписываем end_date если локальная дата ПОЗЖЕ
                # Это защищает от ситуации когда подписка была продлена в боте,
                # но RemnaWave ещё не получил обновление или вернул старую дату
                time_diff = abs((local_end_date_utc - expire_at).total_seconds())
                if time_diff > 60:
                    if expire_at > local_end_date_utc:
                        # RemnaWave имеет более позднюю дату - обновляем
                        # Конвертируем UTC обратно в локальное время для сохранения в БД
                        new_end_date_local = (
                            expire_at.replace(tzinfo=self._utc_timezone)
                            .astimezone(self._panel_timezone)
                            .replace(tzinfo=None)
                        )
                        logger.info(
                            f'✅ Sync: обновлена end_date для user {getattr(user, "telegram_id", "?")}: '
                            f'{subscription.end_date} -> {new_end_date_local} (разница: {time_diff:.0f}с)'
                        )
                        subscription.end_date = new_end_date_local
                    else:
                        # Локальная дата позже - НЕ перезаписываем
                        logger.debug(
                            f'⏭️ Sync: end_date для user {getattr(user, "telegram_id", "?")} актуальна: '
                            f'локальная ({subscription.end_date} / UTC: {local_end_date_utc}) >= RemnaWave ({expire_at} UTC)'
                        )
                else:
                    logger.debug(
                        f'⏭️ Sync: пропускаем обновление end_date для user {getattr(user, "telegram_id", "?")}: '
                        f'разница слишком мала ({time_diff:.0f}с < 60с)'
                    )

            current_time = self._now_utc()
            # Конвертируем end_date в UTC для корректного сравнения с current_time
            end_date_utc = self._local_to_utc(subscription.end_date)

            if panel_status == 'ACTIVE' and end_date_utc > current_time:
                new_status = SubscriptionStatus.ACTIVE.value
            elif panel_status == 'DISABLED':
                new_status = SubscriptionStatus.DISABLED.value
            elif end_date_utc <= current_time:
                # КРИТИЧНО: НЕ деактивируем если текущий статус ACTIVE
                # Это защищает от race condition когда sync использует старую end_date из памяти,
                # а реальная end_date уже обновлена продлением
                if subscription.status == SubscriptionStatus.ACTIVE.value:
                    logger.warning(
                        f'⚠️ Sync: пропускаем деактивацию подписки user {getattr(user, "telegram_id", "?")}: '
                        f'статус ACTIVE, end_date ({subscription.end_date} / UTC: {end_date_utc}) <= now ({current_time}). '
                        f'Деактивация будет выполнена через middleware с буфером.'
                    )
                    new_status = subscription.status  # Сохраняем текущий статус
                else:
                    new_status = SubscriptionStatus.EXPIRED.value
            else:
                new_status = subscription.status

            if subscription.status != new_status:
                subscription.status = new_status
                logger.debug(f'Обновлен статус подписки: {new_status}')

            used_traffic_bytes = _get_user_traffic_bytes(panel_user)
            traffic_used_gb = used_traffic_bytes / (1024**3)

            if abs(subscription.traffic_used_gb - traffic_used_gb) > 0.01:
                subscription.traffic_used_gb = traffic_used_gb
                logger.debug(f'Обновлен использованный трафик: {traffic_used_gb} GB')

            traffic_limit_bytes = panel_user.get('trafficLimitBytes', 0)
            traffic_limit_gb = traffic_limit_bytes // (1024**3) if traffic_limit_bytes > 0 else 0

            if subscription.traffic_limit_gb != traffic_limit_gb:
                subscription.traffic_limit_gb = traffic_limit_gb
                logger.debug(f'Обновлен лимит трафика: {traffic_limit_gb} GB')

            device_limit = panel_user.get('hwidDeviceLimit', 1) or 1
            if subscription.device_limit != device_limit:
                subscription.device_limit = device_limit
                logger.debug(f'Обновлен лимит устройств: {device_limit}')

            new_short_uuid = panel_user.get('shortUuid')
            if new_short_uuid and subscription.remnawave_short_uuid != new_short_uuid:
                old_short_uuid = subscription.remnawave_short_uuid
                subscription.remnawave_short_uuid = new_short_uuid
                logger.debug(
                    'Обновлен short UUID подписки пользователя %s: %s → %s',
                    getattr(user, 'telegram_id', '?'),
                    old_short_uuid,
                    new_short_uuid,
                )

            panel_url = panel_user.get('subscriptionUrl', '')
            if not subscription.subscription_url or subscription.subscription_url != panel_url:
                subscription.subscription_url = panel_url

            panel_crypto_link = panel_user.get('subscriptionCryptoLink') or (panel_user.get('happ') or {}).get(
                'cryptoLink', ''
            )
            if panel_crypto_link and subscription.subscription_crypto_link != panel_crypto_link:
                subscription.subscription_crypto_link = panel_crypto_link

            active_squads = panel_user.get('activeInternalSquads', [])
            squad_uuids = []
            if isinstance(active_squads, list):
                for squad in active_squads:
                    if isinstance(squad, dict) and 'uuid' in squad:
                        squad_uuids.append(squad['uuid'])
                    elif isinstance(squad, str):
                        squad_uuids.append(squad)

            current_squads = set(subscription.connected_squads or [])
            new_squads = set(squad_uuids)

            if current_squads != new_squads:
                subscription.connected_squads = squad_uuids
                logger.debug(f'Обновлены подключенные сквады: {squad_uuids}')

            # Коммитим изменения позже, в основном цикле, чтобы уменьшить количество транзакций
            logger.debug(f'✅ Обновлена подписка для пользователя {user.telegram_id}')

        except Exception as e:
            logger.error(f'❌ Ошибка обновления подписки для пользователя {user.telegram_id}: {e}')
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
                                is_subscription_active = (
                                    sub.status
                                    in (
                                        SubscriptionStatus.ACTIVE.value,
                                        SubscriptionStatus.TRIAL.value,
                                    )
                                    and sub.end_date > datetime.utcnow()
                                )
                                status = UserStatus.ACTIVE if is_subscription_active else UserStatus.DISABLED

                                username = settings.format_remnawave_username(
                                    full_name=user.full_name,
                                    username=user.username,
                                    telegram_id=user.telegram_id,
                                    email=user.email,
                                    user_id=user.id,
                                )

                                create_kwargs = dict(
                                    username=username,
                                    expire_at=expire_at,
                                    status=status,
                                    traffic_limit_bytes=sub.traffic_limit_gb * (1024**3)
                                    if sub.traffic_limit_gb > 0
                                    else 0,
                                    traffic_limit_strategy=TrafficLimitStrategy.MONTH,
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

                                # Определяем UUID для обновления
                                panel_uuid = user.remnawave_uuid

                                # Если нет UUID в базе, ищем пользователя по telegram_id в панели
                                if not panel_uuid and user.telegram_id:
                                    existing_users = await api.get_user_by_telegram_id(user.telegram_id)
                                    if existing_users:
                                        panel_uuid = existing_users[0].uuid
                                        logger.debug(f'Найден пользователь {user.telegram_id} в панели: {panel_uuid}')

                                # Fallback: поиск по email (для OAuth юзеров без telegram_id)
                                if not panel_uuid and user.email:
                                    existing_users = await api.get_user_by_email(user.email)
                                    if existing_users:
                                        panel_uuid = existing_users[0].uuid
                                        logger.debug(
                                            f'Найден пользователь {user.email} в панели по email: {panel_uuid}'
                                        )

                                if panel_uuid:
                                    update_kwargs = dict(
                                        uuid=panel_uuid,
                                        status=status,
                                        expire_at=expire_at,
                                        traffic_limit_bytes=create_kwargs['traffic_limit_bytes'],
                                        traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                                        email=user.email,
                                        description=create_kwargs['description'],
                                        active_internal_squads=sub.connected_squads,
                                    )

                                    if hwid_limit is not None:
                                        update_kwargs['hwid_device_limit'] = hwid_limit

                                    try:
                                        await api.update_user(**update_kwargs)
                                        # Сохраняем UUID если его не было
                                        if not user.remnawave_uuid:
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
                                    f'Ошибка синхронизации пользователя {sub.user.telegram_id if sub.user else "N/A"} в панель: {e}'
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
                        logger.error(
                            'Ошибка фиксации транзакции при синхронизации в панель: %s',
                            commit_error,
                        )
                        await db.rollback()
                        stats['errors'] += len(valid_subscriptions)

                    logger.info(
                        f'📦 Обработано {offset + len(subscriptions)} подписок: '
                        f'создано {stats["created"]}, обновлено {stats["updated"]}, ошибок {stats["errors"]}'
                    )

                    if len(subscriptions) < batch_size:
                        break

                    offset += batch_size

            logger.info(
                f'✅ Синхронизация в панель завершена: создано {stats["created"]}, обновлено {stats["updated"]}, ошибок {stats["errors"]}'
            )
            return stats

        except Exception as e:
            logger.error(f'Ошибка синхронизации пользователей в панель: {e}')
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
            logger.error(f'Ошибка получения статистики трафика для пользователя {telegram_id}: {e}')
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
            logger.error(f'Ошибка получения статистики трафика по UUID {remnawave_uuid}: {e}')
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
                            f"Найден пользователь по username '{user_identifier}': telegram_id={user.telegram_id}"
                        )
                        return user.telegram_id
                except Exception as e:
                    logger.debug(f"Пользователь не найден по username '{user_identifier}': {e}")

                # Если не нашли по username, ищем по email среди всех пользователей
                try:
                    all_users_response = await api.get_all_users(start=0, size=10000)
                    users_list = all_users_response.get('users', [])

                    for panel_user in users_list:
                        panel_email = panel_user.email if hasattr(panel_user, 'email') else None
                        if panel_email and panel_email.lower() == user_identifier.lower():
                            panel_telegram_id = panel_user.telegram_id if hasattr(panel_user, 'telegram_id') else None
                            if panel_telegram_id:
                                logger.info(
                                    f"Найден пользователь по email '{user_identifier}': telegram_id={panel_telegram_id}"
                                )
                                return panel_telegram_id
                except Exception as e:
                    logger.warning(f"Ошибка поиска пользователя по email '{user_identifier}': {e}")

                logger.warning(f"Пользователь с идентификатором '{user_identifier}' не найден в панели")
                return None

        except Exception as e:
            logger.error(f"Ошибка получения telegram_id для '{user_identifier}': {e}")
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
            logger.error(f'Ошибка получения актуального использования нод: {e}')
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
            logger.error(f'Error getting squad details: {e}')
            return None

    async def add_all_users_to_squad(self, squad_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                response = await api._make_request('POST', f'/api/internal-squads/{squad_uuid}/bulk-actions/add-users')
                return response.get('response', {}).get('eventSent', False)
        except Exception as e:
            logger.error(f'Error adding users to squad: {e}')
            return False

    async def remove_all_users_from_squad(self, squad_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                response = await api._make_request(
                    'DELETE', f'/api/internal-squads/{squad_uuid}/bulk-actions/remove-users'
                )
                return response.get('response', {}).get('eventSent', False)
        except Exception as e:
            logger.error(f'Error removing users from squad: {e}')
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
            logger.error(f'Error getting all inbounds: {e}')
            return []

    async def rename_squad(self, squad_uuid: str, new_name: str) -> bool:
        try:
            async with self.get_api_client() as api:
                data = {'uuid': squad_uuid, 'name': new_name}
                await api._make_request('PATCH', '/api/internal-squads', data)
                return True
        except Exception as e:
            logger.error(f'Error renaming squad: {e}')
            return False

    async def get_node_user_usage_by_range(self, node_uuid: str, start_date, end_date) -> list[dict[str, Any]]:
        try:
            async with self.get_api_client() as api:
                start_str = start_date.isoformat() + 'Z'
                end_str = end_date.isoformat() + 'Z'

                params = {'start': start_str, 'end': end_str}

                usage_data = await api._make_request(
                    'GET', f'/api/bandwidth-stats/nodes/{node_uuid}/users/legacy', params=params
                )

                return usage_data.get('response', [])

        except Exception as e:
            logger.error(f'Ошибка получения статистики использования ноды {node_uuid}: {e}')
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

            end_date = datetime.now()
            start_date = end_date - timedelta(days=7)

            usage_history = await self.get_node_user_usage_by_range(node_uuid, start_date, end_date)

            return {
                'node': node,
                'realtime': node_realtime,
                'usage_history': usage_history,
                'last_updated': datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f'Ошибка получения статистики ноды {node_uuid}: {e}')

    async def validate_user_data_before_sync(self, panel_user) -> bool:
        try:
            if not panel_user.telegram_id:
                logger.debug(f'Нет telegram_id для пользователя {panel_user.uuid}')
                return False

            if not panel_user.uuid:
                logger.debug(f'Нет UUID для пользователя {panel_user.telegram_id}')
                return False

            if panel_user.telegram_id <= 0:
                logger.debug(f'Некорректный telegram_id: {panel_user.telegram_id}')
                return False

            return True

        except Exception as e:
            logger.error(f'Ошибка валидации данных пользователя: {e}')
            return False

    async def force_cleanup_user_data(self, db: AsyncSession, user: User) -> bool:
        """
        ОПАСНАЯ ФУНКЦИЯ: Полностью сбрасывает все данные пользователя включая баланс!
        Используйте только для полной очистки пользователя.
        """
        try:
            # Предупреждение для платных пользователей
            was_paid = (
                user.has_had_paid_subscription
                or (user.subscription and not user.subscription.is_trial)
                or user.balance_kopeks > 0
            )
            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            if was_paid:
                logger.warning(
                    f'⚠️ ВНИМАНИЕ: force_cleanup_user_data вызвана для ПЛАТНОГО пользователя {user_id_display}! '
                    f'has_had_paid_subscription={user.has_had_paid_subscription}, '
                    f'balance={user.balance_kopeks}, '
                    f'is_trial={user.subscription.is_trial if user.subscription else "N/A"}'
                )

            logger.info(f'🗑️ ПРИНУДИТЕЛЬНАЯ полная очистка данных пользователя {user_id_display}')

            if user.remnawave_uuid:
                try:
                    async with self.get_api_client() as api:
                        devices_reset = await api.reset_user_devices(user.remnawave_uuid)
                        if devices_reset:
                            logger.info(f'🔧 Сброшены HWID устройства для {user_id_display}')
                except Exception as hwid_error:
                    logger.warning(f'⚠️ Ошибка сброса HWID устройств: {hwid_error}')

            try:
                from sqlalchemy import delete

                from app.database.models import (
                    PromoCodeUse,
                    ReferralEarning,
                    SubscriptionServer,
                    SubscriptionStatus,
                    Transaction,
                )

                if user.subscription:
                    await decrement_subscription_server_counts(db, user.subscription)

                    await db.execute(
                        delete(SubscriptionServer).where(SubscriptionServer.subscription_id == user.subscription.id)
                    )
                    logger.info(f'🗑️ Удалены серверы подписки для {user_id_display}')

                await db.execute(delete(Transaction).where(Transaction.user_id == user.id))
                logger.info(f'🗑️ Удалены транзакции для {user_id_display}')

                await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user.id))
                await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user.id))
                logger.info(f'🗑️ Удалены реферальные доходы для {user_id_display}')

                await db.execute(delete(PromoCodeUse).where(PromoCodeUse.user_id == user.id))
                logger.info(f'🗑️ Удалены использования промокодов для {user_id_display}')

            except Exception as records_error:
                logger.error(f'❌ Ошибка удаления связанных записей: {records_error}')

            try:
                user.balance_kopeks = 0
                user.remnawave_uuid = None
                user.has_had_paid_subscription = False
                user.used_promocodes = 0
                user.updated_at = self._now_utc()

                if user.subscription:
                    user.subscription.status = SubscriptionStatus.DISABLED.value
                    user.subscription.is_trial = True
                    user.subscription.end_date = self._now_utc()
                    user.subscription.traffic_limit_gb = 0
                    user.subscription.traffic_used_gb = 0.0
                    user.subscription.device_limit = 1
                    user.subscription.connected_squads = []
                    user.subscription.autopay_enabled = False
                    user.subscription.autopay_days_before = settings.DEFAULT_AUTOPAY_DAYS_BEFORE
                    user.subscription.remnawave_short_uuid = None
                    user.subscription.subscription_url = ''
                    user.subscription.subscription_crypto_link = ''
                    user.subscription.updated_at = self._now_utc()

                await db.commit()

                logger.info(f'✅ ПРИНУДИТЕЛЬНО очищены ВСЕ данные пользователя {user_id_display}')
                return True

            except Exception as cleanup_error:
                logger.error(f'❌ Ошибка финальной очистки пользователя: {cleanup_error}')
                await db.rollback()
                return False

        except Exception as e:
            logger.error(f'❌ Критическая ошибка принудительной очистки пользователя {user.telegram_id}: {e}')
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

            logger.info(f'📊 Найдено {len(panel_telegram_ids)} пользователей в панели')

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

                        if subscription.status == SubscriptionStatus.DISABLED.value:
                            continue

                        if user.telegram_id not in panel_telegram_ids:
                            logger.info(
                                f'🗑️ ПОЛНАЯ деактивация подписки пользователя {user.telegram_id} (отсутствует в панели)'
                            )

                            cleanup_success = await self.force_cleanup_user_data(db, user)

                            if cleanup_success:
                                stats['deactivated'] += 1
                            else:
                                stats['errors'] += 1

                    except Exception as sub_error:
                        logger.error(f'❌ Ошибка обработки подписки {subscription.id}: {sub_error}')
                        stats['errors'] += 1

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                f'🧹 Усиленная очистка завершена: проверено {stats["checked"]}, деактивировано {stats["deactivated"]}, ошибок {stats["errors"]}'
            )
            return stats

        except Exception as e:
            logger.error(f'❌ Критическая ошибка усиленной очистки подписок: {e}')
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

            logger.info(f'📊 Найдено {len(panel_users_dict)} пользователей в панели для синхронизации')

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
                            logger.debug(f'Пропускаем email-пользователя {user.id} при синхронизации с панелью')
                            continue

                        panel_user = panel_users_dict.get(user.telegram_id)

                        if panel_user:
                            await self._update_subscription_from_panel_data(db, user, panel_user)
                            stats['updated'] += 1
                        elif subscription.status != SubscriptionStatus.DISABLED.value:
                            logger.info(f'🗑️ Деактивируем подписку пользователя {user.telegram_id} (нет в панели)')

                            from app.database.crud.subscription import deactivate_subscription

                            await deactivate_subscription(db, subscription)
                            stats['updated'] += 1

                    except Exception as sub_error:
                        logger.error(f'❌ Ошибка синхронизации подписки {subscription.id}: {sub_error}')
                        stats['errors'] += 1

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                f'🔄 Синхронизация статусов завершена: проверено {stats["checked"]}, обновлено {stats["updated"]}, ошибок {stats["errors"]}'
            )
            return stats

        except Exception as e:
            logger.error(f'❌ Критическая ошибка синхронизации статусов: {e}')
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

                        current_time = self._now_utc()
                        # Конвертируем end_date в UTC для корректного сравнения
                        end_date_utc = self._local_to_utc(subscription.end_date)
                        # Добавляем буфер 5 минут для защиты от race condition при продлении
                        expiry_buffer = timedelta(minutes=5)
                        if (
                            end_date_utc + expiry_buffer <= current_time
                            and subscription.status == SubscriptionStatus.ACTIVE.value
                        ):
                            time_since_expiry = current_time - end_date_utc
                            logger.warning(
                                f'🔧 fix_data_issues: деактивируем подписку {subscription.id} '
                                f'(user={user.telegram_id}), просрочена на {time_since_expiry}'
                            )
                            subscription.status = SubscriptionStatus.EXPIRED.value
                            issues_fixed += 1

                        if not subscription.remnawave_short_uuid and user.remnawave_uuid:
                            try:
                                async with self.get_api_client() as api:
                                    rw_user = await api.get_user_by_uuid(user.remnawave_uuid)
                                    if rw_user:
                                        subscription.remnawave_short_uuid = rw_user.short_uuid
                                        subscription.subscription_url = rw_user.subscription_url
                                        subscription.subscription_crypto_link = rw_user.happ_crypto_link
                                        logger.info(f'🔧 Восстановлены данные Remnawave для {user.telegram_id}')
                                        issues_fixed += 1
                            except Exception as rw_error:
                                logger.warning(
                                    f'⚠️ Не удалось получить данные Remnawave для {user.telegram_id}: {rw_error}'
                                )

                        if subscription.traffic_limit_gb < 0:
                            subscription.traffic_limit_gb = 0
                            logger.info(f'🔧 Исправлен некорректный лимит трафика для {user.telegram_id}')
                            issues_fixed += 1

                        if subscription.traffic_used_gb < 0:
                            subscription.traffic_used_gb = 0.0
                            logger.info(f'🔧 Исправлено некорректное использование трафика для {user.telegram_id}')
                            issues_fixed += 1

                        if subscription.device_limit <= 0:
                            subscription.device_limit = 1
                            logger.info(f'🔧 Исправлен лимит устройств для {user.telegram_id}')
                            issues_fixed += 1

                        if subscription.connected_squads is None:
                            subscription.connected_squads = []
                            logger.info(f'🔧 Инициализирован список сквадов для {user.telegram_id}')
                            issues_fixed += 1

                        if issues_fixed > 0:
                            stats['issues_found'] += issues_fixed
                            stats['fixed'] += 1
                            await db.commit()

                    except Exception as sub_error:
                        logger.error(f'❌ Ошибка валидации подписки {subscription.id}: {sub_error}')
                        stats['errors'] += 1
                        await db.rollback()

                page += 1
                if len(subscriptions) < limit:
                    break

            logger.info(
                f'🔍 Валидация завершена: проверено {stats["checked"]}, исправлено подписок {stats["fixed"]}, найдено проблем {stats["issues_found"]}, ошибок {stats["errors"]}'
            )
            return stats

        except Exception as e:
            logger.error(f'❌ Критическая ошибка валидации: {e}')
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

            users_without_uuid = sum(1 for user in bot_users if not user.remnawave_uuid and user.subscription)

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
            logger.error(f'❌ Ошибка получения рекомендаций: {e}')
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
            logger.error(f'Ошибка мониторинга статуса панели Remnawave: {e}')
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

            logger.info(f'Отправлено уведомление об изменении статуса панели: {old_status} -> {new_status}')

        except Exception as e:
            logger.error(f'Ошибка отправки уведомления об изменении статуса: {e}')

    async def send_manual_status_notification(self, bot, status: str, message: str = ''):
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(bot)

            details = {
                'api_url': settings.REMNAWAVE_API_URL,
                'last_check': datetime.utcnow(),
                'manual_message': message,
            }

            if status == 'maintenance':
                details['maintenance_reason'] = message or 'Плановое обслуживание'

            await notification_service.send_remnawave_panel_status_notification(status, details)

            logger.info(f'Отправлено ручное уведомление о статусе панели: {status}')
            return True

        except Exception as e:
            logger.error(f'Ошибка отправки ручного уведомления: {e}')
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
            logger.error(f'Ошибка получения сводки статуса панели: {e}')
            return {
                'status': 'error',
                'description': '❌ Ошибка проверки статуса',
                'response_time': 0,
                'api_available': False,
                'nodes_status': 'неизвестно',
                'users_online': 0,
                'last_check': datetime.utcnow(),
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
                start_time = datetime.utcnow()

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

                    end_time = datetime.utcnow()
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
                        logger.info('Панель Remnawave ответила с %s попытки', attempt)
                    return result

                last_result = result

                if attempt < attempts:
                    logger.warning(
                        'Панель Remnawave недоступна (попытка %s/%s): %s',
                        attempt,
                        attempts,
                        result.get('api_error') or 'неизвестная ошибка',
                    )
                    await asyncio.sleep(1)

            except Exception as error:
                last_error = error
                if attempt < attempts:
                    logger.warning(
                        'Ошибка проверки здоровья панели (попытка %s/%s): %s',
                        attempt,
                        attempts,
                        error,
                    )
                    await asyncio.sleep(1)
                    continue

                logger.error(f'Ошибка проверки здоровья панели: {error}')

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
            'last_check': datetime.utcnow(),
            'api_url': settings.REMNAWAVE_API_URL,
            'attempts_used': attempts,
        }


# Singleton instance for backward compatibility
remnawave_service = RemnaWaveService()
