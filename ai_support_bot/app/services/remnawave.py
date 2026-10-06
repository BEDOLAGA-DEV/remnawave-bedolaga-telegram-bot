import aiohttp
import structlog

from ai_support_bot.app.core.config import settings


logger = structlog.get_logger(__name__)


def _parse_stats(user: dict) -> dict:
    used = user.get('usedTrafficBytes', 0) or 0
    limit = user.get('trafficLimitBytes', 0) or 0
    return {
        'used_traffic_gb': used / (1024**3),
        'traffic_limit_gb': limit / (1024**3) if limit > 0 else 0,
        'status': user.get('status'),
        'expire_at': user.get('expireAt'),
    }


async def get_remnawave_stats(telegram_id: int | None, remnawave_uuid: str | None) -> dict | None:
    if not settings.remnawave_enabled:
        return None

    base_url = settings.REMNAWAVE_API_URL.rstrip('/')
    headers = {'Authorization': f'Bearer {settings.REMNAWAVE_API_TOKEN}'}
    timeout = aiohttp.ClientTimeout(total=15)

    endpoints: list[str] = []
    if telegram_id:
        endpoints.append(f'{base_url}/api/users/by-telegram-id/{telegram_id}')
    if remnawave_uuid:
        endpoints.append(f'{base_url}/api/users/{remnawave_uuid}')

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in endpoints:
                try:
                    async with session.get(url, headers=headers) as response:
                        if response.status != 200:
                            continue
                        body = await response.json()
                        payload = body.get('response', body)
                        if isinstance(payload, list):
                            if not payload:
                                continue
                            payload = payload[0]
                        if isinstance(payload, dict):
                            return _parse_stats(payload)
                except aiohttp.ClientError:
                    continue
    except Exception as error:
        logger.warning('Remnawave request failed', error=str(error))
    return None
