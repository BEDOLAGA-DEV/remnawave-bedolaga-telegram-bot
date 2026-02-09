import asyncio
import logging
import re
from datetime import datetime, timedelta

import aiohttp
from packaging import version

from app.config import settings


logger = logging.getLogger(__name__)


class VersionInfo:
    def __init__(self, tag_name: str, published_at: str, name: str, body: str, prerelease: bool = False):
        self.tag_name = tag_name
        self.published_at = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        self.name = name or tag_name
        self.body = body
        self.prerelease = prerelease
        self.is_dev = 'dev' in tag_name.lower()

    @property
    def clean_version(self) -> str:
        return re.sub(r'^v', '', self.tag_name)

    @property
    def version_obj(self):
        try:
            clean_ver = self.clean_version
            if 'dev' in clean_ver:
                base_ver = clean_ver.split('-dev')[0]
                return version.parse(f'{base_ver}.dev')
            return version.parse(clean_ver)
        except Exception:
            return version.parse('0.0.0')

    @property
    def formatted_date(self) -> str:
        return self.published_at.strftime('%d.%m.%Y %H:%M')

    @property
    def short_description(self) -> str:
        if not self.body:
            return 'Без описания'

        description = self.body.strip()
        if len(description) > 350:
            description = description[:347] + '...'

        return description

    @property
    def full_description(self) -> str:
        """Full release body text, stripped of leading/trailing whitespace."""
        if not self.body:
            return ''
        return self.body.strip()


class VersionService:
    def __init__(self, bot=None):
        self.bot = bot
        self.repo = getattr(settings, 'VERSION_CHECK_REPO', 'fr1ngg/remnawave-bedolaga-telegram-bot')
        self.enabled = getattr(settings, 'VERSION_CHECK_ENABLED', True)
        self.current_version = self._get_current_version()
        self.cache_ttl = 3600
        self._cache: dict = {}
        self._last_check: datetime | None = None
        self._notification_service = None

    async def get_latest_stable_version(self) -> str:
        try:
            url = f'https://api.github.com/repos/{self.repo}/releases/latest'
            async with aiohttp.ClientSession() as session, session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['tag_name']
        except Exception:
            pass
        return 'UNKNOW'

    def _get_current_version(self) -> str:
        try:
            from pathlib import Path

            pyproject_path = Path(__file__).resolve().parents[2] / 'pyproject.toml'
            if pyproject_path.exists():
                for line in pyproject_path.read_text().splitlines():
                    if line.strip().startswith('version'):
                        ver = line.split('=', 1)[1].strip().strip('"').strip("'")
                        if ver:
                            return ver
        except Exception:
            pass

        return 'UNKNOW'

    def set_notification_service(self, notification_service):
        self._notification_service = notification_service

    async def check_for_updates(self, force: bool = False) -> tuple[bool, list[VersionInfo]]:
        if not self.enabled:
            return False, []

        try:
            releases = await self._fetch_releases(force)
            if not releases:
                return False, []

            current_ver = self._parse_version(self.current_version)
            newer_releases = []

            for release in releases:
                release_ver = release.version_obj
                if release_ver > current_ver:
                    newer_releases.append(release)

            newer_releases.sort(key=lambda x: x.version_obj, reverse=True)

            has_updates = len(newer_releases) > 0

            if has_updates and not force:
                await self._send_update_notification(newer_releases)

            return has_updates, newer_releases

        except Exception as e:
            logger.error(f'Ошибка проверки обновлений: {e}')
            return False, []

    async def _fetch_releases(self, force: bool = False) -> list[VersionInfo]:
        if not force and self._cache and self._last_check:
            if datetime.now() - self._last_check < timedelta(seconds=self.cache_ttl):
                return self._cache.get('releases', [])

        url = f'https://api.github.com/repos/{self.repo}/releases'

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    releases = []

                    for release_data in data[:20]:
                        release = VersionInfo(
                            tag_name=release_data['tag_name'],
                            published_at=release_data['published_at'],
                            name=release_data['name'],
                            body=release_data['body'] or '',
                            prerelease=release_data['prerelease'],
                        )
                        releases.append(release)

                    self._cache['releases'] = releases
                    self._last_check = datetime.now()

                    logger.info(f'Получено {len(releases)} релизов из GitHub')
                    return releases
                logger.warning(f'GitHub API вернул статус {response.status}')
                return []

        except TimeoutError:
            logger.warning('Таймаут при запросе к GitHub API')
            return []
        except Exception as e:
            logger.error(f'Ошибка запроса к GitHub API: {e}')
            return []

    def _parse_version(self, version_str: str):
        try:
            clean_ver = re.sub(r'^v', '', version_str)
            if 'dev' in clean_ver:
                base_ver = clean_ver.split('-dev')[0]
                return version.parse(f'{base_ver}.dev')
            if 'unknow' in clean_ver.lower():
                return version.parse('0.0.0')
            return version.parse(clean_ver)
        except Exception:
            return version.parse('0.0.0')

    async def _send_update_notification(self, newer_releases: list[VersionInfo]):
        if not self._notification_service or not newer_releases:
            return

        try:
            latest_version = newer_releases[0]
            cache_key = f'notified_{latest_version.tag_name}'

            if self._cache.get(cache_key):
                return

            await self._notification_service.send_version_update_notification(
                current_version=self.current_version, latest_version=latest_version, total_updates=len(newer_releases)
            )

            self._cache[cache_key] = True

        except Exception as e:
            logger.error(f'Ошибка отправки уведомления об обновлении: {e}')

    async def get_version_info(self) -> dict:
        try:
            has_updates, newer_releases = await self.check_for_updates()
            all_releases = await self._fetch_releases()

            current_release = None
            current_ver = self._parse_version(self.current_version)

            for release in all_releases:
                if release.version_obj == current_ver:
                    current_release = release
                    break

            return {
                'current_version': self.current_version,
                'current_release': current_release,
                'has_updates': has_updates,
                'newer_releases': newer_releases[:5],
                'total_newer': len(newer_releases),
                'last_check': self._last_check,
                'repo_url': f'https://github.com/{self.repo}',
            }

        except Exception as e:
            logger.error(f'Ошибка получения информации о версиях: {e}')
            return {
                'current_version': self.current_version,
                'current_release': None,
                'has_updates': False,
                'newer_releases': [],
                'total_newer': 0,
                'last_check': None,
                'repo_url': f'https://github.com/{self.repo}',
                'error': str(e),
            }

    async def start_periodic_check(self):
        if not self.enabled:
            logger.info('Проверка версий отключена')
            return

        logger.info(f'Запуск периодической проверки обновлений для {self.repo}')
        logger.info(f'Текущая версия: {self.current_version}')

        while True:
            try:
                await asyncio.sleep(3600)
                await self.check_for_updates()

            except asyncio.CancelledError:
                logger.info('Остановка проверки обновлений')
                break
            except Exception as e:
                logger.error(f'Ошибка в периодической проверке обновлений: {e}')
                await asyncio.sleep(300)

    def format_version_display(self, version_info: VersionInfo) -> str:
        status_icon = ''
        if version_info.prerelease:
            status_icon = '🧪'
        elif version_info.is_dev:
            status_icon = '🔧'
        else:
            status_icon = '📦'

        return f'{status_icon} {version_info.tag_name}'


version_service = VersionService()
