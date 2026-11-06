"""Durable explicit panel field masks, separate from routine quota projection."""

from sqlalchemy import select

from app.database.models import TrafficAccountingState
from app.services.traffic_accounting import AccountingConflict


QUOTA_FIELDS = frozenset({'traffic_limit_bytes', 'traffic_limit_strategy'})
SYNC_FIELDS = frozenset(
    {
        *QUOTA_FIELDS,
        'status',
        'expire_at',
        'telegram_id',
        'email',
        'description',
        'tag',
        'hwid_device_limit',
        'active_internal_squads',
        'external_squad_uuid',
    }
)


async def stage_managed_sync(db, subscription, *, only_fields=None, user_tag=None):
    """Join requested fields before commit; retries recompute their latest values.

    Financial callers already serialize the subscription. This helper has no
    network calls and never commits; narrowing one request cannot discard an
    earlier undelivered renewal's expiry or squad update.
    """
    if getattr(subscription, 'traffic_accounting_mode', None) not in ('managed', 'migrating'):
        return
    await db.flush()
    state = await db.scalar(
        select(TrafficAccountingState)
        .where(
            TrafficAccountingState.subscription_id == subscription.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None or state.phase not in ('ready', 'unlimited'):
        raise AccountingConflict('Panel synchronization awaits ready accounting')
    fields = SYNC_FIELDS if only_fields is None else SYNC_FIELDS.intersection(only_fields)
    if not fields:
        return
    state.pending_sync_fields = sorted(set(state.pending_sync_fields or ()).union(fields))
    if 'tag' in fields:
        state.pending_user_tag = user_tag
    state.revision += 1
    state.next_delivery_at = None
    await db.flush()
