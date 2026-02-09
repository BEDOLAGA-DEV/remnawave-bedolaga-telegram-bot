"""Admin routes for version and release information."""

import logging
from datetime import datetime, timedelta

import aiohttp
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.database.models import User
from app.services.version_service import version_service

from ..dependencies import get_current_admin_user


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/updates', tags=['Cabinet Admin Updates'])


# ============ Schemas ============


class ReleaseItem(BaseModel):
    tag_name: str
    name: str
    body: str
    published_at: str
    prerelease: bool


class ProjectReleasesInfo(BaseModel):
    current_version: str
    has_updates: bool
    releases: list[ReleaseItem]
    repo_url: str


class ReleasesResponse(BaseModel):
    bot: ProjectReleasesInfo
    cabinet: ProjectReleasesInfo


# ============ Cabinet releases cache ============

CABINET_REPO = 'BEDOLAGA-DEV/bedolaga-cabinet'
_cabinet_cache: dict = {}
_cabinet_last_check: datetime | None = None
_CACHE_TTL = 3600


async def _fetch_cabinet_releases(force: bool = False) -> list[dict]:
    global _cabinet_last_check

    if not force and _cabinet_cache.get('releases') and _cabinet_last_check:
        if datetime.now() - _cabinet_last_check < timedelta(seconds=_CACHE_TTL):
            return _cabinet_cache['releases']

    url = f'https://api.github.com/repos/{CABINET_REPO}/releases'

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                releases = []
                for item in data[:20]:
                    releases.append(
                        {
                            'tag_name': item['tag_name'],
                            'name': item.get('name') or item['tag_name'],
                            'body': item.get('body') or '',
                            'published_at': item['published_at'],
                            'prerelease': item.get('prerelease', False),
                        }
                    )
                _cabinet_cache['releases'] = releases
                _cabinet_last_check = datetime.now()
                logger.info('Fetched %d cabinet releases from GitHub', len(releases))
                return releases
            logger.warning('GitHub API returned status %d for cabinet releases', response.status)
            return _cabinet_cache.get('releases', [])
    except TimeoutError:
        logger.warning('Timeout fetching cabinet releases from GitHub')
        return _cabinet_cache.get('releases', [])
    except Exception as e:
        logger.error('Error fetching cabinet releases: %s', e)
        return _cabinet_cache.get('releases', [])


# ============ Routes ============


@router.get('/releases', response_model=ReleasesResponse)
async def get_releases(
    current_user: User = Depends(get_current_admin_user),
) -> ReleasesResponse:
    """Get release information for bot and cabinet."""
    # Bot releases
    bot_releases_raw = await version_service._fetch_releases()
    has_updates, _ = await version_service.check_for_updates()

    bot_releases = [
        ReleaseItem(
            tag_name=r.tag_name,
            name=r.name,
            body=r.full_description,
            published_at=r.published_at.isoformat(),
            prerelease=r.prerelease,
        )
        for r in bot_releases_raw[:10]
    ]

    bot_info = ProjectReleasesInfo(
        current_version=version_service.current_version,
        has_updates=has_updates,
        releases=bot_releases,
        repo_url=f'https://github.com/{version_service.repo}',
    )

    # Cabinet releases
    cabinet_releases_raw = await _fetch_cabinet_releases()
    cabinet_releases = [ReleaseItem(**r) for r in cabinet_releases_raw[:10]]

    # Current version = latest non-prerelease tag
    cabinet_current = ''
    for r in cabinet_releases_raw:
        if not r.get('prerelease', False):
            cabinet_current = r['tag_name']
            break

    cabinet_info = ProjectReleasesInfo(
        current_version=cabinet_current,
        has_updates=False,
        releases=cabinet_releases,
        repo_url=f'https://github.com/{CABINET_REPO}',
    )

    return ReleasesResponse(bot=bot_info, cabinet=cabinet_info)
