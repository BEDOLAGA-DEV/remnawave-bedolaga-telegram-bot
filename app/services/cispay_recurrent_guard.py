"""Local recurring guards. Cancellation intent participates in the caller's transaction."""

from sqlalchemy import select, update

from app.database.crud.cispay_subscriptions import LIVE_STATUSES
from app.database.models import CisPaySubscription


async def has_live_cispay(db, subscription_id: int) -> bool:
    return (
        await db.scalar(
            select(CisPaySubscription.id)
            .where(
                CisPaySubscription.subscription_id == subscription_id,
                CisPaySubscription.status.in_(LIVE_STATUSES),
            )
            .limit(1)
        )
        is not None
    )


async def _has_conflicting_cispay_only(db, *, user_id: int, subscription_id: int) -> bool:
    from app.config import settings

    if settings.is_multi_tariff_enabled():
        return await has_live_cispay(db, subscription_id)
    # In single mode an old expired row must not renew from balance while a
    # hosted checkout is buying the user's replacement subscription.
    return (
        await db.scalar(
            select(CisPaySubscription.id)
            .where(
                CisPaySubscription.owner_id == user_id,
                CisPaySubscription.status.in_(LIVE_STATUSES),
            )
            .limit(1)
        )
        is not None
    )


async def has_conflicting_cispay(db, *, user_id: int, subscription_id: int) -> bool:
    """Compatibility guard for competing automatic billing, including HOT Pay."""
    from app.services.payhot_recurrent_guard import has_conflicting_payhot

    return await _has_conflicting_cispay_only(
        db, user_id=user_id, subscription_id=subscription_id
    ) or await has_conflicting_payhot(db, user_id=user_id, subscription_id=subscription_id)


async def require_no_cispay(db, *, user_id: int, subscription_id: int, lock_user: bool = True):
    """Reject live billing; unlocked preflight must be rechecked before mutation."""
    from app.services.payhot_recurrent_guard import require_no_payhot

    # Both providers reserve under the account lock, even when enrollment is disabled.
    await require_no_payhot(db, user_id=user_id, subscription_id=subscription_id, lock_user=lock_user)
    if await _has_conflicting_cispay_only(db, user_id=user_id, subscription_id=subscription_id):
        raise ValueError('Сначала отключите автоплатёж CisPay и дождитесь подтверждения отключения.')


async def stage_cispay_cancellation(db, subscription_id: int, *, reason: str):
    from app.services.payhot_recurrent_guard import stage_payhot_cancellation

    await stage_payhot_cancellation(db, subscription_id, reason=reason)
    # No commit or network: never commit an unfinished purchase/admin mutation here.
    await db.execute(
        update(CisPaySubscription)
        .where(
            CisPaySubscription.subscription_id == subscription_id,
            CisPaySubscription.status.in_(LIVE_STATUSES),
        )
        .values(status='CANCEL_PENDING', reason=reason[:64], retry_at=None)
    )


async def guard_cispay_credentials(db, key: str, value):
    from app.services.payhot_recurrent_guard import guard_payhot_credentials

    await guard_payhot_credentials(db, key, value)
    if key not in ('CISPAY_SHOP_ID', 'CISPAY_API_KEY'):
        return
    rows = list(
        (
            await db.scalars(
                select(CisPaySubscription.shop_id).where(CisPaySubscription.status.in_(LIVE_STATUSES)).distinct()
            )
        ).all()
    )
    if rows and (
        (key == 'CISPAY_SHOP_ID' and any(shop != str(value).strip() for shop in rows))
        or (key == 'CISPAY_API_KEY' and not str(value or '').strip())
    ):
        raise ValueError('Сначала отключите действующие автоплатежи CisPay. Реквизиты нужны для их отмены.')
