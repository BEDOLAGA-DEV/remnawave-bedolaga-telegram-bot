"""
API роуты колеса удачи для администраторов.
"""

import math
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, require_permission
from app.cabinet.schemas.wheel import (
    AdminSpinItem,
    AdminSpinsResponse,
    AdminWheelConfigResponse,
    CreatePrizeRequest,
    ReorderPrizesRequest,
    UpdatePrizeRequest,
    UpdateWheelConfigRequest,
    WheelPrizeAdminResponse,
    WheelStatisticsResponse,
)
from app.database.crud.wheel import (
    create_wheel_prize,
    delete_wheel_prize,
    get_all_spins,
    get_or_create_wheel_config,
    get_wheel_prizes,
    reorder_wheel_prizes,
    update_wheel_config,
    update_wheel_prize,
)
from app.database.models import User
from app.services.wheel_service import wheel_service


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/wheel', tags=['Admin Fortune Wheel'])


@router.get('/config', response_model=AdminWheelConfigResponse)
async def get_admin_wheel_config(
    admin: User = Depends(require_permission('wheel:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить полную конфигурацию колеса."""
    config = await get_or_create_wheel_config(db)
    prizes = await get_wheel_prizes(db, config.id, active_only=False)

    prizes_response = [
        WheelPrizeAdminResponse(
            id=p.id,
            config_id=p.config_id,
            prize_type=p.prize_type,
            prize_value=p.prize_value,
            display_name=p.display_name,
            emoji=p.emoji,
            color=p.color,
            prize_value_kopeks=p.prize_value_kopeks,
            sort_order=p.sort_order,
            manual_probability=p.manual_probability,
            is_active=p.is_active,
            promo_balance_bonus_kopeks=p.promo_balance_bonus_kopeks or 0,
            promo_subscription_days=p.promo_subscription_days or 0,
            promo_traffic_gb=p.promo_traffic_gb or 0,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in prizes
    ]

    return AdminWheelConfigResponse(
        id=config.id,
        is_enabled=config.is_enabled,
        name=config.name,
        spin_cost_stars=config.spin_cost_stars,
        spin_cost_days=config.spin_cost_days,
        spin_cost_stars_enabled=config.spin_cost_stars_enabled,
        spin_cost_days_enabled=config.spin_cost_days_enabled,
        rtp_percent=config.rtp_percent,
        daily_spin_limit=config.daily_spin_limit,
        min_subscription_days_for_day_payment=config.min_subscription_days_for_day_payment,
        promo_prefix=config.promo_prefix,
        promo_validity_days=config.promo_validity_days,
        prizes=prizes_response,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.put('/config', response_model=AdminWheelConfigResponse)
async def update_admin_wheel_config(
    request: UpdateWheelConfigRequest,
    admin: User = Depends(require_permission('wheel:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Обновить конфигурацию колеса."""
    update_data = request.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No fields to update',
        )

    config = await update_wheel_config(db, **update_data)

    logger.info('🎡 Admin updated wheel config', telegram_id=admin.telegram_id, update_data=update_data)

    # Возвращаем полную конфигурацию
    prizes = await get_wheel_prizes(db, config.id, active_only=False)

    prizes_response = [
        WheelPrizeAdminResponse(
            id=p.id,
            config_id=p.config_id,
            prize_type=p.prize_type,
            prize_value=p.prize_value,
            display_name=p.display_name,
            emoji=p.emoji,
            color=p.color,
            prize_value_kopeks=p.prize_value_kopeks,
            sort_order=p.sort_order,
            manual_probability=p.manual_probability,
            is_active=p.is_active,
            promo_balance_bonus_kopeks=p.promo_balance_bonus_kopeks or 0,
            promo_subscription_days=p.promo_subscription_days or 0,
            promo_traffic_gb=p.promo_traffic_gb or 0,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in prizes
    ]

    return AdminWheelConfigResponse(
        id=config.id,
        is_enabled=config.is_enabled,
        name=config.name,
        spin_cost_stars=config.spin_cost_stars,
        spin_cost_days=config.spin_cost_days,
        spin_cost_stars_enabled=config.spin_cost_stars_enabled,
        spin_cost_days_enabled=config.spin_cost_days_enabled,
        rtp_percent=config.rtp_percent,
        daily_spin_limit=config.daily_spin_limit,
        min_subscription_days_for_day_payment=config.min_subscription_days_for_day_payment,
        promo_prefix=config.promo_prefix,
        promo_validity_days=config.promo_validity_days,
        prizes=prizes_response,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.get('/prizes', response_model=list[WheelPrizeAdminResponse])
async def get_prizes(
    admin: User = Depends(require_permission('wheel:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить список призов."""
    config = await get_or_create_wheel_config(db)
    prizes = await get_wheel_prizes(db, config.id, active_only=False)

    return [
        WheelPrizeAdminResponse(
            id=p.id,
            config_id=p.config_id,
            prize_type=p.prize_type,
            prize_value=p.prize_value,
            display_name=p.display_name,
            emoji=p.emoji,
            color=p.color,
            prize_value_kopeks=p.prize_value_kopeks,
            sort_order=p.sort_order,
            manual_probability=p.manual_probability,
            is_active=p.is_active,
            promo_balance_bonus_kopeks=p.promo_balance_bonus_kopeks or 0,
            promo_subscription_days=p.promo_subscription_days or 0,
            promo_traffic_gb=p.promo_traffic_gb or 0,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in prizes
    ]


@router.post('/prizes', response_model=WheelPrizeAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_prize(
    request: CreatePrizeRequest,
    admin: User = Depends(require_permission('wheel:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Создать новый приз."""
    config = await get_or_create_wheel_config(db)

    prize = await create_wheel_prize(
        db=db,
        config_id=config.id,
        prize_type=request.prize_type.value,
        prize_value=request.prize_value,
        display_name=request.display_name,
        prize_value_kopeks=request.prize_value_kopeks,
        emoji=request.emoji,
        color=request.color,
        sort_order=request.sort_order,
        manual_probability=request.manual_probability,
        is_active=request.is_active,
        promo_balance_bonus_kopeks=request.promo_balance_bonus_kopeks,
        promo_subscription_days=request.promo_subscription_days,
        promo_traffic_gb=request.promo_traffic_gb,
    )

    logger.info('🎁 Admin created prize', telegram_id=admin.telegram_id, display_name=prize.display_name)

    return WheelPrizeAdminResponse(
        id=prize.id,
        config_id=prize.config_id,
        prize_type=prize.prize_type,
        prize_value=prize.prize_value,
        display_name=prize.display_name,
        emoji=prize.emoji,
        color=prize.color,
        prize_value_kopeks=prize.prize_value_kopeks,
        sort_order=prize.sort_order,
        manual_probability=prize.manual_probability,
        is_active=prize.is_active,
        promo_balance_bonus_kopeks=prize.promo_balance_bonus_kopeks or 0,
        promo_subscription_days=prize.promo_subscription_days or 0,
        promo_traffic_gb=prize.promo_traffic_gb or 0,
        created_at=prize.created_at,
        updated_at=prize.updated_at,
    )


@router.put('/prizes/{prize_id}', response_model=WheelPrizeAdminResponse)
async def update_prize(
    prize_id: int,
    request: UpdatePrizeRequest,
    admin: User = Depends(require_permission('wheel:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Обновить приз."""
    update_data = request.model_dump(exclude_unset=True)

    # Конвертируем enum в строку если есть
    if update_data.get('prize_type'):
        update_data['prize_type'] = update_data['prize_type'].value

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No fields to update',
        )

    prize = await update_wheel_prize(db, prize_id, **update_data)

    if not prize:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Prize not found',
        )

    logger.info('🎁 Admin updated prize', telegram_id=admin.telegram_id, prize_id=prize_id, update_data=update_data)

    return WheelPrizeAdminResponse(
        id=prize.id,
        config_id=prize.config_id,
        prize_type=prize.prize_type,
        prize_value=prize.prize_value,
        display_name=prize.display_name,
        emoji=prize.emoji,
        color=prize.color,
        prize_value_kopeks=prize.prize_value_kopeks,
        sort_order=prize.sort_order,
        manual_probability=prize.manual_probability,
        is_active=prize.is_active,
        promo_balance_bonus_kopeks=prize.promo_balance_bonus_kopeks or 0,
        promo_subscription_days=prize.promo_subscription_days or 0,
        promo_traffic_gb=prize.promo_traffic_gb or 0,
        created_at=prize.created_at,
        updated_at=prize.updated_at,
    )


@router.delete('/prizes/{prize_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_prize_endpoint(
    prize_id: int,
    admin: User = Depends(require_permission('wheel:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Удалить приз."""
    success = await delete_wheel_prize(db, prize_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Prize not found',
        )

    logger.info('🗑️ Admin deleted prize', telegram_id=admin.telegram_id, prize_id=prize_id)


@router.post('/prizes/reorder', status_code=status.HTTP_200_OK)
async def reorder_prizes(
    request: ReorderPrizesRequest,
    admin: User = Depends(require_permission('wheel:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Переупорядочить призы."""
    await reorder_wheel_prizes(db, request.prize_ids)
    logger.info('🔄 Admin reordered prizes', telegram_id=admin.telegram_id, prize_ids=request.prize_ids)
    return {'success': True}


@router.get('/statistics', response_model=WheelStatisticsResponse)
async def get_statistics(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    admin: User = Depends(require_permission('wheel:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить статистику колеса."""
    stats = await wheel_service.get_statistics(db, date_from, date_to)

    return WheelStatisticsResponse(
        total_spins=stats['total_spins'],
        total_revenue_kopeks=stats['total_revenue_kopeks'],
        total_payout_kopeks=stats['total_payout_kopeks'],
        actual_rtp_percent=stats['actual_rtp_percent'],
        configured_rtp_percent=stats['configured_rtp_percent'],
        spins_by_payment_type=stats['spins_by_payment_type'],
        prizes_distribution=stats['prizes_distribution'],
        top_wins=stats['top_wins'],
        period_from=stats['period_from'],
        period_to=stats['period_to'],
    )


@router.get('/spins', response_model=AdminSpinsResponse)
async def get_all_spins_endpoint(
    user_id: int | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('wheel:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Получить все спины с фильтрами."""
    offset = (page - 1) * per_page
    spins, total = await get_all_spins(
        db,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        limit=per_page,
        offset=offset,
    )

    items = [
        AdminSpinItem(
            id=spin.id,
            user_id=spin.user_id,
            username=spin.user.username if spin.user else None,
            payment_type=spin.payment_type,
            payment_amount=spin.payment_amount,
            payment_value_kopeks=spin.payment_value_kopeks,
            prize_type=spin.prize_type,
            prize_value=spin.prize_value,
            prize_display_name=spin.prize_display_name,
            prize_value_kopeks=spin.prize_value_kopeks,
            is_applied=spin.is_applied,
            created_at=spin.created_at,
        )
        for spin in spins
    ]

    pages = math.ceil(total / per_page) if total > 0 else 1

    return AdminSpinsResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )
