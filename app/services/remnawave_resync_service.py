from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.models import User
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


async def resync_user_subscriptions_with_panel(
    db: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Resync all active subscriptions for a user with the RemnaWave panel.

    Should be called after any identity change (TG linking, account merge,
    email verification) to ensure the panel has up-to-date telegram_id,
    email, and squads.

    Returns a stats dict with keys:
        synced  - number of subscriptions successfully synced
        failed  - number of subscriptions that failed to sync
        total   - total number of active subscriptions found
        skipped - True when the panel is not configured
    """
    service = SubscriptionService()
    service._refresh_configuration()

    if not service.is_configured:
        logger.warning(
            'remnawave_resync: panel not configured, skipping resync',
            user_id=user.id,
            config_error=service.configuration_error,
        )
        return {'synced': 0, 'failed': 0, 'total': 0, 'skipped': True}

    subscriptions = await get_active_subscriptions_by_user_id(db, int(user.id))

    if not subscriptions:
        logger.info(
            'remnawave_resync: no active subscriptions found',
            user_id=user.id,
        )
        return {'synced': 0, 'failed': 0, 'total': 0, 'skipped': False}

    synced = 0
    failed = 0

    for subscription in subscriptions:
        # Eagerly refresh tariff to avoid lazy-loading in async context.
        try:
            await db.refresh(subscription, ['tariff'])
        except Exception as exc:
            logger.debug(
                'remnawave_resync: could not refresh tariff for subscription',
                subscription_id=subscription.id,
                error=exc,
            )

        # Determine whether a panel user already exists for this subscription.
        if settings.is_multi_tariff_enabled():
            panel_user_exists = bool(subscription.remnawave_uuid)
        else:
            panel_user_exists = bool(user.remnawave_uuid)

        try:
            if panel_user_exists:
                result = await service.update_remnawave_user(
                    db,
                    subscription,
                    sync_squads=True,
                )
            else:
                result = await service.create_remnawave_user(db, subscription)

            if result is not None:
                synced += 1
                logger.info(
                    'remnawave_resync: subscription synced',
                    subscription_id=subscription.id,
                    user_id=user.id,
                    action='update' if panel_user_exists else 'create',
                )
            else:
                failed += 1
                logger.warning(
                    'remnawave_resync: subscription sync returned None',
                    subscription_id=subscription.id,
                    user_id=user.id,
                    action='update' if panel_user_exists else 'create',
                )
        except Exception as exc:
            failed += 1
            logger.error(
                'remnawave_resync: unexpected error syncing subscription',
                subscription_id=subscription.id,
                user_id=user.id,
                error=exc,
            )

    total = len(subscriptions)
    logger.info(
        'remnawave_resync: completed',
        user_id=user.id,
        total=total,
        synced=synced,
        failed=failed,
    )

    return {'synced': synced, 'failed': failed, 'total': total, 'skipped': False}
