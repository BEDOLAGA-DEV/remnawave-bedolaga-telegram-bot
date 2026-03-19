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
    get_all_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
)
from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/subscriptions', tags=['Cabinet Multi-Tariff'], redirect_slashes=False)


class SubscriptionListItem(BaseModel):
    id: int
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
        autopay_enabled=sub.autopay_enabled or False,
        connected_squads=sub.connected_squads,
    )


@router.get('/', response_model=SubscriptionsListResponse)
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
