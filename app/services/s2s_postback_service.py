"""S2S Postback Service — sends server-to-server postbacks on events."""

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

try:
    import httpx
except ImportError:
    httpx = None


def _is_enabled() -> bool:
    return getattr(settings, 'S2S_POSTBACK_ENABLED', False) and httpx is not None


def _get_url(event: str) -> str | None:
    """Get postback URL template for event type."""
    mapping = {
        'registration': getattr(settings, 'S2S_POSTBACK_REGISTRATION_URL', ''),
        'trial': getattr(settings, 'S2S_POSTBACK_TRIAL_URL', ''),
        'purchase': getattr(settings, 'S2S_POSTBACK_PURCHASE_URL', ''),
    }
    url = mapping.get(event, '')
    return url or None


async def send_postback(
    event: str,
    subid: str,
    amount: float | None = None,
    user_id: int | None = None,
) -> bool:
    """Send S2S postback for an event.

    Args:
        event: 'registration', 'trial', or 'purchase'
        subid: tracking subid from URL
        amount: purchase amount in rubles (for purchase event)
        user_id: internal user ID for logging

    Returns:
        True if sent successfully
    """
    if not _is_enabled():
        return False

    if not subid:
        return False

    url_template = _get_url(event)
    if not url_template:
        logger.debug('S2S postback URL not configured', event=event)
        return False

    # Replace placeholders (URL-encode subid to prevent injection)
    from urllib.parse import quote

    url = url_template.replace('{subid}', quote(subid, safe=''))
    url = url.replace('{event}', event)
    if amount is not None:
        url = url.replace('{amount}', str(round(amount, 2)))
    else:
        url = url.replace('{amount}', '0')

    if user_id is not None:
        url = url.replace('{user_id}', str(user_id))

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            logger.info(
                'S2S postback sent',
                event=event,
                subid=subid,
                amount=amount,
                user_id=user_id,
                status_code=response.status_code,
                url=url[:100],
            )
            return response.status_code < 400
    except Exception as e:
        logger.error(
            'S2S postback failed',
            event=event,
            subid=subid,
            error=str(e),
            url=url[:100],
        )
        return False
