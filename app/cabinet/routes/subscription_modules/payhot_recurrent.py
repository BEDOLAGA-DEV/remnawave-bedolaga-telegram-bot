"""Authenticated HOT Pay recurring consent, status and cancellation."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user


router = APIRouter(prefix='/payhot-recurrent')
_SAFE_ERRORS = frozenset(
    {
        'subscription_not_found',
        'user_restricted',
        'payment_method_unavailable',
        'period_unavailable',
        'price_unavailable',
        'amount_out_of_range',
        'checkout_conflict',
        'classic_paid_subscription_required',
        'device_selection_not_available',
        'disable_balance_autopay_first',
        'quote_changed',
        'quote_expired',
        'recurrent_already_exists',
        'select_existing_subscription',
        'subscription_disabled',
        'tariff_switch_requires_balance',
        'tariff_unavailable',
        'credentials_changed',
        'disabled',
    }
)


class PayHotQuoteRequest(BaseModel):
    tariff_id: int | None = Field(default=None, gt=0, strict=True)
    period_days: int = Field(gt=0, le=2_147_483_647, strict=True)
    device_limit: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    subscription_id: int | None = Field(default=None, gt=0, strict=True)
    payment_method: Literal['card', 'sbp']


class PayHotPurchaseRequest(BaseModel):
    quote_id: UUID
    checkout_id: UUID


def payhot_error(error: ValueError) -> HTTPException:
    code = str(error)
    if code not in _SAFE_ERRORS:
        code = 'payment_unavailable'
    return HTTPException(status_code=404 if code == 'subscription_not_found' else 409, detail={'code': code})


async def payhot_availability(db, user) -> dict:
    from app.services.payhot_recurrent_policy import get_payhot_recurrent_policy

    policy = await get_payhot_recurrent_policy(db, user=user, channel='cabinet')
    return {'enabled': policy['enabled'], 'methods': policy['methods']}


@router.post('/quote')
async def quote_payhot_recurrent(
    body: PayHotQuoteRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.payhot_recurrent_checkout import quote_payhot_purchase

    try:
        result = await quote_payhot_purchase(db, user=user, channel='cabinet', **body.model_dump())
        return {
            **{
                key: result[key]
                for key in (
                    'quote_id',
                    'expires_at',
                    'amount_kopeks',
                    'period_days',
                    'payment_method',
                    'first_payment_required',
                )
                if key in result
            },
            'source_subscription_id': result.get('source_subscription_id', body.subscription_id),
        }
    except ValueError as error:
        await db.rollback()
        raise payhot_error(error) from None


@router.post('/purchase')
async def purchase_payhot_recurrent(
    body: PayHotPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.payhot_recurrent_checkout import purchase_payhot_quote

    try:
        return await purchase_payhot_quote(
            db, user=user, channel='cabinet', quote_id=str(body.quote_id), checkout_id=str(body.checkout_id)
        )
    except ValueError as error:
        await db.rollback()
        raise payhot_error(error) from None


@router.get('')
async def get_payhot_recurrent(
    subscription_id: int = Query(..., gt=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.payhot_recurrent_checkout import get_payhot_agreement_state

    try:
        state = await get_payhot_agreement_state(db, user_id=user.id, subscription_id=subscription_id)
        return {**state, **await payhot_availability(db, user)}
    except ValueError as error:
        raise payhot_error(error) from None


@router.post('/cancel')
async def cancel_payhot_recurrent(
    subscription_id: int = Query(..., gt=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.payhot_recurrent_service import cancel_payhot_agreement

    try:
        return await cancel_payhot_agreement(db, user_id=user.id, subscription_id=subscription_id, reason='customer')
    except ValueError as error:
        await db.rollback()
        raise payhot_error(error) from None
