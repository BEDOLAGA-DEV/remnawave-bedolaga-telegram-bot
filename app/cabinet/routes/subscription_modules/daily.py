"""Daily subscription management endpoints.

POST /subscription/pause
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import User
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post('/pause')
async def toggle_subscription_pause(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Toggle pause/resume for daily subscription."""
    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    tariff_id = getattr(subscription, 'tariff_id', None)
    if not tariff_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Subscription has no tariff',
        )

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not getattr(tariff, 'is_daily', False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Pause is only available for daily tariffs',
        )

    # Determine current state
    from app.database.models import SubscriptionStatus

    is_currently_paused = getattr(subscription, 'is_daily_paused', False)
    was_disabled = subscription.status in (
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.LIMITED.value,
    )

    # System-DISABLED subs (insufficient balance) should always be treated as needing resume,
    # even if is_daily_paused is False (it's set by the system, not the user)
    if was_disabled and not is_currently_paused:
        new_paused_state = False  # Force resume path
    else:
        new_paused_state = not is_currently_paused

    raw_daily_price = getattr(tariff, 'daily_price_kopeks', 0)

    # Lock user BEFORE discount computation to prevent TOCTOU on promo group
    # IMPORTANT: must happen BEFORE modifying subscription — lock_user_for_pricing
    # reloads subscriptions via selectinload which resets in-memory changes
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Re-fetch subscription after lock (selectinload may have replaced the ORM object)
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='Subscription not found after lock')

    subscription.is_daily_paused = new_paused_state

    # Apply group discount to daily price (consistent with DailySubscriptionService and miniapp resume)
    from app.services.pricing_engine import PricingEngine

    promo_group = PricingEngine.resolve_promo_group(user)
    daily_group_pct = promo_group.get_discount_percent('period', 1) if promo_group else 0
    daily_price = (
        PricingEngine.apply_discount(raw_daily_price, daily_group_pct) if daily_group_pct > 0 else raw_daily_price
    )

    # If resuming, check balance and charge
    if not new_paused_state:
        if daily_price > 0 and user.balance_kopeks < daily_price:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    'code': 'insufficient_balance',
                    'message': 'Insufficient balance to resume daily subscription',
                    'required': daily_price,
                    'balance': user.balance_kopeks,
                },
            )

        # Charge daily fee FIRST, then restore ACTIVE status
        if was_disabled:
            if daily_price > 0:
                from app.database.crud.user import subtract_user_balance

                deducted = await subtract_user_balance(
                    db,
                    user,
                    daily_price,
                    f'Суточная оплата тарифа «{tariff.name}» (возобновление)',
                    mark_as_paid_subscription=True,
                )
                if not deducted:
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail={
                            'code': 'insufficient_balance',
                            'message': 'Balance deduction failed',
                            'required': daily_price,
                            'balance': user.balance_kopeks,
                        },
                    )

                from app.database.crud.transaction import create_transaction
                from app.database.models import TransactionType

                try:
                    await create_transaction(
                        db=db,
                        user_id=user.id,
                        type=TransactionType.SUBSCRIPTION_PAYMENT,
                        amount_kopeks=daily_price,
                        description=f'Суточная оплата тарифа «{tariff.name}» (возобновление)',
                    )
                except Exception as exc:
                    logger.warning('Failed to create resume transaction', error=exc)

            # Balance deducted successfully — now activate
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.last_daily_charge_at = datetime.now(UTC)
            subscription.end_date = datetime.now(UTC) + timedelta(days=1)

    await db.commit()
    await db.refresh(subscription)
    await db.refresh(user)

    # Sync with RemnaWave only when resuming from DISABLED state
    if not new_paused_state and was_disabled:
        try:
            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=False,
                reset_reason=None,
            )
        except Exception as e:
            logger.error('Error syncing RemnaWave user on resume', error=e)

    if new_paused_state:
        message = 'Daily subscription paused'
    else:
        message = 'Daily subscription resumed'

    return {
        'success': True,
        'message': message,
        'is_paused': new_paused_state,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }
