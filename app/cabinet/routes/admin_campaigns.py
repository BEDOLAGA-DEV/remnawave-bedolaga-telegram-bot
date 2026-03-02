"""Admin routes for managing advertising campaigns in cabinet."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.links import get_campaign_deep_link, get_campaign_web_link
from app.database.crud.campaign import (
    create_campaign,
    delete_campaign,
    get_campaign_by_id,
    get_campaign_by_start_parameter,
    get_campaign_statistics,
    get_campaigns_count,
    get_campaigns_list,
    get_campaigns_overview,
    update_campaign,
)
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import get_all_tariffs
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    PartnerStatus,
    Subscription,
    Tariff,
    User,
)
from app.services.partner_stats_service import PartnerStatsService

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.campaigns import (
    AdminCampaignChartDataResponse,
    AvailablePartnerItem,
    CampaignCreateRequest,
    CampaignDetailResponse,
    CampaignListItem,
    CampaignListResponse,
    CampaignRegistrationItem,
    CampaignRegistrationsResponse,
    CampaignsOverviewResponse,
    CampaignStatisticsResponse,
    CampaignToggleResponse,
    CampaignUpdateRequest,
    ServerSquadInfo,
    TariffInfo,
)
from ..schemas.tariffs import TariffListItem


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/campaigns', tags=['Cabinet Admin Campaigns'])


def _safe_div(value: float | None, divisor: int = 100) -> float:
    """Safely divide kopeks to rubles, handling None values."""
    return (value or 0) / divisor


def _get_partner_name(campaign: AdvertisingCampaign) -> str | None:
    """Get partner display name from campaign."""
    if not campaign.partner_user_id or not campaign.partner:
        return None
    partner = campaign.partner
    return partner.first_name or partner.username or f'#{partner.id}'


@router.get('/overview', response_model=CampaignsOverviewResponse)
async def get_overview(
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get campaigns overview statistics."""
    try:
        overview = await get_campaigns_overview(db)

        # Count tariff bonuses
        tariff_result = await db.execute(
            select(func.count(AdvertisingCampaignRegistration.id)).where(
                AdvertisingCampaignRegistration.bonus_type == 'tariff'
            )
        )
        tariff_count = tariff_result.scalar() or 0

        return CampaignsOverviewResponse(
            total=overview['total'],
            active=overview['active'],
            inactive=overview['inactive'],
            total_registrations=overview['registrations'],
            total_balance_issued_kopeks=overview['balance_total'],
            total_balance_issued_rubles=_safe_div(overview['balance_total']),
            total_subscription_issued=overview['subscription_total'],
            total_tariff_issued=tariff_count,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get campaigns overview', error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load campaigns overview',
        )


@router.get('/available-servers', response_model=list[ServerSquadInfo])
async def get_available_servers(
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of available server squads for campaign subscription bonus."""
    servers, _ = await get_all_server_squads(db, available_only=False)
    return [
        ServerSquadInfo(
            id=server.id,
            squad_uuid=server.squad_uuid,
            display_name=server.display_name,
            country_code=server.country_code,
        )
        for server in servers
    ]


@router.get('/available-tariffs', response_model=list[TariffListItem])
async def get_available_tariffs(
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of available tariffs for campaign tariff bonus."""
    tariffs = await get_all_tariffs(db, include_inactive=False)
    return [
        TariffListItem(
            id=tariff.id,
            name=tariff.name,
            description=tariff.description,
            is_active=tariff.is_active,
            is_trial_available=tariff.is_trial_available,
            is_daily=tariff.is_daily,
            daily_price_kopeks=tariff.daily_price_kopeks or 0,
            allow_traffic_topup=tariff.allow_traffic_topup,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            tier_level=tariff.tier_level,
            display_order=tariff.display_order,
            servers_count=len(tariff.allowed_squads or []),
            subscriptions_count=0,
            created_at=tariff.created_at,
        )
        for tariff in tariffs
    ]


@router.get('/available-partners', response_model=list[AvailablePartnerItem])
async def get_available_partners(
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of approved partners for campaign partner selector."""
    result = await db.execute(
        select(User).where(User.partner_status == PartnerStatus.APPROVED.value).order_by(User.first_name, User.username)
    )
    partners = result.scalars().all()
    return [
        AvailablePartnerItem(
            user_id=p.id,
            username=p.username,
            first_name=p.first_name,
        )
        for p in partners
    ]


@router.get('', response_model=CampaignListResponse)
async def list_campaigns(
    include_inactive: bool = True,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of all campaigns."""
    campaigns = await get_campaigns_list(db, offset=offset, limit=limit, include_inactive=include_inactive)
    total = await get_campaigns_count(db, is_active=True if not include_inactive else None)

    items = []
    for campaign in campaigns:
        # Get quick stats
        stats = await get_campaign_statistics(db, campaign.id)
        items.append(
            CampaignListItem(
                id=campaign.id,
                name=campaign.name,
                start_parameter=campaign.start_parameter,
                bonus_type=campaign.bonus_type,
                is_active=campaign.is_active,
                registrations_count=stats['registrations'],
                total_revenue_kopeks=stats['total_revenue_kopeks'],
                conversion_rate=stats['conversion_rate'],
                partner_user_id=campaign.partner_user_id,
                partner_name=_get_partner_name(campaign),
                created_at=campaign.created_at,
            )
        )

    return CampaignListResponse(campaigns=items, total=total)


@router.get('/{campaign_id}', response_model=CampaignDetailResponse)
async def get_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed campaign info."""
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    tariff_info = None
    if campaign.tariff:
        tariff_info = TariffInfo(
            id=campaign.tariff.id,
            name=campaign.tariff.name,
        )

    return CampaignDetailResponse(
        id=campaign.id,
        name=campaign.name,
        start_parameter=campaign.start_parameter,
        bonus_type=campaign.bonus_type,
        is_active=campaign.is_active,
        balance_bonus_kopeks=campaign.balance_bonus_kopeks or 0,
        balance_bonus_rubles=_safe_div(campaign.balance_bonus_kopeks),
        subscription_duration_days=campaign.subscription_duration_days,
        subscription_traffic_gb=campaign.subscription_traffic_gb,
        subscription_device_limit=campaign.subscription_device_limit,
        subscription_squads=campaign.subscription_squads or [],
        tariff_id=campaign.tariff_id,
        tariff_duration_days=campaign.tariff_duration_days,
        tariff=tariff_info,
        partner_user_id=campaign.partner_user_id,
        partner_name=_get_partner_name(campaign),
        created_by=campaign.created_by,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        deep_link=get_campaign_deep_link(campaign.start_parameter),
        web_link=get_campaign_web_link(campaign.start_parameter),
    )


@router.get('/{campaign_id}/chart-data', response_model=AdminCampaignChartDataResponse)
async def get_campaign_chart_data(
    campaign_id: int,
    admin: User = Depends(require_permission('campaigns:stats')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get chart data for admin campaign analytics."""
    try:
        campaign = await get_campaign_by_id(db, campaign_id)
        if not campaign:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Campaign not found',
            )

        data = await PartnerStatsService.get_admin_campaign_chart_data(db, campaign_id)
        return AdminCampaignChartDataResponse(**data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get campaign chart data', error=str(e), campaign_id=campaign_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load campaign chart data',
        )


@router.get('/{campaign_id}/stats', response_model=CampaignStatisticsResponse)
async def get_campaign_stats(
    campaign_id: int,
    admin: User = Depends(require_permission('campaigns:stats')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed campaign statistics."""
    try:
        campaign = await get_campaign_by_id(db, campaign_id)
        if not campaign:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Campaign not found',
            )

        stats = await get_campaign_statistics(db, campaign_id)

        return CampaignStatisticsResponse(
            id=campaign.id,
            name=campaign.name,
            start_parameter=campaign.start_parameter,
            bonus_type=campaign.bonus_type,
            is_active=campaign.is_active,
            registrations=stats['registrations'],
            balance_issued_kopeks=stats['balance_issued'],
            balance_issued_rubles=_safe_div(stats['balance_issued']),
            subscription_issued=stats['subscription_issued'],
            last_registration=stats['last_registration'],
            total_revenue_kopeks=stats['total_revenue_kopeks'],
            total_revenue_rubles=_safe_div(stats['total_revenue_kopeks']),
            avg_revenue_per_user_kopeks=stats['avg_revenue_per_user_kopeks'],
            avg_revenue_per_user_rubles=_safe_div(stats['avg_revenue_per_user_kopeks']),
            avg_first_payment_kopeks=stats['avg_first_payment_kopeks'],
            avg_first_payment_rubles=_safe_div(stats['avg_first_payment_kopeks']),
            trial_users_count=stats['trial_users_count'],
            active_trials_count=stats['active_trials_count'],
            conversion_count=stats['conversion_count'],
            paid_users_count=stats['paid_users_count'],
            conversion_rate=stats['conversion_rate'],
            trial_conversion_rate=stats['trial_conversion_rate'],
            deep_link=get_campaign_deep_link(campaign.start_parameter),
            web_link=get_campaign_web_link(campaign.start_parameter),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get campaign stats', error=str(e), campaign_id=campaign_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load campaign statistics',
        )


@router.get('/{campaign_id}/registrations', response_model=CampaignRegistrationsResponse)
async def get_campaign_registrations(
    campaign_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('campaigns:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of users registered through campaign."""
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    offset = (page - 1) * per_page

    # Get registrations with user info
    result = await db.execute(
        select(AdvertisingCampaignRegistration, User)
        .join(User, AdvertisingCampaignRegistration.user_id == User.id)
        .where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
        .order_by(AdvertisingCampaignRegistration.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    rows = result.all()

    # Count total
    count_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            AdvertisingCampaignRegistration.campaign_id == campaign_id
        )
    )
    total = count_result.scalar() or 0

    # Batch query: find which users have active subscriptions (avoids N+1)
    user_ids = [user.id for _reg, user in rows]
    active_sub_user_ids: set[int] = set()
    if user_ids:
        sub_result = await db.execute(
            select(Subscription.user_id)
            .where(
                Subscription.user_id.in_(user_ids),
                Subscription.status == 'active',
            )
            .distinct()
        )
        active_sub_user_ids = set(sub_result.scalars().all())

    items = []
    for reg, user in rows:
        items.append(
            CampaignRegistrationItem(
                id=reg.id,
                user_id=user.id,
                telegram_id=user.telegram_id,
                username=user.username,
                first_name=user.first_name,
                bonus_type=reg.bonus_type,
                balance_bonus_kopeks=reg.balance_bonus_kopeks or 0,
                subscription_duration_days=reg.subscription_duration_days,
                tariff_id=reg.tariff_id,
                tariff_duration_days=reg.tariff_duration_days,
                created_at=reg.created_at,
                user_balance_kopeks=user.balance_kopeks or 0,
                has_subscription=user.id in active_sub_user_ids,
                has_paid=user.has_had_paid_subscription or False,
            )
        )

    return CampaignRegistrationsResponse(
        registrations=items,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post('', response_model=CampaignDetailResponse)
async def create_new_campaign(
    request: CampaignCreateRequest,
    admin: User = Depends(require_permission('campaigns:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new advertising campaign."""
    # Check if start_parameter is unique
    existing = await get_campaign_by_start_parameter(db, request.start_parameter)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Campaign with start parameter '{request.start_parameter}' already exists",
        )

    # Validate tariff exists if tariff bonus type
    if request.bonus_type == 'tariff':
        if not request.tariff_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Tariff ID is required for tariff bonus type',
            )
        tariff_result = await db.execute(select(Tariff).where(Tariff.id == request.tariff_id))
        tariff = tariff_result.scalar_one_or_none()
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Tariff not found',
            )

    # Validate partner exists and is approved
    if request.partner_user_id is not None:
        partner_user = await db.get(User, request.partner_user_id)
        if not partner_user or partner_user.partner_status != PartnerStatus.APPROVED.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Partner not found or not approved',
            )

    campaign = await create_campaign(
        db,
        name=request.name,
        start_parameter=request.start_parameter,
        bonus_type=request.bonus_type,
        created_by=admin.id,
        balance_bonus_kopeks=request.balance_bonus_kopeks,
        subscription_duration_days=request.subscription_duration_days,
        subscription_traffic_gb=request.subscription_traffic_gb,
        subscription_device_limit=request.subscription_device_limit,
        subscription_squads=request.subscription_squads,
        tariff_id=request.tariff_id,
        tariff_duration_days=request.tariff_duration_days,
        is_active=request.is_active,
        partner_user_id=request.partner_user_id,
    )

    logger.info('Admin created campaign', admin_id=admin.id, campaign_id=campaign.id, campaign_name=campaign.name)

    return await get_campaign(campaign.id, admin, db)


@router.put('/{campaign_id}', response_model=CampaignDetailResponse)
async def update_existing_campaign(
    campaign_id: int,
    request: CampaignUpdateRequest,
    admin: User = Depends(require_permission('campaigns:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing campaign."""
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    # Check if start_parameter is unique (if changing)
    if request.start_parameter and request.start_parameter != campaign.start_parameter:
        existing = await get_campaign_by_start_parameter(db, request.start_parameter)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Campaign with start parameter '{request.start_parameter}' already exists",
            )

    # Validate tariff if changing to tariff bonus type
    if request.bonus_type == 'tariff' or (campaign.bonus_type == 'tariff' and request.tariff_id):
        tariff_id = request.tariff_id or campaign.tariff_id
        if tariff_id:
            tariff_result = await db.execute(select(Tariff).where(Tariff.id == tariff_id))
            tariff = tariff_result.scalar_one_or_none()
            if not tariff:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Tariff not found',
                )

    # Build updates using model_fields_set to distinguish "not sent" from "sent as None"
    updates = {}
    if 'name' in request.model_fields_set:
        updates['name'] = request.name
    if 'start_parameter' in request.model_fields_set:
        updates['start_parameter'] = request.start_parameter
    if 'bonus_type' in request.model_fields_set:
        updates['bonus_type'] = request.bonus_type
    if 'is_active' in request.model_fields_set:
        updates['is_active'] = request.is_active
    if 'balance_bonus_kopeks' in request.model_fields_set:
        updates['balance_bonus_kopeks'] = request.balance_bonus_kopeks
    if 'subscription_duration_days' in request.model_fields_set:
        updates['subscription_duration_days'] = request.subscription_duration_days
    if 'subscription_traffic_gb' in request.model_fields_set:
        updates['subscription_traffic_gb'] = request.subscription_traffic_gb
    if 'subscription_device_limit' in request.model_fields_set:
        updates['subscription_device_limit'] = request.subscription_device_limit
    if 'subscription_squads' in request.model_fields_set:
        updates['subscription_squads'] = request.subscription_squads
    if 'tariff_id' in request.model_fields_set:
        updates['tariff_id'] = request.tariff_id
    if 'tariff_duration_days' in request.model_fields_set:
        updates['tariff_duration_days'] = request.tariff_duration_days

    # Handle partner_user_id separately (allows explicit None to unassign)
    partner_changed = False
    if 'partner_user_id' in request.model_fields_set:
        new_partner_id = request.partner_user_id
        if new_partner_id is not None:
            partner_user = await db.get(User, new_partner_id)
            if not partner_user or partner_user.partner_status != PartnerStatus.APPROVED.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='Partner not found or not approved',
                )
        campaign.partner_user_id = new_partner_id
        campaign.updated_at = datetime.now(UTC)
        partner_changed = True

    if updates:
        await update_campaign(db, campaign, **updates)
    elif partner_changed:
        await db.commit()
        await db.refresh(campaign)

    logger.info('Admin updated campaign', admin_id=admin.id, campaign_id=campaign_id)

    return await get_campaign(campaign_id, admin, db)


@router.delete('/{campaign_id}')
async def delete_existing_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('campaigns:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a campaign."""
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    # Check if campaign has registrations (COUNT query instead of loading all)
    reg_count_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            AdvertisingCampaignRegistration.campaign_id == campaign_id
        )
    )
    reg_count = reg_count_result.scalar() or 0
    if reg_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Cannot delete campaign with {reg_count} registrations. Deactivate it instead.',
        )

    await delete_campaign(db, campaign)
    logger.info('Admin deleted campaign', admin_id=admin.id, campaign_id=campaign_id, campaign_name=campaign.name)

    return {'message': 'Campaign deleted successfully'}


@router.post('/{campaign_id}/toggle', response_model=CampaignToggleResponse)
async def toggle_campaign(
    campaign_id: int,
    admin: User = Depends(require_permission('campaigns:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle campaign active status."""
    campaign = await get_campaign_by_id(db, campaign_id)
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found',
        )

    new_status = not campaign.is_active
    await update_campaign(db, campaign, is_active=new_status)

    status_text = 'activated' if new_status else 'deactivated'
    logger.info('Admin campaign', admin_id=admin.id, status_text=status_text, campaign_id=campaign_id)

    return CampaignToggleResponse(
        id=campaign_id,
        is_active=new_status,
        message=f'Campaign {status_text}',
    )
