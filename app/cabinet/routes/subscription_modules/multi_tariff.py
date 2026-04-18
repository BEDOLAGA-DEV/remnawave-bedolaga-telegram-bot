"""Multi-tariff subscription endpoints for cabinet API.

GET /subscriptions — list all user subscriptions (multi-tariff)
GET /subscriptions/{id} — get specific subscription details
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    decrement_subscription_server_counts,
    get_all_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
)
from app.database.models import SubscriptionStatus, User

from ...dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/subscriptions', tags=['Cabinet Multi-Tariff'], redirect_slashes=False)


class SubscriptionListItem(BaseModel):
    id: int
    name: str | None = None
    status: str
    tariff_id: int | None = None
    tariff_name: str | None = None
    traffic_limit_gb: int = 0
    traffic_used_gb: float = 0.0
    device_limit: int = 1
    end_date: str | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    is_trial: bool = False
    is_daily: bool = False
    is_daily_paused: bool = False
    autopay_enabled: bool = False
    connected_squads: list[str] | None = None


class SubscriptionsListResponse(BaseModel):
    subscriptions: list[SubscriptionListItem]
    multi_tariff_enabled: bool


def _subscription_to_list_item(sub) -> SubscriptionListItem:
    tariff_name = None
    if sub.tariff:
        tariff_name = sub.tariff.name

    return SubscriptionListItem(
        id=sub.id,
        name=getattr(sub, 'name', None),
        status=sub.actual_status,
        tariff_id=sub.tariff_id,
        tariff_name=tariff_name,
        traffic_limit_gb=sub.traffic_limit_gb or 0,
        traffic_used_gb=sub.traffic_used_gb or 0.0,
        device_limit=sub.device_limit or 1,
        end_date=sub.end_date.isoformat() if sub.end_date else None,
        subscription_url=sub.subscription_url,
        subscription_crypto_link=sub.subscription_crypto_link,
        is_trial=sub.is_trial or False,
        is_daily=bool(sub.tariff and getattr(sub.tariff, 'is_daily', False)),
        is_daily_paused=bool(getattr(sub, 'is_daily_paused', False)),
        autopay_enabled=sub.autopay_enabled or False,
        connected_squads=sub.connected_squads,
    )


@router.get('', response_model=SubscriptionsListResponse)
async def list_subscriptions(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SubscriptionsListResponse:
    """List all user subscriptions. Returns all subscriptions regardless of multi-tariff mode."""
    subscriptions = await get_all_subscriptions_by_user_id(db, user.id)
    items = [_subscription_to_list_item(sub) for sub in subscriptions]
    return SubscriptionsListResponse(
        subscriptions=items,
        multi_tariff_enabled=settings.is_multi_tariff_enabled(),
    )


@router.get('/{subscription_id}', response_model=SubscriptionListItem)
async def get_subscription_detail(
    subscription_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SubscriptionListItem:
    """Get specific subscription details with ownership check."""
    subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Subscription not found',
        )
    return _subscription_to_list_item(subscription)


@router.delete('/{subscription_id}')
async def delete_subscription(
    subscription_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """Delete an expired/disabled subscription. Active subscriptions cannot be deleted."""
    subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Subscription not found',
        )

    # Only expired/disabled subscriptions can be deleted
    deletable_statuses = {
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.DISABLED.value,
    }
    if getattr(subscription, 'actual_status', subscription.status) not in deletable_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only expired or disabled subscriptions can be deleted',
        )

    # Delete from RemnaWave panel (stops webhooks / phantom notifications)
    if subscription.remnawave_uuid:
        try:
            from app.services.subscription_service import SubscriptionService

            service = SubscriptionService()
            await service.delete_remnawave_user(subscription.remnawave_uuid)
        except Exception as e:
            logger.warning('Failed to delete RemnaWave user on subscription delete', error=e)

    # Decrement server counts
    await decrement_subscription_server_counts(db, subscription)

    # Delete the subscription
    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Subscription deleted by user',
        subscription_id=subscription_id,
        user_id=user.id,
        tariff_id=subscription.tariff_id,
    )

    return {'message': 'Subscription deleted'}
