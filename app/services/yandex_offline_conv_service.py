"""Yandex.Metrika offline conversions service.

Sends events (registration, trial-add, purchase) to mc.yandex.ru/collect
using the Measurement Protocol. Each event is preceded by a warm-up pageview.
"""

from __future__ import annotations

import asyncio
import re

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

_CID_RE = re.compile(r'^[A-Za-z0-9._:-]{4,64}$')


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
    payload.update({
        't': 'pageview',
        'dl': settings.YANDEX_OFFLINE_CONV_DL or 'https://web.mtrxvps.ru',
        'dt': settings.YANDEX_OFFLINE_CONV_DT or 'Matrixxx VPN',
    })
    return payload


def _event_payload(cid: str, event_action: str) -> dict[str, str]:
    payload = _base_payload(cid)
    payload.update({
        't': 'event',
        'ea': event_action,
        'dl': settings.YANDEX_OFFLINE_CONV_DL or 'https://web.mtrxvps.ru',
    })
    return payload


async def _post_collect(payload: dict[str, str], kind: str, cid: str) -> bool:
    """POST to mc.yandex.ru/collect with retries. Returns True on success."""
    masked = _mask_cid(cid)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(COLLECT_URL, data=payload)

            if 200 <= resp.status_code < 300:
                logger.info('%s %s sent (cid=%s, status=%s)', LOG_PREFIX, kind, masked, resp.status_code)
                return True

            if 500 <= resp.status_code < 600 and attempt < MAX_RETRIES:
                logger.warning(
                    '%s %s server error (attempt %s/%s, cid=%s, status=%s)',
                    LOG_PREFIX, kind, attempt, MAX_RETRIES, masked, resp.status_code,
                )
                await asyncio.sleep(RETRY_DELAY)
                continue

            logger.error(
                '%s %s rejected (cid=%s, status=%s, body=%s)',
                LOG_PREFIX, kind, masked, resp.status_code, resp.text[:200],
            )
            return False

        except Exception as exc:
            logger.warning(
                '%s %s request error (attempt %s/%s, cid=%s): %s',
                LOG_PREFIX, kind, attempt, MAX_RETRIES, masked, exc,
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
        logger.warning('%s Pageview failed for %s, skipping event %s', LOG_PREFIX, _mask_cid(cid), event_action)
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
        await upsert_cid(db, user_id, normalized, source=source,
                         counter_id=settings.YANDEX_OFFLINE_CONV_COUNTER_ID)
        logger.info('%s Stored CID for user_id=%s source=%s', LOG_PREFIX, user_id, source)
        return True
    except Exception as exc:
        logger.error('%s Failed to store CID for user_id=%s: %s', LOG_PREFIX, user_id, exc)
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
            logger.info('%s registration event sent for user_id=%s', LOG_PREFIX, user_id)
    except Exception as exc:
        logger.error('%s registration event failed for user_id=%s: %s', LOG_PREFIX, user_id, exc)


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
            logger.info('%s trial-add event sent for user_id=%s', LOG_PREFIX, user_id)
    except Exception as exc:
        logger.error('%s trial-add event failed for user_id=%s: %s', LOG_PREFIX, user_id, exc)


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
            logger.info(
                '%s purchase event sent for user_id=%s amount=%s',
                LOG_PREFIX, user_id, amount_kopeks / 100,
            )
    except Exception as exc:
        logger.error('%s purchase event failed for user_id=%s: %s', LOG_PREFIX, user_id, exc)


def parse_cid_from_start_param(param: str) -> tuple[str | None, str]:
    """Extract Yandex CID from bot start parameter.

    If param starts with the configured prefix (e.g. 'utm_ya_'),
    returns (cid, remaining_param). Otherwise returns (None, original_param).
    """
    prefix = settings.YANDEX_OFFLINE_CONV_START_PREFIX
    if not prefix or not param.startswith(prefix):
        return None, param

    cid = param[len(prefix):]
    normalized = _normalize_cid(cid)
    return normalized, param  # Keep original param for UTM tracking
