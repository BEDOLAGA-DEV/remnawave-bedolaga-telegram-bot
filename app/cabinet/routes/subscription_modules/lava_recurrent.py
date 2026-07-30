"""Lava auto-renewal endpoints (cabinet, user-facing).

POST /subscription/lava-recurrent/enable
GET  /subscription/lava-recurrent
POST /subscription/lava-recurrent/cancel

Зеркало ``platega_recurrent.py``. Enable/get гейтятся
``settings.is_lava_recurrent_enabled()``; cancel — намеренно НЕ гейтится
(операция безопасности: при выключенной фиче живые привязки продолжают
списывать, и путь остановки обязан оставаться доступным).
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


@router.post('/lava-recurrent/enable')
async def enable_lava_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Включает автопродление Lava для выбранной подписки."""
    from app.config import settings

    if not settings.is_lava_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Lava recurrent disabled')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    # Паритет с Platega: триальная подписка не должна авторизовывать реальное
    # рекуррентное списание.
    if getattr(subscription, 'is_trial', False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Trial subscriptions cannot enable auto-payment',
        )

    # Тариф грузим явно: subscription.tariff — async lazy-load, обращение к
    # нему здесь упало бы MissingGreenlet.
    if not subscription.tariff_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Subscription has no tariff')

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, subscription.tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')

    from app.services.payment.lava import enable_lava_recurring

    try:
        result = await enable_lava_recurring(
            db,
            user_id=user.id,
            subscription=subscription,
            tariff=tariff,
        )
    except ValueError as error:
        # У тарифа не задан продукт Lava либо продукт без цены — причина
        # человекочитаемая, отдаём её как есть.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        logger.warning('Lava recurrent enable failed', error=str(error), user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Could not create Lava subscription',
        ) from error

    return {'status': result['status'], 'redirect_url': result['redirect_url']}


@router.post('/lava-recurrent/purchase')
async def purchase_with_lava_recurrent(
    tariff_id: int = Query(..., description='Tariff to subscribe to'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Оформление подписки на тариф оплатой привязкой Lava.

    Альтернатива покупке с баланса: первое списание Lava оплачивается по
    ссылке из ответа и оживляет подписку (для нового тарифа создаётся
    неактивная заготовка).
    """
    from app.config import settings

    if not settings.is_lava_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Lava recurrent disabled')

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not getattr(tariff, 'is_active', False):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')

    from app.services.payment.lava import purchase_tariff_with_lava_recurring

    try:
        result = await purchase_tariff_with_lava_recurring(db, user=user, tariff=tariff)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        logger.warning('Lava recurrent purchase failed', error=str(error), user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Could not create Lava subscription',
        ) from error

    return {
        'status': result['status'],
        'redirect_url': result['redirect_url'],
        'subscription_id': result['subscription_id'],
    }


@router.get('/lava-recurrent')
async def get_lava_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Текущее состояние автопродления Lava для подписки."""
    from app.config import settings

    if not settings.is_lava_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Lava recurrent disabled')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.services.payment.lava import get_lava_recurring_status

    state = await get_lava_recurring_status(db, subscription.id)
    if not state:
        return {'status': 'none'}

    return {
        'status': state['status'],
        'charge_days': state['charge_days'],
        'amount_kopeks': state['amount_kopeks'],
        'next_charge_at': state['next_charge_at'].isoformat() if state['next_charge_at'] else None,
        'redirect_url': state['redirect_url'],
    }


@router.post('/lava-recurrent/cancel')
async def cancel_lava_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Отменяет автопродление Lava (best-effort).

    НЕ гейтится флагом рекуррента: см. модульный docstring.
    """
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe

    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)

    return {'status': 'cancelled'}
