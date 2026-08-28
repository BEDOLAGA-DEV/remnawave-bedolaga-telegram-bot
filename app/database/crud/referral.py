from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AdvertisingCampaignRegistration,
    ReferralEarning,
    ReferralRewardType,
    Subscription,
    SubscriptionStatus,
    User,
)


logger = structlog.get_logger(__name__)


def not_referee_directed():
    """Предикат «строка описывает МОЕГО реферала».

    В строке награды приглашённому колонки зеркалятся: ``user_id`` — сам
    приглашённый, ``referral_id`` — его пригласивший. Любая выборка, трактующая
    ``referral_id`` как «приглашённый мной» (GROUP BY, COUNT DISTINCT), обязана
    такие строки отбросить — иначе пользователь увидит в своих рефералах
    собственного пригласившего.

    Предикат нужен и суммам по ``user_id`` — всем, где считаются ДНИ. Для денег
    он был бы избыточен (у дневных строк сумма нулевая), но ``SUM(days_granted)``
    без него приписывает пригласившему дни, выданные приглашённому, а самому
    приглашённому — «заработок» при нуле приглашённых.
    """
    from app.services.referral_reward_service import REFEREE_DIRECTED_REASONS

    return ReferralEarning.reason.notin_(tuple(REFEREE_DIRECTED_REASONS))


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
    reward_type: str = ReferralRewardType.MONEY.value,
    level: int = 1,
    days_granted: int = 0,
    tariff_id: int | None = None,
) -> ReferralEarning:
    """Строка реферального ledger'а.

    Новые аргументы имеют значения по умолчанию, совпадающие с прежним поведением:
    деньги, первый уровень. Так все существующие вызовы продолжают писать ровно то
    же, что и раньше, и статистика на старых установках не меняется.

    Награда в днях пишется с ``amount_kopeks=0``: дни не деньги, и подмешивать их
    в денежную сумму нельзя — на ней построены и статистика, и расчёт доступного
    к выводу реферального баланса.
    """
    earning = ReferralEarning(
        user_id=user_id,
        referral_id=referral_id,
        amount_kopeks=amount_kopeks,
        reason=reason,
        referral_transaction_id=referral_transaction_id,
        campaign_id=campaign_id,
        reward_type=reward_type,
        level=level,
        days_granted=days_granted,
        tariff_id=tariff_id,
    )

    db.add(earning)
    await db.commit()
    await db.refresh(earning)

    if reward_type == ReferralRewardType.DAYS.value:
        logger.info(
            '📅 Создано реферальное начисление днями',
            days_granted=days_granted,
            level=level,
            user_id=user_id,
            tariff_id=tariff_id,
        )
    else:
        logger.info('💰 Создан реферальный заработок', amount_kopeks=amount_kopeks / 100, level=level, user_id=user_id)
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
    """Денежный заработок пригласившего за период."""
    money, _days = await get_referral_earnings_totals(db, user_id, start_date=start_date, end_date=end_date)
    return money


async def get_referral_earnings_totals(
    db: AsyncSession, user_id: int, start_date: datetime | None = None, end_date: datetime | None = None
) -> tuple[int, int]:
    """Заработок пригласившего: (копейки, дни).

    Дни — вторая валюта программы, и без них потребитель показывает ноль на
    установке, где начисления идут днями подписки. Награды приглашённому
    исключаются: строка принадлежит ему самому, а не владельцу ``user_id``.
    """
    query = select(
        func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
        func.coalesce(func.sum(ReferralEarning.days_granted), 0),
    ).where(ReferralEarning.user_id == user_id, not_referee_directed())

    if start_date:
        query = query.where(ReferralEarning.created_at >= start_date)

    if end_date:
        query = query.where(ReferralEarning.created_at <= end_date)

    result = await db.execute(query)
    money, days = result.one()
    return int(money or 0), int(days or 0)


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

    # Дни — вторая валюта программы. Без отдельной суммы установка, платящая
    # только днями, показывает «выплачено 0 ₽» при работающих начислениях.
    # Глобальный итог — это СТОИМОСТЬ программы, поэтому здесь предиката нет
    # намеренно: дни, выданные приглашённым, программа тоже раздала. Отбрасывать
    # их нужно там, где считается «сколько заработал вот этот человек».
    total_days_result = await db.execute(select(func.coalesce(func.sum(ReferralEarning.days_granted), 0)))
    total_days = total_days_result.scalar()

    referrals_stats_result = await db.execute(
        select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('referrals_count'))
        .where(User.referred_by_id.isnot(None))
        .group_by(User.referred_by_id)
    )
    referrals_stats = {row.referrer_id: row.referrals_count for row in referrals_stats_result.all()}

    # Подушевой агрегат: предикат обязателен. Без него приглашённый, не
    # пригласивший никого, попадает в топ рефереров с «заработком» из
    # собственного бонуса. В глобальные итоги выше такие дни, наоборот, входят —
    # там считается стоимость программы, а не чей-то доход.
    referral_earnings_result = await db.execute(
        select(
            ReferralEarning.user_id.label('referrer_id'),
            func.sum(ReferralEarning.amount_kopeks).label('referral_earnings'),
            func.sum(ReferralEarning.days_granted).label('referral_days'),
        )
        .where(not_referee_directed())
        .group_by(ReferralEarning.user_id)
    )
    referral_earnings = {
        row.referrer_id: (row.referral_earnings, row.referral_days) for row in referral_earnings_result.all()
    }

    top_referrers_data = {}

    for referrer_id, count in referrals_stats.items():
        if referrer_id not in top_referrers_data:
            top_referrers_data[referrer_id] = {'referrals_count': 0, 'total_earned': 0, 'total_days': 0}
        top_referrers_data[referrer_id]['referrals_count'] = count

    for referrer_id, (earnings, days) in referral_earnings.items():
        if referrer_id not in top_referrers_data:
            top_referrers_data[referrer_id] = {'referrals_count': 0, 'total_earned': 0, 'total_days': 0}
        top_referrers_data[referrer_id]['total_earned'] += earnings or 0
        top_referrers_data[referrer_id]['total_days'] += days or 0

    # Дни участвуют в сортировке: иначе реферер, которому программа платит только
    # днями, стоит с нулём и выпадает из топа, хотя приглашает больше всех.
    sorted_referrers = sorted(
        top_referrers_data.items(),
        key=lambda x: (x[1]['total_earned'], x[1]['total_days'], x[1]['referrals_count']),
        reverse=True,
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
                    'total_earned_days': stats.get('total_days', 0),
                    'referrals_count': stats['referrals_count'],
                }
            )

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    today_earnings_result = await db.execute(
        select(
            func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
            func.coalesce(func.sum(ReferralEarning.days_granted), 0),
        ).where(ReferralEarning.created_at >= today)
    )
    today_earnings, today_days = today_earnings_result.one()

    week_ago = datetime.now(UTC) - timedelta(days=7)
    week_earnings_result = await db.execute(
        select(
            func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
            func.coalesce(func.sum(ReferralEarning.days_granted), 0),
        ).where(ReferralEarning.created_at >= week_ago)
    )
    week_earnings, week_days = week_earnings_result.one()

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_earnings_result = await db.execute(
        select(
            func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
            func.coalesce(func.sum(ReferralEarning.days_granted), 0),
        ).where(ReferralEarning.created_at >= month_ago)
    )
    month_earnings, month_days = month_earnings_result.one()

    # Разбивка по уровням: главная новая величина многоуровневой схемы. Без неё
    # админ видит общую сумму и не знает, какую её часть создаёт глубина цепочки.
    by_level_result = await db.execute(
        select(
            ReferralEarning.level,
            func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('money'),
            func.coalesce(func.sum(ReferralEarning.days_granted), 0).label('days'),
            func.count(ReferralEarning.id).label('rows'),
        )
        .group_by(ReferralEarning.level)
        .order_by(ReferralEarning.level.asc())
    )
    by_level = [
        {'level': int(row.level or 1), 'money_kopeks': int(row.money), 'days': int(row.days), 'rows': int(row.rows)}
        for row in by_level_result.all()
    ]

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
        'total_paid_days': int(total_days or 0),
        'today_earnings_kopeks': today_earnings,
        'today_earnings_days': int(today_days or 0),
        'week_earnings_kopeks': week_earnings,
        'week_earnings_days': int(week_days or 0),
        'month_earnings_kopeks': month_earnings,
        'month_earnings_days': int(month_days or 0),
        'by_level': by_level,
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
                select(
                    func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                    func.coalesce(func.sum(ReferralEarning.days_granted), 0),
                ).where(
                    and_(
                        ReferralEarning.user_id == row.referrer_id,
                        ReferralEarning.created_at >= start_date,
                        not_referee_directed(),
                    )
                )
            )
            earnings, earned_days = earnings_result.one()

            top_data.append(
                {
                    'referrer_id': row.referrer_id,
                    'invited_count': row.invited_count,
                    'earnings_kopeks': earnings or 0,
                    'earnings_days': int(earned_days or 0),
                }
            )
    else:
        # Топ по заработку за период
        # Собираем заработки из ReferralEarning
        referral_earnings_result = await db.execute(
            select(
                ReferralEarning.user_id.label('referrer_id'),
                func.sum(ReferralEarning.amount_kopeks).label('ref_earnings'),
                func.sum(ReferralEarning.days_granted).label('ref_days'),
            )
            .where(ReferralEarning.created_at >= start_date, not_referee_directed())
            .group_by(ReferralEarning.user_id)
        )
        referral_earnings = {
            row.referrer_id: (row.ref_earnings or 0, int(row.ref_days or 0)) for row in referral_earnings_result
        }

        # Сортируем и берём топ. Дни участвуют в сортировке: иначе реферер
        # «дневной» программы стоит с нулём и обрезается лимитом до того, как
        # попадёт в список, каким бы он ни был.
        sorted_referrers = sorted(referral_earnings.items(), key=lambda x: x[1], reverse=True)[:limit]

        top_data = []
        for referrer_id, (earnings, earned_days) in sorted_referrers:
            # Получаем количество приглашённых за период
            invited_result = await db.execute(
                select(func.count(User.id)).where(
                    and_(User.referred_by_id == referrer_id, User.created_at >= start_date)
                )
            )
            invited_count = invited_result.scalar() or 0

            top_data.append(
                {
                    'referrer_id': referrer_id,
                    'invited_count': invited_count,
                    'earnings_kopeks': earnings,
                    'earnings_days': earned_days,
                }
            )

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
                    'earnings_days': data.get('earnings_days', 0),
                }
            )

    return result


async def get_user_referral_stats(db: AsyncSession, user_id: int) -> dict:
    invited_count_result = await db.execute(select(func.count(User.id)).where(User.referred_by_id == user_id))
    invited_count = invited_count_result.scalar()

    total_earned, total_earned_days = await get_referral_earnings_totals(db, user_id)

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_earned, month_earned_days = await get_referral_earnings_totals(db, user_id, start_date=month_ago)

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
        'total_earned_days': total_earned_days,
        'month_earned_kopeks': month_earned,
        'month_earned_days': month_earned_days,
    }
