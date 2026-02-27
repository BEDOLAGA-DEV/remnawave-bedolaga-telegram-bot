"""Referral program routes for cabinet."""

import math

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import AdvertisingCampaign, ReferralEarning, User, WithdrawalRequest, WithdrawalRequestStatus

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.referral import (
    ReferralEarningResponse,
    ReferralEarningsListResponse,
    ReferralInfoResponse,
    ReferralItemResponse,
    ReferralListResponse,
    ReferralTermsResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/referral', tags=['Cabinet Referral'])


@router.get('', response_model=ReferralInfoResponse)
async def get_referral_info(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral program info for current user."""
    # Get total referrals count
    total_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(total_query)
    total_referrals = total_result.scalar() or 0

    # Get active referrals (with subscription)
    active_query = (
        select(func.count())
        .select_from(User)
        .where(User.referred_by_id == user.id)
        .where(User.has_had_paid_subscription == True)
    )
    active_result = await db.execute(active_query)
    active_referrals = active_result.scalar() or 0

    # Get total earnings
    earnings_query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
        ReferralEarning.user_id == user.id
    )
    earnings_result = await db.execute(earnings_query)
    total_earnings = earnings_result.scalar() or 0

    # Get user's commission percent
    commission_percent = user.referral_commission_percent
    if commission_percent is None:
        commission_percent = settings.REFERRAL_COMMISSION_PERCENT

    # Get withdrawn amount (approved + completed withdrawal requests)
    withdrawn_query = select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
        WithdrawalRequest.user_id == user.id,
        WithdrawalRequest.status.in_(
            [WithdrawalRequestStatus.APPROVED.value, WithdrawalRequestStatus.COMPLETED.value]
        ),
    )
    withdrawn_result = await db.execute(withdrawn_query)
    withdrawn = withdrawn_result.scalar() or 0

    # Get pending withdrawal amount
    pending_query = select(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0)).where(
        WithdrawalRequest.user_id == user.id,
        WithdrawalRequest.status == WithdrawalRequestStatus.PENDING.value,
    )
    pending_result = await db.execute(pending_query)
    pending = pending_result.scalar() or 0

    available_balance = max(0, total_earnings - withdrawn - pending)

    # Build referral link
    bot_username = settings.get_bot_username() or 'bot'
    referral_link = f'https://t.me/{bot_username}?start={user.referral_code}'

    return ReferralInfoResponse(
        referral_code=user.referral_code or '',
        referral_link=referral_link,
        total_referrals=total_referrals,
        active_referrals=active_referrals,
        total_earnings_kopeks=total_earnings,
        total_earnings_rubles=total_earnings / 100,
        commission_percent=commission_percent,
        available_balance_kopeks=available_balance,
        available_balance_rubles=available_balance / 100,
        withdrawn_kopeks=withdrawn,
    )


@router.get('/list', response_model=ReferralListResponse)
async def get_referral_list(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of invited users."""
    # Base query with eager loading of subscription relationship
    query = select(User).options(selectinload(User.subscription)).where(User.referred_by_id == user.id)

    # Get total count
    count_query = select(func.count()).select_from(User).where(User.referred_by_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(User.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    referrals = result.scalars().all()

    items = [
        ReferralItemResponse(
            id=r.id,
            username=r.username,
            first_name=r.first_name,
            created_at=r.created_at,
            has_subscription=r.subscription is not None,
            has_paid=r.has_had_paid_subscription,
        )
        for r in referrals
    ]

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/earnings', response_model=ReferralEarningsListResponse)
async def get_referral_earnings(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get referral earnings history."""
    # Base query
    query = select(ReferralEarning).where(ReferralEarning.user_id == user.id)

    # Get total count and sum
    count_query = select(func.count()).select_from(ReferralEarning).where(ReferralEarning.user_id == user.id)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    sum_query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
        ReferralEarning.user_id == user.id
    )
    sum_result = await db.execute(sum_query)
    total_amount = sum_result.scalar() or 0

    # Paginate
    offset = (page - 1) * per_page
    query = query.order_by(desc(ReferralEarning.created_at)).offset(offset).limit(per_page)

    result = await db.execute(query)
    earnings = result.scalars().all()

    # Batch-fetch referral users to avoid N+1
    referral_ids = list({e.referral_id for e in earnings if e.referral_id})
    if referral_ids:
        referral_users_result = await db.execute(select(User).where(User.id.in_(referral_ids)))
        referral_users_map = {u.id: u for u in referral_users_result.scalars().all()}
    else:
        referral_users_map = {}

    # Batch-fetch campaigns to avoid N+1
    campaign_ids = list({e.campaign_id for e in earnings if e.campaign_id})
    if campaign_ids:
        campaigns_result = await db.execute(select(AdvertisingCampaign).where(AdvertisingCampaign.id.in_(campaign_ids)))
        campaigns_map = {c.id: c for c in campaigns_result.scalars().all()}
    else:
        campaigns_map = {}

    items = []
    for e in earnings:
        referral_user = referral_users_map.get(e.referral_id) if e.referral_id else None
        campaign = campaigns_map.get(e.campaign_id) if e.campaign_id else None

        items.append(
            ReferralEarningResponse(
                id=e.id,
                amount_kopeks=e.amount_kopeks,
                amount_rubles=e.amount_kopeks / 100,
                reason=e.reason or 'Referral commission',
                referral_username=referral_user.username if referral_user else None,
                referral_first_name=referral_user.first_name if referral_user else None,
                campaign_name=campaign.name if campaign else None,
                created_at=e.created_at,
            )
        )

    pages = math.ceil(total / per_page) if total > 0 else 1

    return ReferralEarningsListResponse(
        items=items,
        total=total,
        total_amount_kopeks=total_amount,
        total_amount_rubles=total_amount / 100,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/terms', response_model=ReferralTermsResponse)
async def get_referral_terms():
    """Get referral program terms."""
    return ReferralTermsResponse(
        is_enabled=settings.is_referral_program_enabled(),
        commission_percent=settings.REFERRAL_COMMISSION_PERCENT,
        minimum_topup_kopeks=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS,
        minimum_topup_rubles=settings.REFERRAL_MINIMUM_TOPUP_KOPEKS / 100,
        first_topup_bonus_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
        first_topup_bonus_rubles=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS / 100,
        inviter_bonus_kopeks=settings.REFERRAL_INVITER_BONUS_KOPEKS,
        inviter_bonus_rubles=settings.REFERRAL_INVITER_BONUS_KOPEKS / 100,
        partner_section_visible=settings.REFERRAL_PARTNER_SECTION_VISIBLE,
    )
