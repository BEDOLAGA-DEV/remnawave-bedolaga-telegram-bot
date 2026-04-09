"""Autopay settings endpoint.

PATCH /subscription/autopay
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import AutopayUpdateRequest


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.patch('/autopay')
async def update_autopay(
    request: AutopayUpdateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Update autopay settings."""
    from .helpers import resolve_subscription

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if request.enabled:
        # Classic subscriptions cannot use autopay when tariff mode is enabled
        from app.config import settings

        if settings.is_tariffs_mode() and not subscription.tariff_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for classic subscriptions. Please purchase a tariff.',
            )

        # Триальные подписки — пробник, автопродление не имеет смысла
        if subscription.is_trial:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for trial subscriptions',
            )

        # Суточные подписки имеют свой механизм продления (DailySubscriptionService),
        # глобальный autopay для них запрещён
        await db.refresh(subscription, ['tariff'])
        if subscription.tariff and getattr(subscription.tariff, 'is_daily', False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Autopay is not available for daily subscriptions',
            )

    subscription.autopay_enabled = request.enabled

    if request.days_before is not None:
        subscription.autopay_days_before = request.days_before

    await db.commit()

    return {
        'message': 'Autopay settings updated',
        'autopay_enabled': subscription.autopay_enabled,
        'autopay_days_before': subscription.autopay_days_before,
    }
