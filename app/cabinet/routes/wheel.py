"""
API роуты колеса удачи для пользователей.
"""

import math
import time

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.schemas.wheel import (
    SpinAvailabilityResponse,
    SpinHistoryItem,
    SpinHistoryResponse,
    SpinRequest,
    SpinResultResponse,
    WheelConfigResponse,
    WheelPrizeDisplay,
)
from app.config import settings
from app.database.crud.wheel import (
    get_or_create_wheel_config,
    get_user_spin_history,
    get_user_spins_today,
    get_wheel_prizes,
)
from app.database.models import User
from app.services.wheel_service import wheel_service


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/wheel', tags=['Fortune Wheel'])


@router.get('/config', response_model=WheelConfigResponse)
async def get_wheel_config(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить конфигурацию колеса удачи."""
    config = await get_or_create_wheel_config(db)
    prizes = await get_wheel_prizes(db, config.id, active_only=True)
    spins_today = await get_user_spins_today(db, user.id)

    # Проверяем доступность
    availability = await wheel_service.check_availability(db, user)

    # Проверяем наличие подписки
    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user.id)
    has_subscription = subscription is not None and subscription.is_active

    prizes_display = [
        WheelPrizeDisplay(
            id=p.id,
            display_name=p.display_name,
            emoji=p.emoji,
            color=p.color,
            prize_type=p.prize_type,
        )
        for p in prizes
    ]

    return WheelConfigResponse(
        is_enabled=config.is_enabled,
        name=config.name,
        spin_cost_stars=config.spin_cost_stars if config.spin_cost_stars_enabled else None,
        spin_cost_days=config.spin_cost_days if config.spin_cost_days_enabled else None,
        spin_cost_stars_enabled=config.spin_cost_stars_enabled,
        spin_cost_days_enabled=config.spin_cost_days_enabled,
        prizes=prizes_display,
        daily_limit=config.daily_spin_limit,
        user_spins_today=spins_today,
        can_spin=availability.can_spin,
        can_spin_reason=availability.reason,
        can_pay_stars=availability.can_pay_stars,
        can_pay_days=availability.can_pay_days,
        user_balance_kopeks=availability.user_balance_kopeks,
        required_balance_kopeks=availability.required_balance_kopeks,
        has_subscription=has_subscription,
    )


@router.get('/availability', response_model=SpinAvailabilityResponse)
async def check_spin_availability(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Проверить доступность спина."""
    availability = await wheel_service.check_availability(db, user)

    return SpinAvailabilityResponse(
        can_spin=availability.can_spin,
        reason=availability.reason,
        spins_remaining_today=availability.spins_remaining_today,
        can_pay_stars=availability.can_pay_stars,
        can_pay_days=availability.can_pay_days,
        min_subscription_days=availability.min_subscription_days,
        user_subscription_days=availability.user_subscription_days,
        user_balance_kopeks=availability.user_balance_kopeks,
        required_balance_kopeks=availability.required_balance_kopeks,
    )


@router.post('/spin', response_model=SpinResultResponse)
async def spin_wheel(
    request: SpinRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Крутить колесо удачи."""
    result = await wheel_service.spin(db, user, request.payment_type.value)

    if not result.success:
        # Возвращаем ошибку в теле ответа, а не HTTP exception
        return SpinResultResponse(
            success=False,
            error=result.error,
            message=result.message,
        )

    return SpinResultResponse(
        success=True,
        prize_id=result.prize_id,
        prize_type=result.prize_type,
        prize_value=result.prize_value,
        prize_display_name=result.prize_display_name,
        emoji=result.emoji,
        color=result.color,
        rotation_degrees=result.rotation_degrees,
        message=result.message,
        promocode=result.promocode,
    )


@router.get('/history', response_model=SpinHistoryResponse)
async def get_spin_history(
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить историю спинов пользователя."""
    page = max(page, 1)
    if per_page < 1 or per_page > 100:
        per_page = 20

    offset = (page - 1) * per_page

    spins, total = await get_user_spin_history(db, user.id, limit=per_page, offset=offset)

    items = []
    for spin in spins:
        # Получаем emoji и color из приза, если он есть
        emoji = '🎁'
        color = '#3B82F6'
        if spin.prize:
            emoji = spin.prize.emoji
            color = spin.prize.color

        items.append(
            SpinHistoryItem(
                id=spin.id,
                payment_type=spin.payment_type,
                payment_amount=spin.payment_amount,
                prize_type=spin.prize_type,
                prize_value=spin.prize_value,
                prize_display_name=spin.prize_display_name,
                emoji=emoji,
                color=color,
                prize_value_kopeks=spin.prize_value_kopeks,
                created_at=spin.created_at,
            )
        )

    pages = math.ceil(total / per_page) if total > 0 else 1

    return SpinHistoryResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


class StarsInvoiceResponse(BaseModel):
    """Ответ с ссылкой на Stars invoice."""

    invoice_url: str
    stars_amount: int


@router.post('/stars-invoice', response_model=StarsInvoiceResponse)
async def create_stars_invoice(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Создать Telegram Stars invoice для оплаты спина колеса.
    Используется в Telegram Mini App для прямой оплаты Stars.
    """
    config = await get_or_create_wheel_config(db)

    if not config.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Колесо удачи недоступно',
        )

    if not config.spin_cost_stars_enabled or not config.spin_cost_stars:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Оплата Stars не включена',
        )

    # Проверяем наличие активной подписки
    from app.database.crud.subscription import get_subscription_by_user_id

    subscription = await get_subscription_by_user_id(db, user.id)
    if not subscription or not subscription.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Для использования колеса необходима активная подписка',
        )

    # Проверяем лимит спинов
    spins_today = await get_user_spins_today(db, user.id)
    if config.daily_spin_limit > 0 and spins_today >= config.daily_spin_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Достигнут дневной лимит спинов',
        )

    # Проверяем наличие призов
    prizes = await get_wheel_prizes(db, config.id, active_only=True)
    if not prizes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Призы не настроены',
        )

    stars_amount = config.spin_cost_stars
    payload = f'wheel_spin_{user.id}_{int(time.time())}'

    # Создаем invoice через Telegram Bot API
    try:
        bot_token = settings.BOT_TOKEN
        api_url = f'https://api.telegram.org/bot{bot_token}/createInvoiceLink'

        async with httpx.AsyncClient() as client:
            response = await client.post(
                api_url,
                json={
                    'title': 'Колесо удачи',
                    'description': f'Спин колеса удачи ({stars_amount} ⭐)',
                    'payload': payload,
                    'provider_token': '',  # Пустой для Stars
                    'currency': 'XTR',
                    'prices': [{'label': 'Спин колеса', 'amount': stars_amount}],
                },
            )

            result = response.json()

            if not result.get('ok'):
                logger.error('Telegram API error', result=result)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail='Ошибка создания инвойса',
                )

            invoice_url = result['result']
            logger.info(
                'Created Stars invoice for wheel spin: user=, stars', user_id=user.id, stars_amount=stars_amount
            )

            return StarsInvoiceResponse(
                invoice_url=invoice_url,
                stars_amount=stars_amount,
            )

    except httpx.HTTPError as e:
        logger.error('HTTP error creating invoice', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Ошибка соединения с Telegram',
        )
