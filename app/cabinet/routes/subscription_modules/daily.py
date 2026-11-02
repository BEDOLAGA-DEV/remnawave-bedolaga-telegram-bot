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
from app.services.traffic_reset_policy import lift_panel_traffic_limit, should_reset_traffic_on_daily_charge

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post('/pause')
async def toggle_subscription_pause(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    paused: bool | None = None,
) -> dict[str, Any]:
    """Toggle pause/resume for daily subscription."""
    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    # Serialize money and state decisions, then read the current paid entitlement.
    from app.database.crud.user import lock_user_for_pricing
    from app.database.models import SubscriptionStatus

    user = await lock_user_for_pricing(db, user.id)
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='Subscription not found after lock')
    await db.refresh(subscription, with_for_update=True)

    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Trial subscriptions must be converted through tariff purchase')

    tariff_id = getattr(subscription, 'tariff_id', None)
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id else None
    if not tariff or not getattr(tariff, 'is_daily', False):
        raise HTTPException(status_code=400, detail='Pause is only available for daily tariffs')

    is_currently_paused = bool(subscription.is_daily_paused)
    actual_status = subscription.actual_status
    was_disabled = actual_status in ('disabled', 'expired', 'limited')
    was_limited = actual_status == 'limited'
    # Explicit target makes retries safe; old callers retain toggle behavior.
    new_paused_state = paused if paused is not None else (False if was_disabled else not is_currently_paused)
    if not new_paused_state and user.restriction_subscription:
        raise HTTPException(status_code=403, detail='Subscription renewal is restricted for this account')
    transaction = None

    subscription.is_daily_paused = new_paused_state

    # Apply group discount to daily price (consistent with DailySubscriptionService and miniapp resume)
    from app.services.pricing_engine import PricingEngine

    daily_price = PricingEngine.daily_subscription_price(subscription, tariff, user)

    # If resuming, check balance and charge
    if not new_paused_state:
        if was_disabled and daily_price > 0 and user.balance_kopeks < daily_price:
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
                    commit=False,
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

                transaction = await create_transaction(
                    db=db,
                    user_id=user.id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT,
                    amount_kopeks=daily_price,
                    description=f'Суточная оплата тарифа «{tariff.name}» (возобновление)',
                    commit=False,
                )

            # Balance deducted successfully — now activate
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.last_daily_charge_at = datetime.now(UTC)
            subscription.end_date = datetime.now(UTC) + timedelta(days=1)
            subscription.pending_panel_expire_at = subscription.end_date

    await db.commit()
    await db.refresh(subscription)
    await db.refresh(user)

    if transaction is not None:
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=daily_price,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            description=transaction.description,
        )

    # Sync with RemnaWave only when resuming from DISABLED state
    if not new_paused_state and was_disabled:
        # Возобновление списывает суточную оплату — значит, обнуление счётчика
        # решает та же политика, что и в ночном списании (см. traffic_reset_policy).
        reset_traffic = should_reset_traffic_on_daily_charge(tariff)
        try:
            subscription_service = SubscriptionService()
            await subscription_service.create_remnawave_user(
                db,
                subscription,
                reset_traffic=reset_traffic,
                reset_reason='суточное списание (возобновление)' if reset_traffic else None,
            )
            if reset_traffic:
                subscription.traffic_used_gb = 0.0
                await db.commit()
                if was_limited:
                    # PATCH сам по себе статус «трафик исчерпан» не снимает.
                    await lift_panel_traffic_limit(db, subscription, service=subscription_service)
        except Exception as e:
            logger.error('Error syncing RemnaWave user on resume', error=e)
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=user.id,
                action='create',
            )

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
