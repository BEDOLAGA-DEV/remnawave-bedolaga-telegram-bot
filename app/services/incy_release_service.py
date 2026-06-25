"""Resolve INCY desktop installer URLs from the latest incy-platforms release.

Fetches the GitHub "releases/latest" JSON, maps known asset filenames to
platform/arch/pkg keys, and caches the result in memory with a TTL. On a GitHub
error (timeout / rate limit / non-200) the last cached value is returned if
present, else an empty map — never raises to the handler. Pattern mirrors
``app/services/version_service.py``.
"""

import time

import aiohttp
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Exact upstream asset filename -> internal key.
_FILENAME_TO_KEY: dict[str, str] = {
    'incy-windows-setup.exe': 'windows',
    'incy-macos-arm64.dmg': 'macos:arm',
    'incy-macos-intel.dmg': 'macos:intel',
    'incy-linux-arm64.deb': 'linux:arm:deb',
    'incy-linux-arm64.rpm': 'linux:arm:rpm',
    'incy-linux-arm64-portable.zip': 'linux:arm:portable',
    'incy-linux-x64.deb': 'linux:x64:deb',
    'incy-linux-x64.rpm': 'linux:x64:rpm',
    'incy-linux-x64-portable.zip': 'linux:x64:portable',
}

_cache: dict[str, str] | None = None
_cache_ts: float = 0.0


def _reset_cache_for_tests() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


def _expire_cache_for_tests() -> None:
    global _cache_ts
    _cache_ts = 0.0


def _build_asset_map(release_json: dict) -> dict[str, str]:
    assets = (release_json or {}).get('assets') or []
    result: dict[str, str] = {}
    for asset in assets:
        key = _FILENAME_TO_KEY.get(asset.get('name'))
        if key and asset.get('browser_download_url'):
            result[key] = asset['browser_download_url']
    return result


async def _fetch_latest_release_json() -> dict:
    repo = settings.get_incy_platforms_repo()
    url = f'https://api.github.com/repos/{repo}/releases/latest'
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(f'GitHub API status {response.status}')
        return await response.json()


async def get_incy_desktop_assets(force: bool = False) -> dict[str, str]:
    """Return {platform-key: download_url}. Cached for INCY_RELEASE_CACHE_TTL."""
    global _cache, _cache_ts
    ttl = settings.get_incy_release_cache_ttl()
    if not force and _cache is not None and (time.monotonic() - _cache_ts) < ttl:
        return _cache
    try:
        data = await _fetch_latest_release_json()
        _cache = _build_asset_map(data)
        _cache_ts = time.monotonic()
        logger.info('INCY release resolved', tag=data.get('tag_name'), assets=len(_cache))
        return _cache
    except Exception as e:  # noqa: BLE001 - resolver must never crash the handler
        logger.warning('INCY release fetch failed', error=str(e))
        return _cache if _cache is not None else {}
