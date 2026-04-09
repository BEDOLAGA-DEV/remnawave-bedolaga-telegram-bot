"""
Сервис для работы с черным списком пользователей
Проверяет пользователей по списку из GitHub репозитория
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class BlacklistService:
    """
    Сервис для проверки пользователей по черному списку
    """

    def __init__(self):
        self.blacklist_data = []  # Список в формате [(telegram_id, username, reason), ...]
        self.last_update = None
        # Используем интервал из настроек, по умолчанию 24 часа
        interval_hours = self.get_blacklist_update_interval_hours()
        self.update_interval = timedelta(hours=interval_hours)
        self.lock = asyncio.Lock()  # Блокировка для предотвращения одновременных обновлений
        # Кэш результатов проверки: {telegram_id: (is_blacklisted, reason, timestamp)}
        self._check_cache: dict[int, tuple[bool, str | None, float]] = {}
        self._cache_ttl = 300  # 5 минут

    def is_blacklist_check_enabled(self) -> bool:
        """Проверяет, включена ли проверка черного списка"""
        return getattr(settings, 'BLACKLIST_CHECK_ENABLED', False)

    def get_blacklist_github_url(self) -> str | None:
        """Получает URL к файлу черного списка на GitHub"""
        return getattr(settings, 'BLACKLIST_GITHUB_URL', None)

    def get_blacklist_update_interval_hours(self) -> int:
        """Получает интервал обновления черного списка в часах"""
        return getattr(settings, 'BLACKLIST_UPDATE_INTERVAL_HOURS', 24)

    def should_ignore_admins(self) -> bool:
        """Проверяет, нужно ли игнорировать администраторов при проверке черного списка"""
        return getattr(settings, 'BLACKLIST_IGNORE_ADMINS', True)

    def is_admin(self, telegram_id: int) -> bool:
        """Проверяет, является ли пользователь администратором"""
        return settings.is_admin(telegram_id)

    async def update_blacklist(self) -> bool:
        """
        Обновляет черный список из GitHub репозитория
        """
        async with self.lock:
            github_url = self.get_blacklist_github_url()
            if not github_url:
                logger.warning('URL к черному списку не задан в настройках')
                return False

            try:
                # Заменяем github.com на raw.githubusercontent.com для получения raw содержимого
                if 'github.com' in github_url:
                    raw_url = github_url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
                else:
                    raw_url = github_url

                # Получаем содержимое файла
                async with aiohttp.ClientSession() as session, session.get(raw_url) as response:
                    if response.status != 200:
                        logger.error('Ошибка при получении черного списка: статус', status=response.status)
                        return False

                    content = await response.text()

                # Разбираем содержимое файла
                blacklist_data = []
                lines = content.splitlines()

                for line_num, line in enumerate(lines, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue  # Пропускаем пустые строки и комментарии

                    # В формате '7021477105 # @MAMYT_PAXAL2016, перепродажа подписок'
                    # только первая часть до пробела - это Telegram ID, всё остальное комментарий
                    try:
                        # 1. Разделяем строку на ID и всё остальное по символу '#'
                        if '#' in line:
                            id_part, content_part = line.split('#', 1)
                            telegram_id = int(id_part.strip())
                            content = content_part.strip()
                        else:
                            # Если решётки нет, пробуем просто взять первое число
                            parts = line.split(maxsplit=1)
                            telegram_id = int(parts[0])
                            content = parts[1].strip() if len(parts) > 1 else ''

                        # 2. Обрабатываем контент: вычленяем username, если он есть в начале
                        username = ''
                        reason = 'Занесен в черный список'

                        if content:
                            if content.startswith('@'):
                                # Разбиваем контент только по первому пробелу
                                # content_parts[0] будет юзернеймом, content_parts[1] — причиной
                                content_parts = content.split(maxsplit=1)
                                username = content_parts[0]
                                if len(content_parts) > 1:
                                    reason = content_parts[1].strip()
                            else:
                                # Если собачки нет, значит весь контент — это причина
                                reason = content

                        blacklist_data.append((telegram_id, username, reason))

                    except ValueError:
                        # Если не удается преобразовать в число, это не ID
                        logger.warning(
                            'Неверный формат строки в черном списке первое значение не является числом',
                            line_num=line_num,
                            line=line,
                        )

                self.blacklist_data = blacklist_data
                self.last_update = datetime.now(UTC)
                self._check_cache.clear()
                logger.info('Черный список успешно обновлен. Найдено записей', blacklist_data_count=len(blacklist_data))
                return True

            except ValueError as e:
                logger.error('Ошибка при парсинге ID из черного списка', error=e)
                return False
            except Exception as e:
                logger.error('Ошибка при обновлении черного списка', error=e)
                return False

    async def is_user_blacklisted(self, telegram_id: int, username: str | None = None) -> tuple[bool, str | None]:
        """
        Проверяет, находится ли пользователь в черном списке

        Args:
            telegram_id: Telegram ID пользователя
            username: Username пользователя (опционально)

        Returns:
            Кортеж (в черном списке, причина)
        """
        if not self.is_blacklist_check_enabled():
            return False, None

        # Проверяем кэш
        now = time.monotonic()
        cached = self._check_cache.get(telegram_id)
        if cached is not None:
            is_bl, reason, ts = cached
            if now - ts < self._cache_ttl:
                return is_bl, reason

        # Проверяем, является ли пользователь администратором и нужно ли его игнорировать
        if self.should_ignore_admins() and self.is_admin(telegram_id):
            self._check_cache[telegram_id] = (False, None, now)
            return False, None

        # Если черный список пуст или устарел, обновляем его
        interval_hours = self.get_blacklist_update_interval_hours()
        required_interval = timedelta(hours=interval_hours)
        if not self.blacklist_data or (self.last_update and datetime.now(UTC) - self.last_update > required_interval):
            await self.update_blacklist()

        # Проверяем по Telegram ID
        for bl_id, bl_username, bl_reason in self.blacklist_data:
            if bl_id == telegram_id:
                logger.info('Пользователь найден в черном списке по ID', telegram_id=telegram_id, bl_reason=bl_reason)
                self._check_cache[telegram_id] = (True, bl_reason, now)
                return True, bl_reason

        # Проверяем по username, если он передан
        if username:
            username_lower = username.lower().lstrip('@')
            for bl_id, bl_username, bl_reason in self.blacklist_data:
                if bl_username and bl_username.lower().lstrip('@') == username_lower:
                    logger.info(
                        'Пользователь найден в черном списке по username',
                        username=username,
                        telegram_id=telegram_id,
                        bl_reason=bl_reason,
                    )
                    self._check_cache[telegram_id] = (True, bl_reason, now)
                    return True, bl_reason

        self._check_cache[telegram_id] = (False, None, now)
        return False, None

    async def get_all_blacklisted_users(self) -> list[tuple[int, str, str]]:
        """
        Возвращает весь черный список
        """
        interval_hours = self.get_blacklist_update_interval_hours()
        required_interval = timedelta(hours=interval_hours)
        if not self.blacklist_data or (self.last_update and datetime.now(UTC) - self.last_update > required_interval):
            await self.update_blacklist()

        return self.blacklist_data.copy()

    async def get_user_by_telegram_id(self, telegram_id: int) -> tuple[int, str, str] | None:
        """
        Возвращает информацию о пользователе из черного списка по Telegram ID

        Args:
            telegram_id: Telegram ID пользователя

        Returns:
            Кортеж (telegram_id, username, reason) или None, если не найден
        """
        for bl_id, bl_username, bl_reason in self.blacklist_data:
            if bl_id == telegram_id:
                return (bl_id, bl_username, bl_reason)
        return None

    async def get_user_by_username(self, username: str) -> tuple[int, str, str] | None:
        """
        Возвращает информацию о пользователе из черного списка по username

        Args:
            username: Username пользователя

        Returns:
            Кортеж (telegram_id, username, reason) или None, если не найден
        """
        # Проверяем как с @, так и без
        username_with_at = f'@{username}' if not username.startswith('@') else username
        username_without_at = username.lstrip('@')

        for bl_id, bl_username, bl_reason in self.blacklist_data:
            if bl_username == username_with_at or bl_username.lstrip('@') == username_without_at:
                return (bl_id, bl_username, bl_reason)
        return None

    async def force_update_blacklist(self) -> tuple[bool, str]:
        """
        Принудительно обновляет черный список

        Returns:
            Кортеж (успешно, сообщение)
        """
        success = await self.update_blacklist()
        if success:
            return True, f'Черный список обновлен успешно. Записей: {len(self.blacklist_data)}'
        return False, 'Ошибка обновления черного списка'


# Глобальный экземпляр сервиса
blacklist_service = BlacklistService()
