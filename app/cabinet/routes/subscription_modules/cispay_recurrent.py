"""Authenticated CisPay consent, payment and cancellation; no guest routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user


router = APIRouter(prefix='/cispay-recurrent')


class CisPayQuoteRequest(BaseModel):
    tariff_id: int = Field(gt=0, strict=True)
    period_days: int = Field(ge=7, le=360, strict=True)
    device_limit: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    subscription_id: int | None = Field(default=None, gt=0, strict=True)


class CisPayPurchaseRequest(BaseModel):
    quote_id: UUID
    checkout_id: UUID


def _error(error):
    code = str(error)
    return HTTPException(status_code=404 if code == 'subscription_not_found' else 409, detail={'code': code})


@router.post('/quote')
async def quote_cispay_recurrent(
    body: CisPayQuoteRequest, user: User = Depends(get_current_cabinet_user), db: AsyncSession = Depends(get_cabinet_db)
):
    from app.services.cispay_recurrent_checkout import quote_cispay_purchase

    try:
        return await quote_cispay_purchase(db, user=user, channel='cabinet', **body.model_dump())
    except ValueError as error:
        await db.rollback()
        raise _error(error) from None


@router.post('/purchase')
async def purchase_cispay_recurrent(
    body: CisPayPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.cispay_recurrent_checkout import purchase_cispay_quote

    try:
        return await purchase_cispay_quote(
            db, user=user, channel='cabinet', quote_id=str(body.quote_id), checkout_id=str(body.checkout_id)
        )
    except ValueError as error:
        raise _error(error) from None


@router.get('')
async def get_cispay_recurrent(
    subscription_id: int = Query(..., gt=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.cispay_recurrent_checkout import get_cispay_agreement_state

    try:
        return await get_cispay_agreement_state(db, user_id=user.id, subscription_id=subscription_id)
    except ValueError as error:
        raise _error(error) from None


@router.post('/cancel')
async def cancel_cispay_recurrent(
    subscription_id: int = Query(..., gt=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.services.cispay_recurrent_service import cancel_cispay_agreement

    try:
        return await cancel_cispay_agreement(db, user_id=user.id, subscription_id=subscription_id, reason='customer')
    except ValueError as error:
        raise _error(error) from None
