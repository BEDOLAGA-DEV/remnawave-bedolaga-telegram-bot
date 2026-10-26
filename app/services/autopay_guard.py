"""Revalidate automatic renewal against locked, current account/subscription data."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, User
from app.utils.subscription_time import ends_within_days


async def lock_autopay_target(
    db: AsyncSession,
    user_id: int,
    subscription_id: int,
    *,
    expected_end_date: datetime,
    expired_only: bool = False,
) -> tuple[User, Subscription] | None:
    """Hold User then Subscription until caller commits the whole renewal.

    The candidate list and ORM identity map may predate a manual renewal,
    consent change, transfer or block. Recheck them before computing a price.
    """
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user_id)
    subscription = await db.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .options(selectinload(Subscription.tariff))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if subscription is not None:
        tariff = subscription.tariff
        allowed = (
            user.status == 'active'
            and subscription.autopay_enabled is True
            and subscription.is_trial is False
            and subscription.end_date == expected_end_date
            and not subscription.is_daily_paused
            and not (tariff and (tariff.is_daily or not tariff.is_active))
            and not (settings.is_tariffs_mode() and not subscription.tariff_id)
        )
        if expired_only:
            allowed = (
                allowed
                and subscription.status == 'expired'
                and now >= subscription.end_date
                and (now - subscription.end_date).days <= 30
            )
        else:
            allowed = (
                allowed
                and (
                    subscription.status == 'active'
                    or (subscription.status == 'expired' and subscription.end_date >= now - timedelta(hours=2))
                )
                and ends_within_days(subscription.end_date, min(subscription.autopay_days_before or 3, 3), now)
            )
        if allowed:
            return user, subscription
    # No billing mutations have occurred: release the read locks without
    # expiring the caller's user object (used by the top-up response).
    await db.commit()
    return None
