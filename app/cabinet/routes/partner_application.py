"""User-facing partner application routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AdvertisingCampaign, User
from app.services.partner_application_service import partner_application_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.partners import (
    PartnerApplicationInfo,
    PartnerApplicationRequest,
    PartnerCampaignInfo,
    PartnerStatusResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/referral/partner', tags=['Cabinet Partner'])


def _get_campaign_deep_link(start_parameter: str) -> str | None:
    """Generate Telegram deep link for campaign."""
    bot_username = settings.get_bot_username()
    if bot_username:
        return f'https://t.me/{bot_username}?start={start_parameter}'
    return None


def _get_campaign_web_link(start_parameter: str) -> str | None:
    """Generate web link for campaign."""
    base_url = (settings.MINIAPP_CUSTOM_URL or '').rstrip('/')
    if base_url:
        return f'{base_url}/?campaign={start_parameter}'
    return None


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
        for c in result.scalars().all():
            campaigns.append(
                PartnerCampaignInfo(
                    id=c.id,
                    name=c.name,
                    start_parameter=c.start_parameter,
                    bonus_type=c.bonus_type,
                    balance_bonus_kopeks=c.balance_bonus_kopeks or 0,
                    subscription_duration_days=c.subscription_duration_days,
                    subscription_traffic_gb=c.subscription_traffic_gb,
                    deep_link=_get_campaign_deep_link(c.start_parameter),
                    web_link=_get_campaign_web_link(c.start_parameter),
                )
            )

    return PartnerStatusResponse(
        partner_status=user.partner_status,
        commission_percent=commission,
        latest_application=app_info,
        campaigns=campaigns,
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
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_partner_application_notification(
                    user=user,
                    application_data={
                        'company_name': request.company_name,
                        'telegram_channel': request.telegram_channel,
                        'website_url': request.website_url,
                        'description': request.description,
                        'expected_monthly_referrals': request.expected_monthly_referrals,
                    },
                )
            finally:
                await bot.session.close()
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
