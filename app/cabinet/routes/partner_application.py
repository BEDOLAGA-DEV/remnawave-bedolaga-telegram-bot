"""User-facing partner application routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.links import get_campaign_deep_link, get_campaign_web_link
from app.config import settings
from app.database.models import AdvertisingCampaign, User
from app.services.partner_application_service import partner_application_service
from app.services.partner_stats_service import PartnerStatsService

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.partners import (
    CampaignReferralItem,
    DailyStatItem,
    PartnerApplicationInfo,
    PartnerApplicationRequest,
    PartnerCampaignDetailedStats,
    PartnerCampaignInfo,
    PartnerStatusResponse,
    PeriodChange,
    PeriodComparison,
    PeriodStats,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/referral/partner', tags=['Cabinet Partner'])


@router.get('/status', response_model=PartnerStatusResponse)
async def get_partner_status(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get partner status and latest application for current user."""
    latest_app = await partner_application_service.get_latest_application(db, user.id)

    app_info = None
    if latest_app:
        app_info = PartnerApplicationInfo(
            id=latest_app.id,
            status=latest_app.status,
            company_name=latest_app.company_name,
            website_url=latest_app.website_url,
            telegram_channel=latest_app.telegram_channel,
            description=latest_app.description,
            expected_monthly_referrals=latest_app.expected_monthly_referrals,
            desired_commission_percent=latest_app.desired_commission_percent,
            admin_comment=latest_app.admin_comment,
            approved_commission_percent=latest_app.approved_commission_percent,
            created_at=latest_app.created_at,
            processed_at=latest_app.processed_at,
        )

    commission = user.referral_commission_percent
    if commission is None and user.is_partner:
        commission = settings.REFERRAL_COMMISSION_PERCENT

    # Fetch campaigns assigned to this partner
    campaigns: list[PartnerCampaignInfo] = []
    if user.is_partner:
        result = await db.execute(
            select(AdvertisingCampaign).where(
                AdvertisingCampaign.partner_user_id == user.id,
                AdvertisingCampaign.is_active.is_(True),
            )
        )
        campaign_models = result.scalars().all()

        # Fetch per-campaign stats in one batch
        campaign_ids = [c.id for c in campaign_models]
        campaign_stats = await PartnerStatsService.get_per_campaign_stats(db, user.id, campaign_ids)

        for c in campaign_models:
            stats = campaign_stats.get(c.id, {})
            campaigns.append(
                PartnerCampaignInfo(
                    id=c.id,
                    name=c.name,
                    start_parameter=c.start_parameter,
                    bonus_type=c.bonus_type,
                    balance_bonus_kopeks=c.balance_bonus_kopeks or 0,
                    subscription_duration_days=c.subscription_duration_days,
                    subscription_traffic_gb=c.subscription_traffic_gb,
                    deep_link=get_campaign_deep_link(c.start_parameter),
                    web_link=get_campaign_web_link(c.start_parameter),
                    registrations_count=stats.get('registrations_count', 0),
                    referrals_count=stats.get('referrals_count', 0),
                    earnings_kopeks=stats.get('earnings_kopeks', 0),
                )
            )

    return PartnerStatusResponse(
        partner_status=user.partner_status,
        commission_percent=commission,
        latest_application=app_info,
        campaigns=campaigns,
    )


@router.get('/campaigns/{campaign_id}/stats', response_model=PartnerCampaignDetailedStats)
async def get_campaign_stats(
    campaign_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed stats for a single campaign belonging to the current partner."""
    if not user.is_partner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Partner status required',
        )

    # Verify campaign belongs to this partner
    campaign_result = await db.execute(
        select(AdvertisingCampaign).where(
            AdvertisingCampaign.id == campaign_id,
            AdvertisingCampaign.partner_user_id == user.id,
        )
    )
    campaign = campaign_result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Campaign not found or not assigned to you',
        )

    raw = await PartnerStatsService.get_campaign_detailed_stats(db, user.id, campaign_id)

    return PartnerCampaignDetailedStats(
        campaign_id=raw['campaign_id'],
        campaign_name=campaign.name,
        registrations_count=raw['registrations_count'],
        referrals_count=raw['referrals_count'],
        earnings_kopeks=raw['earnings_kopeks'],
        conversion_rate=raw['conversion_rate'],
        earnings_today=raw['earnings_today'],
        earnings_week=raw['earnings_week'],
        earnings_month=raw['earnings_month'],
        daily_stats=[DailyStatItem(**d) for d in raw['daily_stats']],
        period_comparison=PeriodComparison(
            current=PeriodStats(**raw['period_comparison']['current']),
            previous=PeriodStats(**raw['period_comparison']['previous']),
            referrals_change=PeriodChange(**raw['period_comparison']['referrals_change']),
            earnings_change=PeriodChange(**raw['period_comparison']['earnings_change']),
        ),
        top_referrals=[CampaignReferralItem(**r) for r in raw['top_referrals']],
    )


@router.post('/apply', response_model=PartnerApplicationInfo)
async def apply_for_partner(
    request: PartnerApplicationRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Submit partner application."""
    application, error = await partner_application_service.submit_application(
        db,
        user_id=user.id,
        company_name=request.company_name,
        website_url=request.website_url,
        telegram_channel=request.telegram_channel,
        description=request.description,
        expected_monthly_referrals=request.expected_monthly_referrals,
        desired_commission_percent=request.desired_commission_percent,
    )

    if not application:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error,
        )

    # Уведомляем админов о новой заявке
    try:
        from aiogram import Bot

        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            from app.utils.bot_utils import get_bot

            async with get_bot() as bot:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_partner_application_notification(
                    user=user,
                    application_data={
                        'company_name': request.company_name,
                        'telegram_channel': request.telegram_channel,
                        'website_url': request.website_url,
                        'description': request.description,
                        'expected_monthly_referrals': request.expected_monthly_referrals,
                        'desired_commission_percent': request.desired_commission_percent,
                    },
                )
    except Exception as e:
        logger.error('Failed to send admin notification for partner application', error=e)

    return PartnerApplicationInfo(
        id=application.id,
        status=application.status,
        company_name=application.company_name,
        website_url=application.website_url,
        telegram_channel=application.telegram_channel,
        description=application.description,
        expected_monthly_referrals=application.expected_monthly_referrals,
        desired_commission_percent=application.desired_commission_percent,
        admin_comment=application.admin_comment,
        approved_commission_percent=application.approved_commission_percent,
        created_at=application.created_at,
        processed_at=application.processed_at,
    )
