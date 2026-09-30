"""Cashera auto-renewal endpoints (cabinet, user-facing).

POST /subscription/cashera-recurrent/enable
GET  /subscription/cashera-recurrent
POST /subscription/cashera-recurrent/cancel

Зеркало ``lava_recurrent.py`` / ``platega_recurrent.py``. Enable/get гейтятся
``settings.is_cashera_recurrent_enabled()``; cancel — намеренно НЕ гейтится
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


@router.post('/cashera-recurrent/enable')
async def enable_cashera_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Включает автопродление Cashera для выбранной подписки."""
    from app.config import settings

    if not settings.is_cashera_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Cashera recurrent disabled')

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

    from app.services.payment.cashera import enable_cashera_recurring

    try:
        result = await enable_cashera_recurring(
            db,
            user_id=user.id,
            subscription=subscription,
            tariff=tariff,
        )
    except ValueError as error:
        # Нет цены за период и т. п. — причина человекочитаемая, отдаём как есть.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        logger.warning('Cashera recurrent enable failed', error=str(error), user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Could not create Cashera subscription',
        ) from error

    return {'status': result['status'], 'redirect_url': result['redirect_url']}


@router.post('/cashera-recurrent/purchase')
async def purchase_with_cashera_recurrent(
    tariff_id: int = Query(..., description='Tariff to subscribe to'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Оформление подписки на тариф оплатой привязкой Cashera.

    Альтернатива покупке с баланса: клиент подтверждает подписку по ссылке из
    ответа, первое списание оживляет подписку (для нового тарифа создаётся
    неактивная заготовка).
    """
    from app.config import settings

    if not settings.is_cashera_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Cashera recurrent disabled')

    from app.database.crud.tariff import get_tariff_by_id

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff or not getattr(tariff, 'is_active', False):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff not found')

    from app.services.payment.cashera import purchase_tariff_with_cashera_recurring

    try:
        result = await purchase_tariff_with_cashera_recurring(db, user=user, tariff=tariff)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except Exception as error:
        logger.warning('Cashera recurrent purchase failed', error=str(error), user_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Could not create Cashera subscription',
        ) from error

    return {
        'status': result['status'],
        'redirect_url': result['redirect_url'],
        'subscription_id': result['subscription_id'],
    }


@router.get('/cashera-recurrent')
async def get_cashera_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Текущее состояние автопродления Cashera для подписки."""
    from app.config import settings

    if not settings.is_cashera_recurrent_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Cashera recurrent disabled')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.services.cashera_recurring_cancel import get_cashera_recurring_status

    state = await get_cashera_recurring_status(db, subscription.id)
    if not state:
        return {'status': 'none'}

    return {
        'status': state['status'],
        'charge_days': state['charge_days'],
        'interval': state['interval'],
        'amount_kopeks': state['amount_kopeks'],
        'next_charge_at': state['next_charge_at'].isoformat() if state['next_charge_at'] else None,
        'redirect_url': state['redirect_url'],
    }


@router.post('/cashera-recurrent/cancel')
async def cancel_cashera_recurrent(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    """Отменяет автопродление Cashera (best-effort).

    НЕ гейтится флагом рекуррента: см. модульный docstring.
    """
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    from app.services.cashera_recurring_cancel import cancel_cashera_recurring_for_subscription_safe

    await cancel_cashera_recurring_for_subscription_safe(db, subscription.id)

    return {'status': 'cancelled'}
