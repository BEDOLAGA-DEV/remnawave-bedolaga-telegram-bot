"""S2S Postback Service — sends server-to-server postbacks on events."""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import quote, urlparse

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

try:
    import httpx
except ImportError:
    httpx = None


# Bounded concurrency so a slow partner endpoint cannot exhaust the event loop
# during a burst of deposits. 20 is enough headroom for normal throughput.
_postback_sem: asyncio.Semaphore | None = None
_http_client: httpx.AsyncClient | None = None


def _get_sem() -> asyncio.Semaphore:
    global _postback_sem
    if _postback_sem is None:
        _postback_sem = asyncio.Semaphore(20)
    return _postback_sem


def _get_client() -> httpx.AsyncClient:
    """Lazy-init a process-wide AsyncClient to amortize TLS handshake across postbacks."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
            follow_redirects=False,
            http2=False,
        )
    return _http_client


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


def _host_is_private(hostname: str) -> bool:
    """Reject loopback/link-local/private/reserved hosts to prevent SSRF.

    Catches `127.0.0.1`, `10/8`, `172.16/12`, `192.168/16`, `169.254/16`
    (AWS IMDS), `fc00::/7`, `fe80::/10`, `::1`, plus literal localhost names
    and `.internal` / `.local` DNS suffixes.
    """
    if not hostname:
        return True
    host = hostname.strip().lower()
    if host in {'localhost', 'ip6-localhost', 'ip6-loopback'}:
        return True
    if host.endswith('.local') or host.endswith('.internal') or host.endswith('.localhost'):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _is_safe_url(url: str) -> bool:
    """Allow only http(s) URLs pointing at a public, resolvable host."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {'http', 'https'}:
        return False
    if not parsed.hostname:
        return False
    return not _host_is_private(parsed.hostname)


def _mask_subid(subid: str | None) -> str:
    """Return a non-sensitive prefix for log lines (PII protection)."""
    if not subid:
        return ''
    return subid[:8] + ('…' if len(subid) > 8 else '')


async def send_postback(
    event: str,
    subid: str,
    amount: float | None = None,
    user_id: int | None = None,
    tx_id: str | None = None,
) -> bool:
    """Send S2S postback for an event.

    Args:
        event: 'registration', 'trial', or 'purchase'
        subid: tracking subid from URL
        amount: purchase amount in rubles (for purchase event)
        user_id: internal user ID for logging
        tx_id: idempotency hint for the partner tracker

    Returns:
        True if sent successfully
    """
    if not _is_enabled():
        return False

    if not subid:
        return False

    url_template = _get_url(event)
    if not url_template:
        logger.debug('s2s_postback_url_not_configured', event_type=event)
        return False

    url = url_template.replace('{subid}', quote(subid, safe=''))
    url = url.replace('{event}', event)
    if amount is not None:
        url = url.replace('{amount}', str(round(amount, 2)))
    else:
        url = url.replace('{amount}', '0')

    url = url.replace('{user_id}', str(user_id) if user_id is not None else '0')
    url = url.replace('{tx_id}', quote(tx_id, safe='') if tx_id else '')

    if not _is_safe_url(url):
        logger.warning(
            's2s_postback_url_rejected',
            event_type=event,
            user_id=user_id,
            url_host=urlparse(url).hostname,
        )
        return False

    try:
        async with _get_sem():
            response = await _get_client().get(url)
        logger.info(
            's2s_postback_sent',
            event_type=event,
            subid_prefix=_mask_subid(subid),
            amount=amount,
            user_id=user_id,
            status_code=response.status_code,
            url_host=urlparse(url).hostname,
        )
        return response.status_code < 400
    except Exception as e:
        logger.error(
            's2s_postback_failed',
            event_type=event,
            subid_prefix=_mask_subid(subid),
            user_id=user_id,
            error=str(e),
            url_host=urlparse(url).hostname,
        )
        return False
