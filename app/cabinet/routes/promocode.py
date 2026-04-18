"""Promo code routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.promocode_service import PromoCodeService

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/promocode', tags=['Cabinet Promocode'])


class PromocodeActivateRequest(BaseModel):
    """Request to activate a promo code."""

    code: str = Field(..., min_length=1, max_length=50, description='Promo code to activate')
    subscription_id: int | None = Field(None, description='Subscription ID for multi-tariff promo codes')


class PromocodeActivateResponse(BaseModel):
    """Response after activating a promo code."""

    success: bool
    message: str
    balance_before: float = 0
    balance_after: float = 0
    bonus_description: str | None = None


class PromocodeDeactivateResponse(BaseModel):
    """Response after deactivating a discount promo code."""

    success: bool
    message: str
    deactivated_code: str | None = None
    discount_percent: int = 0


@router.post('/activate')
async def activate_promocode(
    request: PromocodeActivateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Activate a promo code for the current user."""
    promocode_service = PromoCodeService()

    result = await promocode_service.activate_promocode(
        db=db, user_id=user.id, code=request.code.strip(), subscription_id=request.subscription_id
    )

    if result.get('error') == 'select_subscription':
        return {
            'success': False,
            'error': 'select_subscription',
            'eligible_subscriptions': result.get('eligible_subscriptions', []),
            'code': result.get('code', request.code.strip()),
        }

    if result['success']:
        balance_before_rubles = result.get('balance_before_kopeks', 0) / 100
        balance_after_rubles = result.get('balance_after_kopeks', 0) / 100

        return PromocodeActivateResponse(
            success=True,
            message='Promo code activated successfully',
            balance_before=balance_before_rubles,
            balance_after=balance_after_rubles,
            bonus_description=result.get('description'),
        )

    # Map error codes to messages
    error_messages = {
        'not_found': 'Promo code not found',
        'expired': 'Promo code has expired',
        'inactive': 'Promo code is deactivated',
        'not_yet_valid': 'Promo code is not yet active',
        'used': 'Promo code has been fully used',
        'already_used_by_user': 'You have already used this promo code',
        'active_discount_exists': 'You already have an active discount. Deactivate it first via /deactivate-discount',
        'no_subscription_for_days': 'This promo code requires an active or expired subscription',
        'subscription_not_found': 'Subscription not found',
        'not_first_purchase': 'This promo code is only available for first purchase',
        'daily_limit': 'Too many promo code activations today',
        'user_not_found': 'User not found',
        'server_error': 'Server error occurred',
    }

    error_code = result.get('error', 'server_error')
    error_message = error_messages.get(error_code, 'Failed to activate promo code')

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=error_message,
    )


@router.post('/deactivate-discount', response_model=PromocodeDeactivateResponse)
async def deactivate_discount_promocode(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PromocodeDeactivateResponse:
    """Deactivate the currently active discount promo code for the current user."""
    promocode_service = PromoCodeService()

    result = await promocode_service.deactivate_discount_promocode(
        db=db,
        user_id=user.id,
        admin_initiated=False,
    )

    if result['success']:
        return PromocodeDeactivateResponse(
            success=True,
            message='Discount promo code deactivated successfully',
            deactivated_code=result.get('deactivated_code'),
            discount_percent=result.get('discount_percent', 0),
        )

    error_messages = {
        'user_not_found': 'User not found',
        'no_active_discount_promocode': 'No active discount promo code found',
        'discount_already_expired': 'Discount has already expired',
        'server_error': 'Server error occurred',
    }

    error_code = result.get('error', 'server_error')
    error_message = error_messages.get(error_code, 'Failed to deactivate promo code')

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=error_message,
    )
