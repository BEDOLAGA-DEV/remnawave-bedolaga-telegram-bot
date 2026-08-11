"""Shared helper for verifying that an HWID belongs to a user's panel account.

Used by the cabinet user-facing rename endpoint and the admin rename
endpoint. Extracted into a single module so the two callers can't drift
again — both had `_verify_hwid_belongs_to_user` near-duplicates with
subtly different fallback logic in the past.

Multi-tariff correctness: a single user can own multiple subscriptions
each pointing to a different RemnaWave panel user (different
`remnawave_id`s). The previous "take the first non-null id"
heuristic produced spurious 404s when the device the user was renaming
was attached to a NON-first subscription. This helper unions device
lists across ALL distinct panel user ids the user holds.
"""

from __future__ import annotations

import structlog

from app.database.models import User
from app.external.remnawave_api import RemnaWaveInvalidUserIdError


logger = structlog.get_logger(__name__)


def _collect_panel_user_ids(user: User) -> list[int]:
    """Return every distinct RemnaWave panel user id the user is attached to.

    Includes the legacy single-tariff `user.remnawave_id` AND each
    multi-tariff subscription's `remnawave_id`. De-duped while
    preserving insertion order so the most-likely-active id is queried
    first.
    """
    seen: dict[int, None] = {}  # ordered set
    panel_user_id = getattr(user, 'remnawave_id', None)
    if panel_user_id:
        seen[panel_user_id] = None
    for sub in getattr(user, 'subscriptions', None) or []:
        sub_panel_user_id = getattr(sub, 'remnawave_id', None)
        if sub_panel_user_id:
            seen.setdefault(sub_panel_user_id, None)
    return list(seen.keys())


async def verify_hwid_belongs_to_user(user: User, hwid: str) -> bool:
    """Best-effort check that `hwid` is on one of the user's RemnaWave panels.

    Multi-tariff aware: queries EVERY distinct panel user id the user owns
    and unions the device sets. Short-circuits on the first match.

    Degrade-open policy: if RemnaWave is unreachable while iterating,
    returns True so renames don't break during transient outages of an
    external dependency. The alias remains user-scoped — there is no
    privacy or authorization concern from accepting a write under
    degraded conditions; at worst we get an orphan alias row.

    Degrade-open НЕ распространяется на непригодный локальный идентификатор
    (`RemnaWaveInvalidUserIdError`): это битая ссылка в нашей же БД, а не сбой
    панели, и запрос до неё вообще не доходит. Принимать по такому поводу любой
    hwid значило бы отключить проверку владения целиком, поэтому такой id
    просто пропускается — проверка остаётся закрытой.

    Returns False only when we successfully fetched ALL the panel's
    device lists and the hwid appeared in none of them.
    """
    from app.services.remnawave_service import RemnaWaveService

    panel_user_ids = _collect_panel_user_ids(user)
    if not panel_user_ids:
        return False

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            for panel_user_id in panel_user_ids:
                try:
                    response = await api.get_user_devices_all(panel_user_id)
                except RemnaWaveInvalidUserIdError as invalid_id_error:
                    logger.warning(
                        'Unusable panel user id during hwid validation, skipping',
                        user_id=getattr(user, 'id', None),
                        error=str(invalid_id_error)[:200],
                    )
                    continue
                hwids_on_panel = {
                    (d.get('hwid') or d.get('deviceId') or d.get('id')) for d in (response or {}).get('devices', [])
                }
                if hwid in hwids_on_panel:
                    return True
            return False
    except Exception as remnawave_error:
        logger.warning(
            'RemnaWave unreachable during hwid validation, degrading open',
            user_id=getattr(user, 'id', None),
            panel_user_id_count=len(panel_user_ids),
            error=str(remnawave_error)[:200],
        )
        return True
