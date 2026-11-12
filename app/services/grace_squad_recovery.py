"""Recover only catalog-proven deletion of a grace squad, never manual revocation."""

from dataclasses import replace
from datetime import UTC, datetime

from app.services.grace_access_service import (
    GraceSessionState,
    GraceSquadReplacementRequired,
    panel_is_safe_pending_source,
    panel_matches_overlay,
)


async def _catalog(api):
    squads = await api.get_internal_squads()
    if not isinstance(squads, list) or any(
        not isinstance(getattr(squad, 'uuid', None), str) or not squad.uuid for squad in squads
    ):
        raise ValueError('Invalid internal squad catalog; grace recovery refused')
    return {squad.uuid for squad in squads}


async def repair_deleted_grace_squad(api, session, replacement):
    from app.services.grace_access_runtime import GracePanelError, _panel_user_to_snapshot
    from app.services.panel_sync.writer import patch_panel_squads

    original = session.overlay
    available = await _catalog(api)
    # A mere operator preference change does not retarget an existing incident.
    if not original.squad_uuids or set(original.squad_uuids) <= available:
        return original
    current_user = await api.get_user_by_id(session.remnawave_id)
    if current_user is None:
        return original
    current = _panel_user_to_snapshot(current_user)
    now = datetime.now(UTC)
    stripped = replace(original, squad_uuids=tuple(s for s in original.squad_uuids if s in available))
    candidate = replace(original, squad_uuids=(replacement,))
    candidate_applied = panel_matches_overlay(current, candidate, now=now)
    deletion_only = panel_matches_overlay(current, stripped, now=now)
    original_source = session.state is GraceSessionState.PENDING and panel_is_safe_pending_source(
        current, session.panel_before, original
    )
    if not (candidate_applied or deletion_only or original_source):
        return original  # The ordinary conservative conflict handler owns this state.
    if len(original.squad_uuids) != 1 or not replacement or replacement not in available:
        raise GraceSquadReplacementRequired(
            'Grace squad was deleted. Select an existing replacement in Grace settings; '
            'the subscription and grace deadline have not been changed.'
        )
    if candidate_applied:
        return candidate  # A previous PATCH succeeded but its reply/DB commit was lost.
    if deletion_only:
        # Only routing changes: no status, expiry, quota or reset, even for
        # sessions originally created with RESET_TRAFFIC_ON_START enabled.
        updated = await patch_panel_squads(
            api,
            user_id=session.remnawave_id,
            squads=[replacement],
            external_squad_uuid=original.external_squad_uuid,
        )
        if updated is None or not panel_matches_overlay(_panel_user_to_snapshot(updated), candidate, now=now):
            raise GracePanelError('Panel did not confirm replacement grace squad')
    # PENDING at the original source is activated by the normal guarded path.
    return candidate


async def overlay_after_confirmed_deletion(api, current, overlay, *, now, replacement=None):
    """Allow teardown from deletion-only state, without assigning replacement access."""
    missing = set(overlay.squad_uuids) - set(current.squad_uuids)
    if not missing:
        return overlay
    deletion_only = set(current.squad_uuids) <= set(overlay.squad_uuids)
    replacement_applied = replacement and current.squad_uuids == (replacement,)
    if not deletion_only and not replacement_applied:
        return overlay
    stripped = replace(overlay, squad_uuids=current.squad_uuids)
    if not panel_matches_overlay(current, stripped, now=now):
        return overlay
    available = await _catalog(api)
    if missing & available or (replacement_applied and replacement not in available):
        return overlay  # Manual removal of an existing squad is not deletion.
    return stripped
