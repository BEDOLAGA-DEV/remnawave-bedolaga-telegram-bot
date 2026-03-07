"""Yandex.Metrika offline conversions service.

Sends events (registration, trial-add, purchase) to mc.yandex.ru/collect
using the Measurement Protocol. Each event is preceded by a warm-up pageview.
"""

from __future__ import annotations

import asyncio

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


logger = structlog.get_logger(__name__)

LOG_PREFIX = '[YandexOfflineConv]'
COLLECT_URL = 'https://mc.yandex.ru/collect'
TIMEOUT = 10.0
MAX_RETRIES = 3
RETRY_DELAY = 1.0

_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create a reusable httpx client."""
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
    if not cid:
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
            'dl': settings.YANDEX_OFFLINE_CONV_DL or 'https://web.mtrxvps.ru',
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
                logger.info(f'{LOG_PREFIX} {kind} sent', cid=masked, status=resp.status_code)
                return True

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                logger.warning(
                    f'{LOG_PREFIX} {kind} server error',
                    attempt=f'{attempt}/{MAX_RETRIES}',
                    cid=masked,
                    status=resp.status_code,
                )
                await asyncio.sleep(RETRY_DELAY)
                continue

            logger.error(
                f'{LOG_PREFIX} {kind} rejected',
                cid=masked,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False

        except Exception as exc:
            logger.warning(
                f'{LOG_PREFIX} {kind} request error',
                attempt=f'{attempt}/{MAX_RETRIES}',
                cid=masked,
                error=str(exc),
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return False

    return False


async def _send_event(cid: str, event_action: str) -> bool:
    """Send a warm-up pageview followed by the actual event."""
    # Warm-up pageview (required by Metrika to associate the CID)
    pv_ok = await _post_collect(_pageview_payload(cid), 'pageview', cid)
    if not pv_ok:
        logger.warning(f'{LOG_PREFIX} Pageview failed, skipping event', cid=_mask_cid(cid), event=event_action)
        return False

    return await _post_collect(_event_payload(cid, event_action), event_action, cid)


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
        logger.info(f'{LOG_PREFIX} Stored CID', user_id=user_id, source=source)
        return True
    except Exception as exc:
        logger.error(f'{LOG_PREFIX} Failed to store CID', user_id=user_id, error=str(exc))
        return False


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
            logger.info(f'{LOG_PREFIX} registration event sent', user_id=user_id)
    except Exception as exc:
        logger.error(f'{LOG_PREFIX} registration event failed', user_id=user_id, error=str(exc))


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
            logger.info(f'{LOG_PREFIX} trial-add event sent', user_id=user_id)
    except Exception as exc:
        logger.error(f'{LOG_PREFIX} trial-add event failed', user_id=user_id, error=str(exc))


async def on_purchase(db: AsyncSession, user_id: int, amount_kopeks: int) -> None:
    """Fire purchase event (every payment)."""
    if not _is_enabled():
        return

    try:
        row = await get_cid(db, user_id)
        if not row:
            return

        success = await _send_event(row.yandex_cid, 'purchase')
        if success:
            logger.info(f'{LOG_PREFIX} purchase event sent', user_id=user_id, amount=amount_kopeks / 100)
    except Exception as exc:
        logger.error(f'{LOG_PREFIX} purchase event failed', user_id=user_id, error=str(exc))


def parse_cid_from_start_param(param: str) -> tuple[str | None, str]:
    """Extract Yandex CID from bot start parameter.

    If param starts with the configured prefix (e.g. 'utm_ya_'),
    returns (cid, remaining_param). Otherwise returns (None, original_param).
    """
    prefix = settings.YANDEX_OFFLINE_CONV_START_PREFIX
    if not prefix or not param.startswith(prefix):
        return None, param

    cid = param[len(prefix) :]
    normalized = _normalize_cid(cid)
    return normalized, param  # Keep original param for UTM tracking
