"""Serialize reservations on the account; no network inside the reservation transaction."""

from uuid import UUID

from sqlalchemy import select

from app.database.models import CisPaySubscription, Subscription, User


LIVE_STATUSES = ('CREATING', 'CREATION_UNKNOWN', 'PENDING', 'ACTIVE', 'PAUSED', 'CANCEL_PENDING')


async def reserve_agreement(db, *, user_id: int, subscription_id: int, shop_id: str, checkout_id: str, snapshot: dict):
    checkout_id = str(UUID(str(checkout_id)))
    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if user is None or user.status != 'active' or user.restriction_topup:
        raise ValueError('user_restricted')
    previous = await db.scalar(
        select(CisPaySubscription).where(
            CisPaySubscription.shop_id == shop_id, CisPaySubscription.checkout_id == checkout_id
        )
    )
    if previous is not None:
        if previous.owner_id != user_id or previous.subscription_id != subscription_id or previous.snapshot != snapshot:
            raise ValueError('checkout_conflict')
        await db.commit()
        return previous
    target = await db.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        .with_for_update()
    )
    if target is None:
        raise ValueError('subscription_not_found')
    conflict = await db.scalar(
        select(CisPaySubscription.id).where(
            ((CisPaySubscription.subscription_id == subscription_id) & CisPaySubscription.status.in_(LIVE_STATUSES))
            | (
                (CisPaySubscription.owner_id == user_id)
                & (CisPaySubscription.shop_id == shop_id)
                & CisPaySubscription.provider_id.is_(None)
                & CisPaySubscription.status.in_(LIVE_STATUSES)
            )
        )
    )
    if conflict is not None:
        raise ValueError('recurrent_already_exists')
    purpose = f'Подписка на {snapshot["period_days"]} дней · Заказ {checkout_id}'
    agreement = CisPaySubscription(
        user_id=user_id,
        owner_id=user_id,
        subscription_id=subscription_id,
        shop_id=shop_id,
        checkout_id=checkout_id,
        snapshot=dict(snapshot),
        purpose=purpose,
        status='CREATING',
        expected_end_date=target.end_date,
    )
    db.add(agreement)
    await db.commit()
    await db.refresh(agreement)
    return agreement
