from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.localization.texts import get_texts
from app.database.crud.referral import get_user_referral_stats
from app.database.crud.user import (
    get_user_by_id,
    get_user_by_telegram_id,
    update_user,
)
from app.database.models import User
from app.services.partner_stats_service import PartnerStatsService
from app.utils.user_utils import (
    get_detailed_referral_list,
    get_effective_referral_commission_percent,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.partners import (
    ChangeData,
    DailyStats,
    DailyStatsResponse,
    EarningsByPeriod,
    GlobalPartnerStats,
    GlobalPartnerSummary,
    NewReferralsByPeriod,
    PartnerReferralCommissionUpdate,
    PartnerReferralItem,
    PartnerReferralList,
    PartnerReferrerDetail,
    PartnerReferrerItem,
    PartnerReferrerListResponse,
    PayoutsByPeriod,
    PeriodChange,
    PeriodComparisonResponse,
    PeriodData,
    ReferralsCountByPeriod,
    ReferrerDetailedStats,
    ReferrerSummary,
    TopReferralItem,
    TopReferralsResponse,
    TopReferrerItem,
    TopReferrersResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter()


def _apply_search_filter(query, search: str):
    search_lower = f'%{search.lower()}%'
    conditions = [
        func.lower(User.username).like(search_lower),
        func.lower(User.first_name).like(search_lower),
        func.lower(User.last_name).like(search_lower),
        func.lower(User.referral_code).like(search_lower),
    ]

    if search.isdigit():
        conditions.append(User.telegram_id == int(search))
        conditions.append(User.id == int(search))

    return query.where(or_(*conditions))


def _serialize_referrer(user: User, stats: dict) -> PartnerReferrerItem:
    total_earned_kopeks = int(stats.get('total_earned_kopeks') or 0)
    month_earned_kopeks = int(stats.get('month_earned_kopeks') or 0)

    return PartnerReferrerItem(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        referral_code=user.referral_code,
        referral_commission_percent=getattr(user, 'referral_commission_percent', None),
        effective_referral_commission_percent=get_effective_referral_commission_percent(user),
        invited_count=int(stats.get('invited_count') or 0),
        active_referrals=int(stats.get('active_referrals') or 0),
        total_earned_kopeks=total_earned_kopeks,
        total_earned_rubles=round(total_earned_kopeks / 100, 2),
        month_earned_kopeks=month_earned_kopeks,
        month_earned_rubles=round(month_earned_kopeks / 100, 2),
        created_at=user.created_at,
        last_activity=user.last_activity,
    )


def _serialize_referral_item(referral: dict) -> PartnerReferralItem:
    balance_kopeks = int(referral.get('balance_kopeks') or 0)
    total_earned_kopeks = int(referral.get('total_earned_kopeks') or 0)

    # Handle email-only users (telegram_id=None)
    raw_telegram_id = referral.get('telegram_id')
    telegram_id = int(raw_telegram_id) if raw_telegram_id is not None else None

    return PartnerReferralItem(
        id=int(referral.get('id')),
        telegram_id=telegram_id,
        full_name=str(referral.get('full_name')),
        username=referral.get('username'),
        created_at=referral.get('created_at'),
        last_activity=referral.get('last_activity'),
        has_made_first_topup=bool(referral.get('has_made_first_topup', False)),
        balance_kopeks=balance_kopeks,
        balance_rubles=round(balance_kopeks / 100, 2),
        total_earned_kopeks=total_earned_kopeks,
        total_earned_rubles=round(total_earned_kopeks / 100, 2),
        topups_count=int(referral.get('topups_count') or 0),
        days_since_registration=int(referral.get('days_since_registration') or 0),
        days_since_activity=referral.get('days_since_activity'),
        status=str(referral.get('status') or 'inactive'),
    )


@router.get('/referrers', response_model=PartnerReferrerListResponse)
async def list_referrers(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None),
) -> PartnerReferrerListResponse:
    referral_alias = aliased(User)
    has_referrals = select(referral_alias.id).where(referral_alias.referred_by_id == User.id).exists()

    base_query = (
        select(User).options(selectinload(User.referrer)).where(or_(User.referral_code.isnot(None), has_referrals))
    )

    if search:
        base_query = _apply_search_filter(base_query, search)

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(base_query.order_by(User.created_at.desc()).offset(offset).limit(limit))
    referrers = result.scalars().unique().all()

    items: list[PartnerReferrerItem] = []
    for referrer in referrers:
        stats = await get_user_referral_stats(db, referrer.id)
        items.append(_serialize_referrer(referrer, stats))

    return PartnerReferrerListResponse(
        items=items,
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/referrers/{user_id}', response_model=PartnerReferrerDetail)
async def get_referrer_detail(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PartnerReferrerDetail:
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    stats = await get_user_referral_stats(db, user.id)
    referrer_item = _serialize_referrer(user, stats)

    referrals_data = await get_detailed_referral_list(db, user.id, limit=limit, offset=offset)
    referral_items = [_serialize_referral_item(referral) for referral in referrals_data.get('referrals', [])]

    referrals_list = PartnerReferralList(
        items=referral_items,
        total=int(referrals_data.get('total_count') or 0),
        limit=limit,
        offset=offset,
        has_next=bool(referrals_data.get('has_next')),
        has_prev=bool(referrals_data.get('has_prev')),
        current_page=int(referrals_data.get('current_page') or 1),
        total_pages=int(referrals_data.get('total_pages') or 1),
    )

    return PartnerReferrerDetail(referrer=referrer_item, referrals=referrals_list)


@router.patch('/referrers/{user_id}/commission', response_model=PartnerReferrerItem)
async def update_referrer_commission(
    user_id: int,
    payload: PartnerReferralCommissionUpdate,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PartnerReferrerItem:
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    await update_user(
        db,
        user,
        referral_commission_percent=payload.referral_commission_percent,
    )

    stats = await get_user_referral_stats(db, user.id)
    return _serialize_referrer(user, stats)


# ============================================================================
# РАСШИРЕННАЯ СТАТИСТИКА ПАРТНЁРОВ
# ============================================================================


@router.get('/stats', response_model=GlobalPartnerStats)
async def get_global_partner_stats(
    days: int = Query(30, ge=1, le=365),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> GlobalPartnerStats:
    """Глобальная статистика партнёрской программы."""
    data = await PartnerStatsService.get_global_partner_stats(db, days)

    return GlobalPartnerStats(
        summary=GlobalPartnerSummary(**data['summary']),
        payouts=PayoutsByPeriod(**data['payouts']),
        new_referrals=NewReferralsByPeriod(**data['new_referrals']),
    )


@router.get('/stats/daily', response_model=DailyStatsResponse)
async def get_global_daily_stats(
    days: int = Query(30, ge=1, le=365),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> DailyStatsResponse:
    """Глобальная статистика по дням."""
    data = await PartnerStatsService.get_global_daily_stats(db, days)

    return DailyStatsResponse(
        items=[DailyStats(**item) for item in data],
        days=days,
        user_id=None,
    )


@router.get('/stats/top-referrers', response_model=TopReferrersResponse)
async def get_top_referrers(
    limit: int = Query(10, ge=1, le=100),
    days: int | None = Query(None, ge=1, le=365),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TopReferrersResponse:
    """Топ рефереров по заработку."""
    data = await PartnerStatsService.get_top_referrers(db, limit, days)

    return TopReferrersResponse(
        items=[TopReferrerItem(**item) for item in data],
        days=days,
    )


@router.get('/referrers/{user_id}/stats', response_model=ReferrerDetailedStats)
async def get_referrer_detailed_stats(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ReferrerDetailedStats:
    """Детальная статистика реферера."""
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    data = await PartnerStatsService.get_referrer_detailed_stats(db, user.id)

    return ReferrerDetailedStats(
        user_id=data['user_id'],
        summary=ReferrerSummary(**data['summary']),
        earnings=EarningsByPeriod(**data['earnings']),
        referrals_count=ReferralsCountByPeriod(**data['referrals_count']),
    )


@router.get('/referrers/{user_id}/stats/daily', response_model=DailyStatsResponse)
async def get_referrer_daily_stats(
    user_id: int,
    days: int = Query(30, ge=1, le=365),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> DailyStatsResponse:
    """Статистика реферера по дням."""
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    data = await PartnerStatsService.get_referrer_daily_stats(db, user.id, days)

    return DailyStatsResponse(
        items=[DailyStats(**item) for item in data],
        days=days,
        user_id=user.id,
    )


@router.get('/referrers/{user_id}/stats/top-referrals', response_model=TopReferralsResponse)
async def get_referrer_top_referrals(
    user_id: int,
    limit: int = Query(10, ge=1, le=100),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> TopReferralsResponse:
    """Топ рефералов реферера по принесённому доходу."""
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    data = await PartnerStatsService.get_referrer_top_referrals(db, user.id, limit)

    return TopReferralsResponse(
        items=[TopReferralItem(**item) for item in data],
        user_id=user.id,
    )


@router.get('/referrers/{user_id}/stats/compare', response_model=PeriodComparisonResponse)
async def get_referrer_period_comparison(
    user_id: int,
    current_days: int = Query(7, ge=1, le=365),
    previous_days: int = Query(7, ge=1, le=365),
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> PeriodComparisonResponse:
    """Сравнение периодов для реферера."""
    user = await get_user_by_telegram_id(db, user_id)
    if not user:
        user = await get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_PARTNERS_USER_NOT_FOUND', 'User not found'),
        )

    data = await PartnerStatsService.get_referrer_period_comparison(db, user.id, current_days, previous_days)

    return PeriodComparisonResponse(
        current_period=PeriodData(**data['current_period']),
        previous_period=PeriodData(**data['previous_period']),
        change=PeriodChange(
            referrals=ChangeData(**data['change']['referrals']),
            earnings=ChangeData(**data['change']['earnings']),
        ),
        user_id=user.id,
    )
