from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import AdvertisingCampaignRegistration, ReferralEarning, Subscription, SubscriptionStatus, User


logger = structlog.get_logger(__name__)


async def get_user_campaign_id(db: AsyncSession, user_id: int) -> int | None:
    """Получить campaign_id первой регистрации пользователя."""
    result = await db.execute(
        select(AdvertisingCampaignRegistration.campaign_id)
        .where(AdvertisingCampaignRegistration.user_id == user_id)
        .order_by(AdvertisingCampaignRegistration.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_referral_earning(
    db: AsyncSession,
    user_id: int,
    referral_id: int,
    amount_kopeks: int,
    reason: str,
    referral_transaction_id: int | None = None,
    campaign_id: int | None = None,
) -> ReferralEarning:
    earning = ReferralEarning(
        user_id=user_id,
        referral_id=referral_id,
        amount_kopeks=amount_kopeks,
        reason=reason,
        referral_transaction_id=referral_transaction_id,
        campaign_id=campaign_id,
    )

    db.add(earning)
    await db.commit()
    await db.refresh(earning)

    logger.info(
        '💰 Создан реферальный заработок: ₽ для пользователя', amount_kopeks=amount_kopeks / 100, user_id=user_id
    )
    return earning


async def get_commission_payment_count(db: AsyncSession, referrer_id: int, referral_id: int) -> int:
    """Подсчитать количество комиссионных начислений реферера за платежи конкретного реферала."""
    result = await db.execute(
        select(func.count(ReferralEarning.id)).where(
            and_(
                ReferralEarning.user_id == referrer_id,
                ReferralEarning.referral_id == referral_id,
                ReferralEarning.reason == 'referral_commission_topup',
            )
        )
    )
    return result.scalar() or 0


async def get_referral_earnings_by_user(
    db: AsyncSession, user_id: int, limit: int = 50, offset: int = 0
) -> list[ReferralEarning]:
    result = await db.execute(
        select(ReferralEarning)
        .options(
            selectinload(ReferralEarning.referral),
            selectinload(ReferralEarning.referral_transaction),
            selectinload(ReferralEarning.campaign),
        )
        .where(ReferralEarning.user_id == user_id)
        .order_by(ReferralEarning.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


async def get_referral_earnings_by_referral(db: AsyncSession, referral_id: int) -> list[ReferralEarning]:
    result = await db.execute(
        select(ReferralEarning)
        .where(ReferralEarning.referral_id == referral_id)
        .order_by(ReferralEarning.created_at.desc())
    )
    return result.scalars().all()


async def get_referral_earnings_sum(
    db: AsyncSession, user_id: int, start_date: datetime | None = None, end_date: datetime | None = None
) -> int:
    query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(ReferralEarning.user_id == user_id)

    if start_date:
        query = query.where(ReferralEarning.created_at >= start_date)

    if end_date:
        query = query.where(ReferralEarning.created_at <= end_date)

    result = await db.execute(query)
    return result.scalar() or 0


async def get_referral_statistics(db: AsyncSession) -> dict:
    users_with_referrals_result = await db.execute(
        select(func.count(func.distinct(User.id))).where(User.referred_by_id.isnot(None))
    )
    users_with_referrals = users_with_referrals_result.scalar()

    active_referrers_result = await db.execute(
        select(func.count(func.distinct(User.referred_by_id))).where(User.referred_by_id.isnot(None))
    )
    active_referrers = active_referrers_result.scalar()

    referral_paid_result = await db.execute(select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)))
    total_paid = referral_paid_result.scalar()

    referrals_stats_result = await db.execute(
        select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('referrals_count'))
        .where(User.referred_by_id.isnot(None))
        .group_by(User.referred_by_id)
    )
    referrals_stats = {row.referrer_id: row.referrals_count for row in referrals_stats_result.all()}

    referral_earnings_result = await db.execute(
        select(
            ReferralEarning.user_id.label('referrer_id'),
            func.sum(ReferralEarning.amount_kopeks).label('referral_earnings'),
        ).group_by(ReferralEarning.user_id)
    )
    referral_earnings = {row.referrer_id: row.referral_earnings for row in referral_earnings_result.all()}

    top_referrers_data = {}

    for referrer_id, count in referrals_stats.items():
        if referrer_id not in top_referrers_data:
            top_referrers_data[referrer_id] = {'referrals_count': 0, 'total_earned': 0}
        top_referrers_data[referrer_id]['referrals_count'] = count

    for referrer_id, earnings in referral_earnings.items():
        if referrer_id not in top_referrers_data:
            top_referrers_data[referrer_id] = {'referrals_count': 0, 'total_earned': 0}
        top_referrers_data[referrer_id]['total_earned'] += earnings or 0

    sorted_referrers = sorted(
        top_referrers_data.items(), key=lambda x: (x[1]['total_earned'], x[1]['referrals_count']), reverse=True
    )

    top_referrers = []
    for referrer_id, stats in sorted_referrers[:5]:
        user_result = await db.execute(
            select(User.id, User.username, User.first_name, User.last_name, User.telegram_id).where(
                User.id == referrer_id
            )
        )
        user = user_result.first()

        if user:
            display_name = ''
            if user.first_name:
                display_name = user.first_name
                if user.last_name:
                    display_name += f' {user.last_name}'
            elif user.username:
                display_name = f'@{user.username}'
            elif user.telegram_id:
                display_name = f'ID{user.telegram_id}'
            else:
                display_name = user.email or f'#{user.id}'

            top_referrers.append(
                {
                    'user_id': user.id,  # Use internal ID, not telegram_id
                    'display_name': display_name,
                    'username': user.username,
                    'telegram_id': user.telegram_id,  # Can be None for email users
                    'total_earned_kopeks': stats['total_earned'],
                    'referrals_count': stats['referrals_count'],
                }
            )

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    today_earnings_result = await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(ReferralEarning.created_at >= today)
    )
    today_earnings = today_earnings_result.scalar()

    week_ago = datetime.now(UTC) - timedelta(days=7)
    week_earnings_result = await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(ReferralEarning.created_at >= week_ago)
    )
    week_earnings = week_earnings_result.scalar()

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_earnings_result = await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(ReferralEarning.created_at >= month_ago)
    )
    month_earnings = month_earnings_result.scalar()

    logger.info(
        'Реферальная статистика: рефералов, рефереров, выплачено копеек',
        users_with_referrals=users_with_referrals,
        active_referrers=active_referrers,
        total_paid=total_paid,
    )

    return {
        'users_with_referrals': users_with_referrals,
        'active_referrers': active_referrers,
        'total_paid_kopeks': total_paid,
        'today_earnings_kopeks': today_earnings,
        'week_earnings_kopeks': week_earnings,
        'month_earnings_kopeks': month_earnings,
        'top_referrers': top_referrers,
    }


async def get_top_referrers_by_period(
    db: AsyncSession,
    period: str = 'week',  # "week" или "month"
    sort_by: str = 'earnings',  # "earnings" или "invited"
    limit: int = 20,
) -> list:
    """
    Получает топ рефереров за период.

    Args:
        period: "week" (7 дней) или "month" (30 дней)
        sort_by: "earnings" (по заработку) или "invited" (по приглашённым)
        limit: количество записей

    Returns:
        Список словарей с данными рефереров
    """
    now = datetime.now(UTC)
    if period == 'week':
        start_date = now - timedelta(days=7)
    else:  # month
        start_date = now - timedelta(days=30)

    if sort_by == 'invited':
        # Топ по количеству приглашённых за период
        referrals_result = await db.execute(
            select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('invited_count'))
            .where(and_(User.referred_by_id.isnot(None), User.created_at >= start_date))
            .group_by(User.referred_by_id)
            .order_by(func.count(User.id).desc())
            .limit(limit)
        )

        top_data = []
        for row in referrals_result:
            # Получаем заработок за период для этого реферера
            earnings_result = await db.execute(
                select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
                    and_(ReferralEarning.user_id == row.referrer_id, ReferralEarning.created_at >= start_date)
                )
            )
            earnings = earnings_result.scalar() or 0

            top_data.append(
                {'referrer_id': row.referrer_id, 'invited_count': row.invited_count, 'earnings_kopeks': earnings}
            )
    else:
        # Топ по заработку за период
        # Собираем заработки из ReferralEarning
        referral_earnings_result = await db.execute(
            select(
                ReferralEarning.user_id.label('referrer_id'),
                func.sum(ReferralEarning.amount_kopeks).label('ref_earnings'),
            )
            .where(ReferralEarning.created_at >= start_date)
            .group_by(ReferralEarning.user_id)
        )
        referral_earnings = {row.referrer_id: row.ref_earnings for row in referral_earnings_result}

        # Сортируем и берём топ
        sorted_referrers = sorted(referral_earnings.items(), key=lambda x: x[1], reverse=True)[:limit]

        top_data = []
        for referrer_id, earnings in sorted_referrers:
            # Получаем количество приглашённых за период
            invited_result = await db.execute(
                select(func.count(User.id)).where(
                    and_(User.referred_by_id == referrer_id, User.created_at >= start_date)
                )
            )
            invited_count = invited_result.scalar() or 0

            top_data.append({'referrer_id': referrer_id, 'invited_count': invited_count, 'earnings_kopeks': earnings})

    # Добавляем информацию о пользователях
    result = []
    for data in top_data:
        user_result = await db.execute(
            select(User.id, User.username, User.first_name, User.last_name, User.telegram_id).where(
                User.id == data['referrer_id']
            )
        )
        user = user_result.first()

        if user:
            display_name = ''
            if user.first_name:
                display_name = user.first_name
                if user.last_name:
                    display_name += f' {user.last_name}'
            elif user.username:
                display_name = f'@{user.username}'
            elif user.telegram_id:
                display_name = f'ID{user.telegram_id}'
            else:
                display_name = user.email or f'#{user.id}'

            result.append(
                {
                    'user_id': user.id,
                    'telegram_id': user.telegram_id,  # Can be None for email users
                    'username': user.username,
                    'display_name': display_name,
                    'invited_count': data['invited_count'],
                    'earnings_kopeks': data['earnings_kopeks'],
                }
            )

    return result


async def get_user_referral_stats(db: AsyncSession, user_id: int) -> dict:
    invited_count_result = await db.execute(select(func.count(User.id)).where(User.referred_by_id == user_id))
    invited_count = invited_count_result.scalar()

    total_earned = await get_referral_earnings_sum(db, user_id)

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_earned = await get_referral_earnings_sum(db, user_id, start_date=month_ago)

    active_referrals_result = await db.execute(
        select(func.count(func.distinct(User.id)))
        .join(Subscription, User.id == Subscription.user_id)
        .where(
            and_(
                User.referred_by_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > func.now(),
            )
        )
    )
    active_referrals = active_referrals_result.scalar() or 0

    return {
        'invited_count': invited_count,
        'active_referrals': active_referrals,
        'total_earned_kopeks': total_earned,
        'month_earned_kopeks': month_earned,
    }
