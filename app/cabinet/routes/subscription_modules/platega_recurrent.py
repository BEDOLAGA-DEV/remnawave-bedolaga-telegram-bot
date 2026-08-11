"""Platega SBP auto-renewal endpoints (cabinet, user-facing).

POST /subscription/platega-recurrent/enable
GET  /subscription/platega-recurrent
POST /subscription/platega-recurrent/cancel

All three gate on ``settings.is_platega_recurrent_enabled()`` before doing
anything else. ``resolve_subscription`` is imported at module level (unlike
most other lazy imports here) so tests can monkeypatch it directly, mirroring
``subscription_modules/traffic.py``.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post('/platega-recurrent/enable')
async def enable_platega_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Enable Platega SBP auto-renewal for the resolved subscription."""
    from app.config import settings

    if not settings.is_platega_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Platega recurrent disabled')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    # Паритет с бот-флоу: триальная подписка не должна авторизовывать реальное
    # рекуррентное списание в банке (бот блокирует это в handle_sbp_recurring_enable).
    if getattr(subscription, 'is_trial', False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Trial subscriptions cannot enable SBP auto-payment',
        )

    # Load the tariff explicitly — subscription.tariff is an async lazy-load
    # relationship that would raise MissingGreenlet if touched here.
    if not subscription.tariff_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Subscription has no tariff')

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, subscription.tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')

    from app.services.payment.platega import enable_platega_sbp_recurring

    try:
        result = await enable_platega_sbp_recurring(
            db,
            user_id=user.id,
            subscription=subscription,
            tariff=tariff,
        )
    except ValueError as error:
        # No price configured for the resolved charge period.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except RuntimeError as error:
        # PlategaApiError несёт человекочитаемую причину провайдера (например,
        # «метод Subscription не включён для мерчанта»); голый RuntimeError
        # (нет transactionId / транспорт) остаётся с generic-текстом.
        from app.services.platega_service import PlategaApiError

        detail = (
            f'Could not create Platega subscription: {error}'
            if isinstance(error, PlategaApiError)
            else 'Could not create Platega subscription'
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from error

    return {'status': result['status'], 'redirect_url': result['redirect_url']}


@router.post('/platega-recurrent/purchase')
async def purchase_with_platega_recurrent(
    tariff_id: int = Query(..., description='Tariff to subscribe to'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Оформление подписки на тариф через СБП-автопродление (оплата привязкой).

    Альтернатива покупке с баланса: первое списание Platega = подтверждение
    привязки в банке. Для нового тарифа создаётся EXPIRED-заготовка, которую
    активирует первый чардж (см. ``purchase_tariff_with_sbp_recurring``).
    """
    from app.config import settings

    if not settings.is_platega_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Platega recurrent disabled')

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not getattr(tariff, 'is_active', False):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')

    from app.services.payment.platega import purchase_tariff_with_sbp_recurring

    try:
        result = await purchase_tariff_with_sbp_recurring(db, user=user, tariff=tariff)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except RuntimeError as error:
        from app.services.platega_service import PlategaApiError

        detail = (
            f'Could not create Platega subscription: {error}'
            if isinstance(error, PlategaApiError)
            else 'Could not create Platega subscription'
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from error

    return {
        'status': result['status'],
        'redirect_url': result['redirect_url'],
        'subscription_id': result['subscription_id'],
    }


@router.get('/platega-recurrent')
async def get_platega_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Return the current Platega SBP auto-renewal state for the subscription."""
    from app.config import settings

    if not settings.is_platega_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Platega recurrent disabled')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.database.crud import platega_subscription as sub_crud

    record = await sub_crud.get_active_platega_subscription_by_subscription(db, subscription.id)
    if not record:
        return {'status': 'none'}

    return {
        'status': record.status,
        'interval': record.interval,
        'amount_kopeks': record.amount_kopeks,
        'next_charge_at': record.next_charge_at.isoformat() if record.next_charge_at else None,
        'redirect_url': record.redirect_url,
    }


@router.post('/platega-recurrent/cancel')
async def cancel_platega_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Cancel Platega SBP auto-renewal for the resolved subscription (best-effort).

    НЕ гейтится флагом рекуррента: отмена — операция безопасности. Если фичу
    выключили при живых привязках, Platega продолжает списывать — юзер обязан
    иметь путь остановить это (гейт оставлен только на enable/get).
    """
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

    return {'status': 'cancelled'}
