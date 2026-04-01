"""Yandex.Metrika offline conversions service.

Sends events (registration, trial-add, purchase) to mc.yandex.ru/collect
using the Measurement Protocol. No pageview needed — user has active
Metrika session from the site. yclid is passed via landing page URL,
Metrika matches it automatically.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.yandex_client_id import (
    get_cid,
    mark_registration_sent,
    mark_trial_sent,
    upsert_cid,
)
from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

LOG_PREFIX = '[YandexOfflineConv]'
COLLECT_URL = 'https://mc.yandex.ru/collect'
TIMEOUT = 10.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0

_CID_RE = re.compile(r'^[A-Za-z0-9._:-]{4,128}$')
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT)
    return _http_client


def _is_enabled() -> bool:
    return bool(
        settings.YANDEX_OFFLINE_CONV_ENABLED
        and settings.YANDEX_OFFLINE_CONV_COUNTER_ID
        and settings.YANDEX_OFFLINE_CONV_MEASUREMENT_SECRET
    )


def _normalize_cid(cid: str | None) -> str | None:
    if not isinstance(cid, str):
        return None
    cid = cid.strip()
    if not cid or not _CID_RE.match(cid):
        return None
    return cid


def _mask_cid(cid: str) -> str:
    if len(cid) <= 4:
        return '****'
    return '*' * (len(cid) - 4) + cid[-4:]


def _base_payload(cid: str) -> dict[str, str]:
    return {
        'tid': settings.YANDEX_OFFLINE_CONV_COUNTER_ID,
        'cid': cid,
        'ms': settings.YANDEX_OFFLINE_CONV_MEASUREMENT_SECRET,
    }


def _pageview_payload(cid: str) -> dict[str, str]:
    payload = _base_payload(cid)
    payload.update(
        {
            't': 'pageview',
            'dl': settings.YANDEX_OFFLINE_CONV_DL or 'https://web.mtrxvps.ru',
            'dt': settings.YANDEX_OFFLINE_CONV_DT or 'Matrixxx VPN',
        }
    )
    return payload


def _event_payload(cid: str, event_action: str) -> dict[str, str]:
    payload = _base_payload(cid)
    payload.update(
        {
            't': 'event',
            'ea': event_action,
        }
    )
    return payload


def _ecommerce_purchase_payload(
    cid: str,
    amount_rubles: float,
    order_id: str = '',
    product_name: str = '',
    product_category: str = '',
) -> dict[str, str]:
    """Build ecommerce:purchase payload for Metrika Measurement Protocol."""

    service_name = (
        getattr(settings, 'YANDEX_OFFLINE_CONV_DT', '')
        or getattr(settings, 'PAYMENT_SERVICE_NAME', '')
        or 'Subscription'
    )
    currency = getattr(settings, 'YANDEX_OFFLINE_CONV_CURRENCY', '') or 'RUB'
    payload = _base_payload(cid)
    payload.update(
        {
            't': 'event',
            'ea': 'purchase',
            'pa': 'purchase',
            'ti': order_id or str(int(time.time())),
            'tr': str(amount_rubles),
            'cu': currency,
            'ev': str(amount_rubles),
            'pr1id': 'subscription',
            'pr1nm': product_name or service_name,
            'pr1ca': product_category or 'subscription',
            'pr1pr': str(amount_rubles),
            'pr1qt': '1',
        }
    )
    return payload


async def _post_collect(payload: dict[str, str], kind: str, cid: str) -> bool:
    """POST to mc.yandex.ru/collect with retries. Returns True on success."""
    masked = _mask_cid(cid)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _get_client()
            resp = await client.post(COLLECT_URL, data=payload)

            if 200 <= resp.status_code < 300:
                logger.info('collect sent', kind=kind, cid=masked, status=resp.status_code)
                return True

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                logger.warning(
                    'collect server error',
                    kind=kind,
                    attempt=attempt,
                    max=MAX_RETRIES,
                    cid=masked,
                    status=resp.status_code,
                )
                await asyncio.sleep(RETRY_DELAY)
                continue

            logger.error('collect rejected', kind=kind, cid=masked, status=resp.status_code, body=resp.text[:200])
            return False

        except Exception as exc:
            logger.warning(
                'collect request error', kind=kind, attempt=attempt, max=MAX_RETRIES, cid=masked, error=str(exc)
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return False

    return False


async def _send_event(cid: str, event_action: str) -> bool:
    """Send event directly — no pageview needed, user has active Metrika session."""
    return await _post_collect(_event_payload(cid, event_action), event_action, cid)


# --- Background task helpers ---

_background_tasks: set[asyncio.Task] = set()


def _task_done(task):
    """Log errors from background conversion tasks."""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f'{LOG_PREFIX} Background task failed', error=str(exc))


def spawn_bg(coro) -> None:
    """Spawn a background Yandex conversion task with proper reference tracking.

    Checks _is_enabled() early so callers don't need to.
    """
    if not _is_enabled():
        # Close the coroutine to avoid RuntimeWarning
        coro.close()
        return
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_task_done)


async def _fire_bg(event_name: str, event_fn, user_id: int, **kwargs) -> None:
    """Generic background wrapper: opens a session, calls event_fn, logs errors."""
    try:
        async with AsyncSessionLocal() as db:
            await event_fn(db, user_id, **kwargs)
    except Exception as exc:
        logger.warning(f'{LOG_PREFIX} Background {event_name} event failed', user_id=user_id, error=str(exc))


async def fire_registration_bg(user_id: int) -> None:
    """Fire registration event in background with its own DB session."""
    await _fire_bg('registration', on_registration, user_id)


async def fire_trial_bg(user_id: int) -> None:
    """Fire trial event in background with its own DB session."""
    await _fire_bg('trial', on_trial, user_id)


async def fire_purchase_bg(user_id: int, amount_kopeks: int) -> None:
    """Fire purchase event in background with its own DB session."""
    await _fire_bg('purchase', on_purchase, user_id, amount_kopeks=amount_kopeks)


# --- Public API ---
async def store_cid(
    db: AsyncSession,
    user_id: int,
    cid: str | None,
    source: str = 'web',
) -> bool:
    """Store Yandex ClientID for a user. Returns True if stored."""
    normalized = _normalize_cid(cid)
    if not normalized:
        return False

    try:
        await upsert_cid(db, user_id, normalized, source=source, counter_id=settings.YANDEX_OFFLINE_CONV_COUNTER_ID)
        logger.info('stored CID', user_id=user_id, source=source)
        return True
    except Exception as exc:
        logger.error('failed to store CID', user_id=user_id, error=str(exc))
        return False


async def store_cid_and_fire_registration(
    user_id: int,
    cid: str | None,
    *,
    source: str = 'web',
) -> None:
    """Store Yandex CID and fire registration conversion in background (best-effort).

    Opens its own DB session so it never interferes with the caller's transaction.
    """
    if not cid:
        return
    try:
        async with AsyncSessionLocal() as db:
            stored = await store_cid(db, user_id, cid, source=source)
            if stored:
                await db.commit()
                spawn_bg(fire_registration_bg(user_id))
    except Exception as exc:
        logger.warning(f'{LOG_PREFIX} Failed to store CID and fire registration', user_id=user_id, error=str(exc))


async def on_registration(db: AsyncSession, user_id: int) -> None:
    """Fire registration event (once per user)."""
    if not _is_enabled():
        return

    try:
        row = await get_cid(db, user_id)
        if not row or row.registration_sent:
            return

        success = await _send_event(row.yandex_cid, 'registration')
        if success:
            await mark_registration_sent(db, user_id)
            await db.commit()
            logger.info('registration event sent', user_id=user_id)
    except Exception as exc:
        logger.error('registration event failed', user_id=user_id, error=str(exc))


async def on_trial(db: AsyncSession, user_id: int) -> None:
    """Fire trial-add event (once per user)."""
    if not _is_enabled():
        return

    try:
        row = await get_cid(db, user_id)
        if not row or row.trial_sent:
            return

        success = await _send_event(row.yandex_cid, 'trial-add')
        if success:
            await mark_trial_sent(db, user_id)
            await db.commit()
            logger.info('trial-add event sent', user_id=user_id)
    except Exception as exc:
        logger.error('trial-add event failed', user_id=user_id, error=str(exc))


async def on_purchase(db: AsyncSession, user_id: int, amount_kopeks: int) -> None:
    """Fire ecommerce purchase event (every payment)."""
    if not _is_enabled():
        return

    try:
        row = await get_cid(db, user_id)
        if not row:
            return

        amount_rubles = amount_kopeks / 100
        payload = _ecommerce_purchase_payload(row.yandex_cid, amount_rubles)
        success = await _post_collect(payload, 'purchase', row.yandex_cid)
        if success:
            logger.info('purchase event sent', user_id=user_id, amount=amount_rubles)
    except Exception as exc:
        logger.error('purchase event failed', user_id=user_id, error=str(exc))


def parse_cid_from_start_param(param: str) -> tuple[str | None, str]:
    """Extract Yandex CID from bot start parameter.

    If param starts with the configured prefix (e.g. 'utm_ya_'),
    returns (cid, original_param). Otherwise returns (None, original_param).
    Original param is always preserved for UTM tracking.
    """
    prefix = settings.YANDEX_OFFLINE_CONV_START_PREFIX
    if not prefix or not param.startswith(prefix):
        return None, param

    cid = param[len(prefix) :]
    normalized = _normalize_cid(cid)
    return normalized, param  # Keep original param for UTM tracking
