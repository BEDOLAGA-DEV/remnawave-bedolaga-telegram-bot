"""Admin routes for managing tariffs in cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import (
    create_tariff,
    delete_tariff,
    get_all_tariffs,
    get_tariff_by_id,
    get_tariff_subscriptions_count,
    load_period_prices_from_db,
    reorder_tariffs,
    set_tariff_promo_groups,
    update_tariff,
)
from app.database.models import PromoGroup, Subscription, Tariff, Transaction, TransactionType, User

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.tariffs import (
    PeriodPrice,
    PromoGroupInfo,
    ServerInfo,
    ServerTrafficLimit,
    TariffCreateRequest,
    TariffDetailResponse,
    TariffListItem,
    TariffListResponse,
    TariffSortOrderRequest,
    TariffStatsResponse,
    TariffToggleResponse,
    TariffTrialResponse,
    TariffUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tariffs', tags=['Cabinet Admin Tariffs'])


async def _get_tariff_servers(
    db: AsyncSession, allowed_squads: list[str], server_traffic_limits: dict = None
) -> list[ServerInfo]:
    """Get server info for tariff."""
    servers, _ = await get_all_server_squads(db, available_only=False)
    limits = server_traffic_limits or {}
    result = []
    for server in servers:
        # Получаем индивидуальный лимит трафика для сервера
        server_limit = None
        if server.squad_uuid in limits:
            limit_data = limits[server.squad_uuid]
            if isinstance(limit_data, dict) and 'traffic_limit_gb' in limit_data:
                server_limit = limit_data['traffic_limit_gb']
            elif isinstance(limit_data, int):
                server_limit = limit_data

        result.append(
            ServerInfo(
                id=server.id,
                squad_uuid=server.squad_uuid,
                display_name=server.display_name,
                country_code=server.country_code,
                is_selected=server.squad_uuid in allowed_squads,
                traffic_limit_gb=server_limit,
            )
        )
    return result


async def _get_tariff_promo_groups(db: AsyncSession, tariff: Tariff) -> list[PromoGroupInfo]:
    """Get promo group info for tariff."""
    result = await db.execute(select(PromoGroup).order_by(PromoGroup.name))
    all_groups = result.scalars().all()

    selected_ids = {pg.id for pg in tariff.allowed_promo_groups} if tariff.allowed_promo_groups else set()

    return [
        PromoGroupInfo(
            id=pg.id,
            name=pg.name,
            is_selected=pg.id in selected_ids,
        )
        for pg in all_groups
    ]


def _period_prices_to_list(period_prices: dict) -> list[PeriodPrice]:
    """Convert period_prices dict to list."""
    if not period_prices:
        return []
    return [
        PeriodPrice(days=int(days), price_kopeks=price)
        for days, price in sorted(period_prices.items(), key=lambda x: int(x[0]))
    ]


def _period_prices_to_dict(period_prices: list[PeriodPrice]) -> dict:
    """Convert period_prices list to dict."""
    return {str(pp.days): pp.price_kopeks for pp in period_prices}


@router.get('', response_model=TariffListResponse)
async def list_tariffs(
    include_inactive: bool = True,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of all tariffs."""
    tariffs = await get_all_tariffs(db, include_inactive=include_inactive)

    items = []
    for tariff in tariffs:
        subs_count = await get_tariff_subscriptions_count(db, tariff.id)
        items.append(
            TariffListItem(
                id=tariff.id,
                name=tariff.name,
                description=tariff.description,
                is_active=tariff.is_active,
                is_trial_available=tariff.is_trial_available,
                is_daily=tariff.is_daily,
                daily_price_kopeks=tariff.daily_price_kopeks,
                allow_traffic_topup=tariff.allow_traffic_topup,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                tier_level=tariff.tier_level,
                display_order=tariff.display_order,
                servers_count=len(tariff.allowed_squads or []),
                subscriptions_count=subs_count,
                created_at=tariff.created_at,
            )
        )

    return TariffListResponse(tariffs=items, total=len(items))


@router.get('/available-servers', response_model=list[ServerInfo])
async def get_available_servers(
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of all servers for tariff selection."""
    servers, _ = await get_all_server_squads(db, available_only=False)
    return [
        ServerInfo(
            id=server.id,
            squad_uuid=server.squad_uuid,
            display_name=server.display_name,
            country_code=server.country_code,
            is_selected=False,
        )
        for server in servers
    ]


@router.put('/order')
async def update_tariff_order(
    request: TariffSortOrderRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update the display order of tariffs."""
    await reorder_tariffs(db, request.tariff_ids)
    await db.commit()

    logger.info('Admin updated tariff order', admin_id=admin.id, tariff_ids=request.tariff_ids)

    return {'message': 'Tariff order updated successfully'}


@router.get('/{tariff_id}', response_model=TariffDetailResponse)
async def get_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed tariff info."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    allowed_squads = tariff.allowed_squads or []
    server_traffic_limits = tariff.server_traffic_limits or {}
    servers = await _get_tariff_servers(db, allowed_squads, server_traffic_limits)
    promo_groups = await _get_tariff_promo_groups(db, tariff)
    subs_count = await get_tariff_subscriptions_count(db, tariff.id)

    # Преобразуем server_traffic_limits в формат для схемы
    server_limits_response = {}
    for uuid, limit_data in server_traffic_limits.items():
        if isinstance(limit_data, dict):
            server_limits_response[uuid] = ServerTrafficLimit(**limit_data)
        elif isinstance(limit_data, int):
            server_limits_response[uuid] = ServerTrafficLimit(traffic_limit_gb=limit_data)

    return TariffDetailResponse(
        id=tariff.id,
        name=tariff.name,
        description=tariff.description,
        is_active=tariff.is_active,
        is_trial_available=tariff.is_trial_available,
        allow_traffic_topup=tariff.allow_traffic_topup,
        traffic_topup_enabled=tariff.traffic_topup_enabled,
        traffic_topup_packages=tariff.traffic_topup_packages or {},
        max_topup_traffic_gb=tariff.max_topup_traffic_gb,
        traffic_limit_gb=tariff.traffic_limit_gb,
        device_limit=tariff.device_limit,
        device_price_kopeks=tariff.device_price_kopeks,
        max_device_limit=tariff.max_device_limit,
        tier_level=tariff.tier_level,
        display_order=tariff.display_order,
        period_prices=_period_prices_to_list(tariff.period_prices),
        allowed_squads=allowed_squads,
        server_traffic_limits=server_limits_response,
        servers=servers,
        promo_groups=promo_groups,
        subscriptions_count=subs_count,
        # Произвольное количество дней
        custom_days_enabled=tariff.custom_days_enabled,
        price_per_day_kopeks=tariff.price_per_day_kopeks,
        min_days=tariff.min_days,
        max_days=tariff.max_days,
        # Произвольный трафик при покупке
        custom_traffic_enabled=tariff.custom_traffic_enabled,
        traffic_price_per_gb_kopeks=tariff.traffic_price_per_gb_kopeks,
        min_traffic_gb=tariff.min_traffic_gb,
        max_traffic_gb=tariff.max_traffic_gb,
        # Дневной тариф
        is_daily=tariff.is_daily,
        daily_price_kopeks=tariff.daily_price_kopeks,
        # Режим сброса трафика
        traffic_reset_mode=tariff.traffic_reset_mode,
        created_at=tariff.created_at,
        updated_at=tariff.updated_at,
    )


@router.post('', response_model=TariffDetailResponse)
async def create_new_tariff(
    request: TariffCreateRequest,
    admin: User = Depends(require_permission('tariffs:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new tariff."""
    period_prices_dict = _period_prices_to_dict(request.period_prices)

    # Преобразуем ServerTrafficLimit в dict для хранения
    server_limits_dict = (
        {uuid: limit.model_dump() for uuid, limit in request.server_traffic_limits.items()}
        if request.server_traffic_limits
        else {}
    )

    tariff = await create_tariff(
        db=db,
        name=request.name,
        description=request.description,
        is_active=request.is_active,
        allow_traffic_topup=request.allow_traffic_topup,
        traffic_topup_enabled=request.traffic_topup_enabled,
        traffic_topup_packages=request.traffic_topup_packages,
        max_topup_traffic_gb=request.max_topup_traffic_gb,
        traffic_limit_gb=request.traffic_limit_gb,
        device_limit=request.device_limit,
        device_price_kopeks=request.device_price_kopeks,
        max_device_limit=request.max_device_limit,
        tier_level=request.tier_level,
        period_prices=period_prices_dict,
        allowed_squads=request.allowed_squads,
        server_traffic_limits=server_limits_dict,
        promo_group_ids=request.promo_group_ids if request.promo_group_ids else None,
        # Произвольное количество дней
        custom_days_enabled=request.custom_days_enabled,
        price_per_day_kopeks=request.price_per_day_kopeks,
        min_days=request.min_days,
        max_days=request.max_days,
        # Произвольный трафик при покупке
        custom_traffic_enabled=request.custom_traffic_enabled,
        traffic_price_per_gb_kopeks=request.traffic_price_per_gb_kopeks,
        min_traffic_gb=request.min_traffic_gb,
        max_traffic_gb=request.max_traffic_gb,
        # Дневной тариф
        is_daily=request.is_daily,
        daily_price_kopeks=request.daily_price_kopeks,
        # Режим сброса трафика
        traffic_reset_mode=request.traffic_reset_mode,
    )

    logger.info('Admin created tariff', admin_id=admin.id, tariff_id=tariff.id, tariff_name=tariff.name)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    # Return full detail
    return await get_tariff(tariff.id, admin, db)


@router.put('/{tariff_id}', response_model=TariffDetailResponse)
async def update_existing_tariff(
    tariff_id: int,
    request: TariffUpdateRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing tariff."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    # Build updates dict
    updates = {}
    if request.name is not None:
        updates['name'] = request.name
    if request.description is not None:
        updates['description'] = request.description
    if request.is_active is not None:
        updates['is_active'] = request.is_active
    if request.allow_traffic_topup is not None:
        updates['allow_traffic_topup'] = request.allow_traffic_topup
    if request.traffic_topup_enabled is not None:
        updates['traffic_topup_enabled'] = request.traffic_topup_enabled
    if request.traffic_topup_packages is not None:
        updates['traffic_topup_packages'] = request.traffic_topup_packages
    if request.max_topup_traffic_gb is not None:
        updates['max_topup_traffic_gb'] = request.max_topup_traffic_gb
    if request.traffic_limit_gb is not None:
        updates['traffic_limit_gb'] = request.traffic_limit_gb
    if request.device_limit is not None:
        updates['device_limit'] = request.device_limit
    if request.device_price_kopeks is not None:
        updates['device_price_kopeks'] = request.device_price_kopeks
    if request.max_device_limit is not None:
        updates['max_device_limit'] = request.max_device_limit
    if request.tier_level is not None:
        updates['tier_level'] = request.tier_level
    if request.display_order is not None:
        updates['display_order'] = request.display_order
    if request.period_prices is not None:
        updates['period_prices'] = _period_prices_to_dict(request.period_prices)
    if request.allowed_squads is not None:
        updates['allowed_squads'] = request.allowed_squads
    if request.server_traffic_limits is not None:
        # Преобразуем ServerTrafficLimit в dict для хранения
        updates['server_traffic_limits'] = {
            uuid: limit.model_dump() for uuid, limit in request.server_traffic_limits.items()
        }
    # Произвольное количество дней
    if request.custom_days_enabled is not None:
        updates['custom_days_enabled'] = request.custom_days_enabled
    if request.price_per_day_kopeks is not None:
        updates['price_per_day_kopeks'] = request.price_per_day_kopeks
    if request.min_days is not None:
        updates['min_days'] = request.min_days
    if request.max_days is not None:
        updates['max_days'] = request.max_days
    # Произвольный трафик при покупке
    if request.custom_traffic_enabled is not None:
        updates['custom_traffic_enabled'] = request.custom_traffic_enabled
    if request.traffic_price_per_gb_kopeks is not None:
        updates['traffic_price_per_gb_kopeks'] = request.traffic_price_per_gb_kopeks
    if request.min_traffic_gb is not None:
        updates['min_traffic_gb'] = request.min_traffic_gb
    if request.max_traffic_gb is not None:
        updates['max_traffic_gb'] = request.max_traffic_gb
    # Дневной тариф
    if request.is_daily is not None:
        updates['is_daily'] = request.is_daily
    if request.daily_price_kopeks is not None:
        updates['daily_price_kopeks'] = request.daily_price_kopeks
    # Режим сброса трафика (None допускается как значение для сброса к глобальной настройке)
    if 'traffic_reset_mode' in request.model_fields_set:
        updates['traffic_reset_mode'] = request.traffic_reset_mode

    if updates:
        await update_tariff(db, tariff, **updates)

    # Update promo groups separately
    if request.promo_group_ids is not None:
        await set_tariff_promo_groups(db, tariff, request.promo_group_ids)

    logger.info('Admin updated tariff', admin_id=admin.id, tariff_id=tariff_id)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    return await get_tariff(tariff_id, admin, db)


@router.delete('/{tariff_id}')
async def delete_existing_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a tariff."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)
    await delete_tariff(db, tariff)
    logger.info(
        'Admin deleted tariff (affected subscriptions: )',
        admin_id=admin.id,
        tariff_id=tariff_id,
        tariff_name=tariff.name,
        subs_count=subs_count,
    )

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    return {'message': 'Tariff deleted successfully', 'affected_subscriptions': subs_count}


@router.post('/{tariff_id}/toggle', response_model=TariffToggleResponse)
async def toggle_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle tariff active status."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    new_status = not tariff.is_active
    await update_tariff(db, tariff, is_active=new_status)

    status_text = 'activated' if new_status else 'deactivated'
    logger.info('Admin tariff', admin_id=admin.id, status_text=status_text, tariff_id=tariff_id)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    return TariffToggleResponse(
        id=tariff_id,
        is_active=new_status,
        message=f'Tariff {status_text}',
    )


@router.post('/{tariff_id}/trial', response_model=TariffTrialResponse)
async def toggle_trial_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle tariff trial availability.

    When enabling trial on a tariff, removes trial flag from all other tariffs
    (only one tariff can be the trial tariff at a time).
    """
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    new_status = not tariff.is_trial_available

    if new_status:
        # При включении триала - снимаем флаг со ВСЕХ тарифов, затем ставим на текущий
        # Это гарантирует, что триальным будет только один тариф
        await db.execute(Tariff.__table__.update().values(is_trial_available=False))
        await db.commit()
        # Обновляем объект тарифа после массового обновления
        await db.refresh(tariff)

    await update_tariff(db, tariff, is_trial_available=new_status)

    status_text = 'set as trial' if new_status else 'removed from trial'
    logger.info('Admin tariff', admin_id=admin.id, status_text=status_text, tariff_id=tariff_id)

    return TariffTrialResponse(
        id=tariff_id,
        is_trial_available=new_status,
        message=f'Tariff {status_text}',
    )


@router.get('/{tariff_id}/stats', response_model=TariffStatsResponse)
async def get_tariff_stats(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get tariff statistics."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    # Count subscriptions
    total_result = await db.execute(select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id))
    total_count = total_result.scalar() or 0

    # Count active subscriptions
    active_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff_id,
            Subscription.status == 'active',
        )
    )
    active_count = active_result.scalar() or 0

    # Count trial subscriptions
    trial_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff_id,
            Subscription.is_trial == True,
        )
    )
    trial_count = trial_result.scalar() or 0

    # Calculate revenue from subscription payments for users on this tariff
    revenue_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0))
        .join(Subscription, Transaction.user_id == Subscription.user_id)
        .where(
            Subscription.tariff_id == tariff_id,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed == True,
        )
    )
    revenue_kopeks = revenue_result.scalar() or 0

    return TariffStatsResponse(
        id=tariff_id,
        name=tariff.name,
        subscriptions_count=total_count,
        active_subscriptions=active_count,
        trial_subscriptions=trial_count,
        revenue_kopeks=revenue_kopeks,
        revenue_rubles=revenue_kopeks / 100,
    )
