"""AuraPay card renewal status and cancellation for a cabinet user."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.aurapay_recurrent import cancel, get_active

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


router = APIRouter()


@router.post('/aurapay-recurrent/enable')
async def enable_aurapay_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    from app.config import settings

    if not settings.is_aurapay_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='AuraPay recurrent disabled')
    subscription = await resolve_subscription(db, user, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    if not subscription.tariff_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Subscription has no tariff')

    from app.database.crud.tariff import get_tariff_by_id
    from app.services.aurapay_recurrent import enable

    tariff = await get_tariff_by_id(db, subscription.tariff_id)
    if tariff is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')
    try:
        record = await enable(db, user=user, subscription=subscription, tariff=tariff)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Could not create AuraPay subscription',
        ) from error
    return {'status': record.status, 'redirect_url': record.redirect_url}


@router.post('/aurapay-recurrent/purchase')
async def purchase_with_aurapay_recurrent(
    tariff_id: int = Query(...),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    from app.config import settings

    if not settings.is_aurapay_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='AuraPay recurrent disabled')
    from app.database.crud.tariff import get_tariff_by_id
    from app.services.aurapay_recurrent import purchase

    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')
    try:
        record = await purchase(db, user=user, tariff=tariff)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Could not create AuraPay subscription',
        ) from error
    return {
        'status': record.status,
        'redirect_url': record.redirect_url,
        'subscription_id': record.subscription_id,
    }


@router.get('/aurapay-recurrent')
async def get_aurapay_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    subscription = await resolve_subscription(db, user, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    record = await get_active(db, subscription.id)
    if record is None:
        return {'status': 'none'}
    return {
        'status': record.status,
        'amount_kopeks': record.amount_kopeks,
        'period': record.period,
        'interval': record.interval,
        'next_charge_at': record.next_charge_at.isoformat() if record.next_charge_at else None,
        'redirect_url': record.redirect_url if record.status == 'NEW' else None,
    }


@router.post('/aurapay-recurrent/cancel')
async def cancel_aurapay_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    subscription = await resolve_subscription(db, user, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    record = await get_active(db, subscription.id)
    if record is None:
        return {'status': 'none'}
    try:
        await cancel(db, record)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='AuraPay cancellation was not confirmed',
        ) from error
    return {'status': 'cancelled'}
