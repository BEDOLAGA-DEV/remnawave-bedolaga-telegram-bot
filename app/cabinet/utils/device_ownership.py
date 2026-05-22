"""Shared helper for verifying that an HWID belongs to a user's panel account.

Used by the cabinet user-facing rename endpoint and the admin rename
endpoint. Extracted into a single module so the two callers can't drift
again — both had `_verify_hwid_belongs_to_user` near-duplicates with
subtly different fallback logic in the past.

Multi-tariff correctness: a single user can own multiple subscriptions
each pointing to a different RemnaWave panel user (different
`remnawave_uuid`s). The previous "take the first non-null uuid"
heuristic produced spurious 404s when the device the user was renaming
was attached to a NON-first subscription. This helper unions device
lists across ALL distinct panel UUIDs the user holds.
"""

from __future__ import annotations

import structlog

from app.database.models import User


logger = structlog.get_logger(__name__)


def _collect_panel_uuids(user: User) -> list[str]:
    """Return every distinct RemnaWave panel UUID the user is attached to.

    Includes the legacy single-tariff `user.remnawave_uuid` AND each
    multi-tariff subscription's `remnawave_uuid`. De-duped while
    preserving insertion order so the most-likely-active UUID is queried
    first.
    """
    seen: dict[str, None] = {}  # ordered set
    uuid = getattr(user, 'remnawave_uuid', None)
    if uuid:
        seen[uuid] = None
    for sub in getattr(user, 'subscriptions', None) or []:
        sub_uuid = getattr(sub, 'remnawave_uuid', None)
        if sub_uuid:
            seen.setdefault(sub_uuid, None)
    return list(seen.keys())


async def verify_hwid_belongs_to_user(user: User, hwid: str) -> bool:
    """Best-effort check that `hwid` is on one of the user's RemnaWave panels.

    Multi-tariff aware: queries EVERY distinct panel UUID the user owns
    and unions the device sets. Short-circuits on the first match.

    Degrade-open policy: if RemnaWave is unreachable while iterating,
    returns True so renames don't break during transient outages of an
    external dependency. The alias remains user-scoped — there is no
    privacy or authorization concern from accepting a write under
    degraded conditions; at worst we get an orphan alias row.

    Returns False only when we successfully fetched ALL the panel's
    device lists and the hwid appeared in none of them.
    """
    from app.services.remnawave_service import RemnaWaveService

    panel_uuids = _collect_panel_uuids(user)
    if not panel_uuids:
        return False

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            for panel_uuid in panel_uuids:
                response = await api.get_user_devices_all(panel_uuid)
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
            panel_uuid_count=len(panel_uuids),
            error=str(remnawave_error)[:200],
        )
        return True
