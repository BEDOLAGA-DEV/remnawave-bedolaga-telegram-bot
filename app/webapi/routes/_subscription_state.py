from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription


def snapshot_subscription_state(subscription: Subscription) -> dict[str, Any]:
    return {
        'status': subscription.status,
        'is_trial': subscription.is_trial,
        'start_date': subscription.start_date,
        'end_date': subscription.end_date,
        'traffic_limit_gb': subscription.traffic_limit_gb,
        'traffic_used_gb': subscription.traffic_used_gb,
        'purchased_traffic_gb': subscription.purchased_traffic_gb,
        'traffic_reset_at': subscription.traffic_reset_at,
        'device_limit': subscription.device_limit,
        'connected_squads': list(subscription.connected_squads or []),
        'subscription_url': subscription.subscription_url,
        'subscription_crypto_link': subscription.subscription_crypto_link,
        'remnawave_short_uuid': subscription.remnawave_short_uuid,
        'tariff_id': subscription.tariff_id,
        'autopay_enabled': getattr(subscription, 'autopay_enabled', False),
        'autopay_days_before': getattr(subscription, 'autopay_days_before', None),
        'is_daily_paused': getattr(subscription, 'is_daily_paused', False),
        'last_daily_charge_at': getattr(subscription, 'last_daily_charge_at', None),
        'updated_at': subscription.updated_at,
    }


async def restore_subscription_state(
    db: AsyncSession,
    subscription_id: int,
    snapshot: dict[str, Any],
) -> None:
    result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    subscription = result.scalar_one_or_none()
    if not subscription:
        return

    for field, value in snapshot.items():
        if field == 'connected_squads':
            setattr(subscription, field, list(value or []))
        else:
            setattr(subscription, field, value)

    await db.commit()
    await db.refresh(subscription)
