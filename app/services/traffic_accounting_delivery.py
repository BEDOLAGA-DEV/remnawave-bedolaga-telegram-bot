"""Post-commit projection of durable quota revisions; never replays payments."""

from dataclasses import replace
from datetime import UTC, datetime

import structlog

from app.config import settings
from app.services.traffic_accounting import AccountingConflict
from app.services.traffic_accounting_service import (
    _fresh_panel,
    lock_accounting_subscription,
    reconcile_account_locked,
)
from app.services.traffic_accounting_store import load_account


logger = structlog.get_logger(__name__)


async def deliver_account_projection(session_factory, api, subscription_id, *, now=None):
    """Commit fresh usage and disable ownership before any external PATCH.

    A second locked transaction coalesces newer grants. If that last GET newly
    exhausts quota, persist its disable intent first and repeat the delivery.
    Lost responses leave the committed revision pending for the recovery worker.
    """
    moment = now or datetime.now(UTC)
    async with session_factory() as db, db.begin():
        subscription, _ = await lock_accounting_subscription(db, subscription_id)
        loaded = await load_account(db, subscription_id)
        if loaded is None or loaded[1].phase not in {'ready', 'unlimited'}:
            raise AccountingConflict('Accounting is not ready for projection')
        panel = await _fresh_panel(db, api, subscription)
        from app.services.traffic_base_grant_service import apply_pending_base_grants_locked

        account = await apply_pending_base_grants_locked(db, subscription, panel, now=moment)
        _, state = await load_account(db, subscription_id)
        if state.phase == 'ready' and not state.grace_excluded:
            observed_status = getattr(getattr(panel, 'status', None), 'value', getattr(panel, 'status', None))
            if observed_status == 'DISABLED' and not state.quota_disabled and not state.activation_requested:
                subscription.status = 'disabled'
            if account.remaining_bytes(moment) == 0 and subscription.status != 'disabled':
                state.quota_disabled = True
                if subscription.status in {'active', 'trial'}:
                    subscription.status = 'limited'
                    subscription.grace_candidate_reason = 'limited'
                    subscription.grace_candidate_at = moment

    for _attempt in range(2):
        async with session_factory() as db, db.begin():
            subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
            loaded = await load_account(db, subscription_id)
            if loaded is None or loaded[1].phase not in {'ready', 'unlimited'}:
                raise AccountingConflict('Accounting changed before projection')
            panel = await _fresh_panel(db, api, subscription)
            account = await reconcile_account_locked(db, subscription, panel, now=moment)
            _, state = await load_account(db, subscription_id)
            if grace_open or state.grace_excluded:
                from app.services.grace_access_runtime import apply_recovered_grace_update_locked

                completed, restored = await apply_recovered_grace_update_locked(
                    db, api, subscription_id, update_kwargs={}, source='traffic_accounting_delivery'
                )
                if completed:
                    return restored
                # Grace exit explicitly queues the latest paid projection.
                return panel
            unlimited = state.phase == 'unlimited'
            empty = not unlimited and account.remaining_bytes(moment) == 0
            if empty and not state.quota_disabled and subscription.status != 'disabled':
                state.quota_disabled = True
                if subscription.status in {'active', 'trial'}:
                    subscription.status = 'limited'
                    subscription.grace_candidate_reason = 'limited'
                    subscription.grace_candidate_at = moment
                continue
            from app.external.remnawave_api import UserStatus
            from app.services.panel_sync.payload import build_panel_payload, resolve_panel_status
            from app.services.panel_sync.writer import write_managed_payload_locked

            panel_status = getattr(getattr(panel, 'status', None), 'value', getattr(panel, 'status', None))
            if panel_status == 'DISABLED' and not state.quota_disabled and not state.activation_requested:
                # A disable not owned by quota is an administrative action.
                subscription.status = 'disabled'
            if (
                state.quota_disabled
                and not empty
                and subscription.status == 'limited'
                and subscription.user.status == 'active'
                and subscription.end_date > moment
            ):
                subscription.status = 'active'
                subscription.grace_candidate_reason = None
                subscription.grace_candidate_at = None
            payload = build_panel_payload(
                subscription.user,
                subscription,
                multi_tariff=settings.is_multi_tariff_enabled(),
                now=moment,
                accounting_account=account,
                user_tag=state.pending_user_tag,
            )
            if unlimited:
                payload = replace(
                    payload,
                    traffic_limit_bytes=0,
                    status=resolve_panel_status(subscription.user, subscription, is_live=payload.is_live, now=moment),
                )
            from app.services.panel_sync.expiry_intent import acknowledge_pending_expiry
            from app.services.traffic_accounting_sync import QUOTA_FIELDS

            fields = set(state.pending_sync_fields or QUOTA_FIELDS)
            # Polling owns quota, not arbitrary metadata. Status changes are
            # necessary only for quota exhaustion/recovery or explicit intent.
            if empty or state.quota_disabled or state.activation_requested:
                fields.update(QUOTA_FIELDS)
                fields.add('status')
            if subscription.pending_panel_expire_at is not None:
                fields.add('expire_at')
            # A PATCH emits user.modified, which schedules this delivery again.
            # Acknowledging an already-observed quota/status is essential: never
            # echo our own unchanged state back to the panel indefinitely.
            desired = payload.update_kwargs(
                user_id=panel.id, now=moment, panel_current=getattr(panel, 'expire_at', None), only_fields=fields
            )
            matches = fields.issubset(QUOTA_FIELDS | {'status'}) and all(
                getattr(getattr(panel, key, None), 'value', getattr(panel, key, None)) == getattr(value, 'value', value)
                for key, value in desired.items()
                if key != 'user_id'
            )
            if matches:
                result = panel
            else:
                result = await write_managed_payload_locked(
                    api,
                    panel.id,
                    payload,
                    now=moment,
                    panel_current=getattr(panel, 'expire_at', None),
                    only_fields=fields,
                )
            if 'status' in fields and payload.status is UserStatus.ACTIVE:
                state.quota_disabled = False
            state.activation_requested = False
            state.pending_sync_fields = []
            state.pending_user_tag = None
            if 'expire_at' in fields:
                await acknowledge_pending_expiry(db, subscription, result)
            if QUOTA_FIELDS.issubset(fields):
                state.projected_revision = state.revision
            # Keep Telegram's cached used/nominal coherent as well as Cabinet.
            if not unlimited:
                subscription.traffic_used_gb = max(
                    0.0, subscription.traffic_limit_gb - account.remaining_bytes(moment) / 1024**3
                )
            return result
    raise AccountingConflict('Quota intent changed repeatedly before delivery')
