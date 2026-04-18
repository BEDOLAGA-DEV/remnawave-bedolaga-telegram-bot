"""Сервис расширенной статистики партнёров (рефереров)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    AdvertisingCampaignRegistration,
    ReferralEarning,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)

# Constants for campaign detailed stats
DAILY_STATS_DAYS = 30
TOP_REFERRALS_LIMIT = 5
PERIOD_COMPARISON_DAYS = 7


def _calc_change(current_val: int, previous_val: int) -> dict[str, Any]:
    """Calculate period-over-period change metrics."""
    diff = current_val - previous_val
    pct = round((diff / previous_val * 100), 2) if previous_val > 0 else (100.0 if current_val > 0 else 0.0)
    trend = 'up' if diff > 0 else 'down' if diff < 0 else 'stable'
    return {'absolute': diff, 'percent': pct, 'trend': trend}


class PartnerStatsService:
    """Сервис для детальной статистики партнёров."""

    @classmethod
    async def get_referrer_detailed_stats(
        cls,
        db: AsyncSession,
        user_id: int,
    ) -> dict[str, Any]:
        """Получить детальную статистику реферера."""
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        year_ago = now - timedelta(days=365)

        # Агрегированная статистика рефералов одним запросом (без загрузки всех User в память)
        referral_counts_result = await db.execute(
            select(
                func.count(User.id).label('total'),
                func.sum(case((User.has_made_first_topup.is_(True), 1), else_=0)).label('paid'),
                func.sum(case((User.created_at >= today_start, 1), else_=0)).label('today'),
                func.sum(case((User.created_at >= week_ago, 1), else_=0)).label('week'),
                func.sum(case((User.created_at >= month_ago, 1), else_=0)).label('month'),
                func.sum(case((User.created_at >= year_ago, 1), else_=0)).label('year'),
            ).where(User.referred_by_id == user_id)
        )
        ref_row = referral_counts_result.one()
        total_referrals = int(ref_row.total or 0)
        paid_referrals = int(ref_row.paid or 0)
        referrals_today = int(ref_row.today or 0)
        referrals_week = int(ref_row.week or 0)
        referrals_month = int(ref_row.month or 0)
        referrals_year = int(ref_row.year or 0)

        # Активные рефералы (с активной подпиской)
        if total_referrals > 0:
            active_result = await db.execute(
                select(func.count(func.distinct(User.id)))
                .join(Subscription, User.id == Subscription.user_id)
                .where(
                    and_(
                        User.referred_by_id == user_id,
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.end_date > now,
                    )
                )
            )
            active_referrals = active_result.scalar() or 0
        else:
            active_referrals = 0

        # Заработки по периодам - один запрос с CASE WHEN
        earnings_result = await db.execute(
            select(
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('all_time'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= today_start, ReferralEarning.amount_kopeks), else_=0)),
                    0,
                ).label('today'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= week_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('week'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= month_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('month'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= year_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('year'),
            ).where(ReferralEarning.user_id == user_id)
        )
        earnings_row = earnings_result.one()
        earnings_all_time = int(earnings_row.all_time)
        earnings_today = int(earnings_row.today)
        earnings_week = int(earnings_row.week)
        earnings_month = int(earnings_row.month)
        earnings_year = int(earnings_row.year)

        # Конверсии
        conversion_to_paid = round((paid_referrals / total_referrals * 100), 2) if total_referrals > 0 else 0
        conversion_to_active = round((active_referrals / total_referrals * 100), 2) if total_referrals > 0 else 0

        # Средний доход с реферала
        avg_earnings_per_referral = round(earnings_all_time / paid_referrals, 2) if paid_referrals > 0 else 0

        return {
            'user_id': user_id,
            'summary': {
                'total_referrals': total_referrals,
                'paid_referrals': paid_referrals,
                'active_referrals': active_referrals,
                'conversion_to_paid_percent': conversion_to_paid,
                'conversion_to_active_percent': conversion_to_active,
                'avg_earnings_per_referral_kopeks': avg_earnings_per_referral,
            },
            'earnings': {
                'all_time_kopeks': earnings_all_time,
                'year_kopeks': earnings_year,
                'month_kopeks': earnings_month,
                'week_kopeks': earnings_week,
                'today_kopeks': earnings_today,
            },
            'referrals_count': {
                'all_time': total_referrals,
                'year': referrals_year,
                'month': referrals_month,
                'week': referrals_week,
                'today': referrals_today,
            },
        }

    @classmethod
    async def get_referrer_daily_stats(
        cls,
        db: AsyncSession,
        user_id: int,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику реферера по дням."""
        now = datetime.now(UTC)
        start_date = now - timedelta(days=days)

        # Рефералы по дням
        referrals_by_day = await db.execute(
            select(
                func.date(User.created_at).label('date'),
                func.count(User.id).label('referrals_count'),
            )
            .where(
                and_(
                    User.referred_by_id == user_id,
                    User.created_at >= start_date,
                )
            )
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
        referrals_dict = {str(row.date): row.referrals_count for row in referrals_by_day.all()}

        # Заработки по дням (из ReferralEarning)
        earnings_by_day = await db.execute(
            select(
                func.date(ReferralEarning.created_at).label('date'),
                func.sum(ReferralEarning.amount_kopeks).label('earnings'),
            )
            .where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.created_at >= start_date,
                )
            )
            .group_by(func.date(ReferralEarning.created_at))
        )
        earnings_dict = {str(row.date): int(row.earnings or 0) for row in earnings_by_day.all()}

        # Формируем массив за все дни
        result = []
        for i in range(days):
            date = (start_date + timedelta(days=i)).date()
            date_str = str(date)
            result.append(
                {
                    'date': date_str,
                    'referrals_count': referrals_dict.get(date_str, 0),
                    'earnings_kopeks': earnings_dict.get(date_str, 0),
                }
            )

        return result

    @classmethod
    async def get_referrer_top_referrals(
        cls,
        db: AsyncSession,
        user_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Получить топ рефералов по доходу для реферера."""
        now = datetime.now(UTC)

        # Получаем рефералов с их доходами
        result = await db.execute(
            select(
                User.id,
                User.telegram_id,
                User.username,
                User.first_name,
                User.last_name,
                User.created_at,
                User.has_made_first_topup,
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('total_earnings'),
            )
            .outerjoin(ReferralEarning, ReferralEarning.referral_id == User.id)
            .where(User.referred_by_id == user_id)
            .group_by(User.id)
            .order_by(desc(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)))
            .limit(limit)
        )
        rows = result.all()

        if not rows:
            return []

        # Собираем user_id для проверки активных подписок одним запросом
        user_ids = [row.id for row in rows]

        # Получаем все активные подписки для этих пользователей одним запросом
        active_subs_result = await db.execute(
            select(Subscription.user_id).where(
                and_(
                    Subscription.user_id.in_(user_ids),
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now,
                )
            )
        )
        active_user_ids = {row.user_id for row in active_subs_result.all()}

        referrals = []
        for row in rows:
            referrals.append(
                {
                    'id': row.id,
                    'telegram_id': row.telegram_id,
                    'username': row.username,
                    'first_name': row.first_name,
                    'last_name': row.last_name,
                    'full_name': f'{row.first_name or ""} {row.last_name or ""}'.strip()
                    or (row.telegram_id and f'User {row.telegram_id}')
                    or f'User #{row.id}',
                    'created_at': row.created_at,
                    'has_made_first_topup': row.has_made_first_topup,
                    'is_active': row.id in active_user_ids,
                    'total_earnings_kopeks': int(row.total_earnings),
                }
            )

        return referrals

    @classmethod
    async def get_referrer_period_comparison(
        cls,
        db: AsyncSession,
        user_id: int,
        current_days: int = 7,
        previous_days: int = 7,
    ) -> dict[str, Any]:
        """Сравнить текущий и предыдущий период."""
        now = datetime.now(UTC)
        current_start = now - timedelta(days=current_days)
        previous_start = current_start - timedelta(days=previous_days)
        previous_end = current_start

        # Рефералы за текущий период
        current_referrals = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.referred_by_id == user_id,
                    User.created_at >= current_start,
                )
            )
        )
        current_referrals_count = current_referrals.scalar() or 0

        # Рефералы за предыдущий период
        previous_referrals = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.referred_by_id == user_id,
                    User.created_at >= previous_start,
                    User.created_at < previous_end,
                )
            )
        )
        previous_referrals_count = previous_referrals.scalar() or 0

        # Заработки за текущий период
        current_earnings = await cls._get_earnings_for_period(db, user_id, current_start)

        # Заработки за предыдущий период
        previous_earnings = await cls._get_earnings_for_period(db, user_id, previous_start, previous_end)

        # Расчёт изменений
        referrals_change = current_referrals_count - previous_referrals_count
        referrals_change_percent = (
            round((referrals_change / previous_referrals_count * 100), 2) if previous_referrals_count > 0 else 0
        )

        earnings_change = current_earnings - previous_earnings
        earnings_change_percent = round((earnings_change / previous_earnings * 100), 2) if previous_earnings > 0 else 0

        return {
            'current_period': {
                'days': current_days,
                'start': current_start.isoformat(),
                'end': now.isoformat(),
                'referrals_count': current_referrals_count,
                'earnings_kopeks': current_earnings,
            },
            'previous_period': {
                'days': previous_days,
                'start': previous_start.isoformat(),
                'end': previous_end.isoformat(),
                'referrals_count': previous_referrals_count,
                'earnings_kopeks': previous_earnings,
            },
            'change': {
                'referrals': {
                    'absolute': referrals_change,
                    'percent': referrals_change_percent,
                    'trend': 'up' if referrals_change > 0 else 'down' if referrals_change < 0 else 'stable',
                },
                'earnings': {
                    'absolute': earnings_change,
                    'percent': earnings_change_percent,
                    'trend': 'up' if earnings_change > 0 else 'down' if earnings_change < 0 else 'stable',
                },
            },
        }

    @classmethod
    async def get_global_partner_stats(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> dict[str, Any]:
        """Глобальная статистика партнёрской программы."""
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        year_ago = now - timedelta(days=365)

        # Всего рефереров (у кого есть рефералы)
        total_referrers = await db.execute(
            select(func.count(func.distinct(User.referred_by_id))).where(User.referred_by_id.isnot(None))
        )
        total_referrers_count = total_referrers.scalar() or 0

        # Всего рефералов
        total_referrals = await db.execute(select(func.count(User.id)).where(User.referred_by_id.isnot(None)))
        total_referrals_count = total_referrals.scalar() or 0

        # Рефералы которые заплатили
        paid_referrals = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.referred_by_id.isnot(None),
                    User.has_made_first_topup.is_(True),
                )
            )
        )
        paid_referrals_count = paid_referrals.scalar() or 0

        # Всего выплачено - один запрос с CASE WHEN
        payouts_result = await db.execute(
            select(
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('all_time'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= today_start, ReferralEarning.amount_kopeks), else_=0)),
                    0,
                ).label('today'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= week_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('week'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= month_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('month'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= year_ago, ReferralEarning.amount_kopeks), else_=0)), 0
                ).label('year'),
            )
        )
        payouts_row = payouts_result.one()
        total_paid = int(payouts_row.all_time)
        today_paid = int(payouts_row.today)
        week_paid = int(payouts_row.week)
        month_paid = int(payouts_row.month)
        year_paid = int(payouts_row.year)

        # Новые рефералы по периодам - один запрос с CASE WHEN
        new_referrals_result = await db.execute(
            select(
                func.sum(case((User.created_at >= today_start, 1), else_=0)).label('today'),
                func.sum(case((User.created_at >= week_ago, 1), else_=0)).label('week'),
                func.sum(case((User.created_at >= month_ago, 1), else_=0)).label('month'),
            ).where(User.referred_by_id.isnot(None))
        )
        new_referrals_row = new_referrals_result.one()
        new_referrals_today_count = int(new_referrals_row.today or 0)
        new_referrals_week_count = int(new_referrals_row.week or 0)
        new_referrals_month_count = int(new_referrals_row.month or 0)

        # Конверсия
        conversion_rate = (
            round((paid_referrals_count / total_referrals_count * 100), 2) if total_referrals_count > 0 else 0
        )

        # Средний доход с реферала
        avg_per_referral = round(total_paid / paid_referrals_count, 2) if paid_referrals_count > 0 else 0

        return {
            'summary': {
                'total_referrers': total_referrers_count,
                'total_referrals': total_referrals_count,
                'paid_referrals': paid_referrals_count,
                'conversion_rate_percent': conversion_rate,
                'avg_earnings_per_referral_kopeks': avg_per_referral,
            },
            'payouts': {
                'all_time_kopeks': total_paid,
                'year_kopeks': year_paid,
                'month_kopeks': month_paid,
                'week_kopeks': week_paid,
                'today_kopeks': today_paid,
            },
            'new_referrals': {
                'today': new_referrals_today_count,
                'week': new_referrals_week_count,
                'month': new_referrals_month_count,
            },
        }

    @classmethod
    async def get_global_daily_stats(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Глобальная статистика по дням."""
        now = datetime.now(UTC)
        start_date = now - timedelta(days=days)

        # Рефералы по дням
        referrals_by_day = await db.execute(
            select(
                func.date(User.created_at).label('date'),
                func.count(User.id).label('referrals_count'),
            )
            .where(
                and_(
                    User.referred_by_id.isnot(None),
                    User.created_at >= start_date,
                )
            )
            .group_by(func.date(User.created_at))
        )
        referrals_dict = {str(row.date): row.referrals_count for row in referrals_by_day.all()}

        # Выплаты по дням
        earnings_by_day = await db.execute(
            select(
                func.date(ReferralEarning.created_at).label('date'),
                func.sum(ReferralEarning.amount_kopeks).label('earnings'),
            )
            .where(ReferralEarning.created_at >= start_date)
            .group_by(func.date(ReferralEarning.created_at))
        )
        earnings_dict = {str(row.date): int(row.earnings or 0) for row in earnings_by_day.all()}

        result = []
        for i in range(days):
            date = (start_date + timedelta(days=i)).date()
            date_str = str(date)
            result.append(
                {
                    'date': date_str,
                    'referrals_count': referrals_dict.get(date_str, 0),
                    'earnings_kopeks': earnings_dict.get(date_str, 0),
                }
            )

        return result

    @classmethod
    async def get_top_referrers(
        cls,
        db: AsyncSession,
        limit: int = 10,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Получить топ рефереров."""
        now = datetime.now(UTC)
        start_date = now - timedelta(days=days) if days else None

        # Подсчёт рефералов и заработков
        earnings_query = select(
            ReferralEarning.user_id,
            func.sum(ReferralEarning.amount_kopeks).label('total_earnings'),
        ).group_by(ReferralEarning.user_id)
        if start_date:
            earnings_query = earnings_query.where(ReferralEarning.created_at >= start_date)

        earnings_result = await db.execute(earnings_query)
        earnings_dict = {row.user_id: int(row.total_earnings or 0) for row in earnings_result.all()}

        # Подсчёт рефералов
        referrals_query = (
            select(
                User.referred_by_id,
                func.count(User.id).label('referrals_count'),
            )
            .where(User.referred_by_id.isnot(None))
            .group_by(User.referred_by_id)
        )
        if start_date:
            referrals_query = referrals_query.where(User.created_at >= start_date)

        referrals_result = await db.execute(referrals_query)
        referrals_dict = {row.referred_by_id: row.referrals_count for row in referrals_result.all()}

        # Объединяем данные
        all_referrer_ids = set(earnings_dict.keys()) | set(referrals_dict.keys())
        referrers_data = []

        for referrer_id in all_referrer_ids:
            referrers_data.append(
                {
                    'user_id': referrer_id,
                    'referrals_count': referrals_dict.get(referrer_id, 0),
                    'total_earnings': earnings_dict.get(referrer_id, 0),
                }
            )

        # Сортируем по заработку
        referrers_data.sort(key=lambda x: x['total_earnings'], reverse=True)
        top_referrers = referrers_data[:limit]

        if not top_referrers:
            return []

        # Получаем данные всех пользователей одним запросом
        top_user_ids = [data['user_id'] for data in top_referrers]
        users_result = await db.execute(select(User).where(User.id.in_(top_user_ids)))
        users_dict = {user.id: user for user in users_result.scalars().all()}

        # Формируем результат с сохранением порядка сортировки
        result = []
        for data in top_referrers:
            user = users_dict.get(data['user_id'])
            if user:
                result.append(
                    {
                        'id': user.id,
                        'telegram_id': user.telegram_id,
                        'username': user.username,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'full_name': f'{user.first_name or ""} {user.last_name or ""}'.strip()
                        or (user.telegram_id and f'User {user.telegram_id}')
                        or user.email
                        or f'User #{user.id}',
                        'referral_code': user.referral_code,
                        'referrals_count': data['referrals_count'],
                        'total_earnings_kopeks': data['total_earnings'],
                    }
                )

        return result

    @classmethod
    async def get_per_campaign_stats(
        cls,
        db: AsyncSession,
        user_id: int,
        campaign_ids: list[int],
    ) -> dict[int, dict[str, int]]:
        """Получить статистику по каждой кампании партнёра.

        Returns:
            dict keyed by campaign_id with registrations_count, referrals_count, earnings_kopeks.
        """
        if not campaign_ids:
            return {}

        # Registrations per campaign (only users referred by this partner)
        reg_result = await db.execute(
            select(
                AdvertisingCampaignRegistration.campaign_id,
                func.count(AdvertisingCampaignRegistration.id).label('count'),
            )
            .join(User, User.id == AdvertisingCampaignRegistration.user_id)
            .where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id.in_(campaign_ids),
                    User.referred_by_id == user_id,
                )
            )
            .group_by(AdvertisingCampaignRegistration.campaign_id)
        )
        registrations_map = {row.campaign_id: int(row.count) for row in reg_result.all()}

        # Referral earnings per campaign (only for this partner)
        earnings_result = await db.execute(
            select(
                ReferralEarning.campaign_id,
                func.count(func.distinct(ReferralEarning.referral_id)).label('referrals'),
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('earnings'),
            )
            .where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id.in_(campaign_ids),
                )
            )
            .group_by(ReferralEarning.campaign_id)
        )
        earnings_map = {
            row.campaign_id: {'referrals': int(row.referrals), 'earnings': int(row.earnings)}
            for row in earnings_result.all()
        }

        result: dict[int, dict[str, int]] = {}
        for cid in campaign_ids:
            earning_data = earnings_map.get(cid, {'referrals': 0, 'earnings': 0})
            result[cid] = {
                'registrations_count': registrations_map.get(cid, 0),
                'referrals_count': earning_data['referrals'],
                'earnings_kopeks': earning_data['earnings'],
            }

        return result

    @classmethod
    async def get_campaign_detailed_stats(
        cls,
        db: AsyncSession,
        user_id: int,
        campaign_id: int,
    ) -> dict[str, Any]:
        """Detailed stats for a single campaign owned by the partner."""
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=PERIOD_COMPARISON_DAYS)
        month_ago = now - timedelta(days=DAILY_STATS_DAYS)

        # --- Summary: registrations, referrals, earnings ---
        basic = await cls.get_per_campaign_stats(db, user_id, [campaign_id])
        summary = basic.get(campaign_id, {'registrations_count': 0, 'referrals_count': 0, 'earnings_kopeks': 0})

        # --- Period earnings (today / week / month) ---
        period_earnings_result = await db.execute(
            select(
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= today_start, ReferralEarning.amount_kopeks), else_=0)),
                    0,
                ).label('today'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= week_ago, ReferralEarning.amount_kopeks), else_=0)),
                    0,
                ).label('week'),
                func.coalesce(
                    func.sum(case((ReferralEarning.created_at >= month_ago, ReferralEarning.amount_kopeks), else_=0)),
                    0,
                ).label('month'),
            ).where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id == campaign_id,
                )
            )
        )
        pe_row = period_earnings_result.one()

        # --- Daily stats (DAILY_STATS_DAYS days) ---
        start_date = now - timedelta(days=DAILY_STATS_DAYS)

        referrals_by_day = await db.execute(
            select(
                func.date(User.created_at).label('date'),
                func.count(User.id).label('count'),
            )
            .join(AdvertisingCampaignRegistration, AdvertisingCampaignRegistration.user_id == User.id)
            .where(
                and_(
                    User.referred_by_id == user_id,
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    User.created_at >= start_date,
                )
            )
            .group_by(func.date(User.created_at))
        )
        referrals_dict = {str(row.date): int(row.count) for row in referrals_by_day.all()}

        earnings_by_day = await db.execute(
            select(
                func.date(ReferralEarning.created_at).label('date'),
                func.sum(ReferralEarning.amount_kopeks).label('earnings'),
            )
            .where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id == campaign_id,
                    ReferralEarning.created_at >= start_date,
                )
            )
            .group_by(func.date(ReferralEarning.created_at))
        )
        earnings_dict = {str(row.date): int(row.earnings or 0) for row in earnings_by_day.all()}

        daily_stats = []
        for i in range(DAILY_STATS_DAYS):
            date = (start_date + timedelta(days=i)).date()
            date_str = str(date)
            daily_stats.append(
                {
                    'date': date_str,
                    'referrals_count': referrals_dict.get(date_str, 0),
                    'earnings_kopeks': earnings_dict.get(date_str, 0),
                }
            )

        # --- Period comparison (this week vs last week) ---
        previous_start = week_ago - timedelta(days=PERIOD_COMPARISON_DAYS)

        current_ref_result = await db.execute(
            select(func.count(User.id))
            .join(AdvertisingCampaignRegistration, AdvertisingCampaignRegistration.user_id == User.id)
            .where(
                and_(
                    User.referred_by_id == user_id,
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    User.created_at >= week_ago,
                )
            )
        )
        current_referrals = current_ref_result.scalar() or 0

        previous_ref_result = await db.execute(
            select(func.count(User.id))
            .join(AdvertisingCampaignRegistration, AdvertisingCampaignRegistration.user_id == User.id)
            .where(
                and_(
                    User.referred_by_id == user_id,
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    User.created_at >= previous_start,
                    User.created_at < week_ago,
                )
            )
        )
        previous_referrals = previous_ref_result.scalar() or 0

        current_earn = await db.execute(
            select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id == campaign_id,
                    ReferralEarning.created_at >= week_ago,
                )
            )
        )
        current_earnings = int(current_earn.scalar() or 0)

        previous_earn = await db.execute(
            select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id == campaign_id,
                    ReferralEarning.created_at >= previous_start,
                    ReferralEarning.created_at < week_ago,
                )
            )
        )
        previous_earnings = int(previous_earn.scalar() or 0)

        period_comparison = {
            'current': {
                'days': PERIOD_COMPARISON_DAYS,
                'referrals_count': current_referrals,
                'earnings_kopeks': current_earnings,
            },
            'previous': {
                'days': PERIOD_COMPARISON_DAYS,
                'referrals_count': previous_referrals,
                'earnings_kopeks': previous_earnings,
            },
            'referrals_change': _calc_change(current_referrals, previous_referrals),
            'earnings_change': _calc_change(current_earnings, previous_earnings),
        }

        # --- Top referrals for this campaign ---
        top_result = await db.execute(
            select(
                User.id,
                User.username,
                User.first_name,
                User.last_name,
                User.created_at,
                User.has_made_first_topup,
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('total_earnings'),
            )
            .join(AdvertisingCampaignRegistration, AdvertisingCampaignRegistration.user_id == User.id)
            .outerjoin(
                ReferralEarning,
                and_(
                    ReferralEarning.referral_id == User.id,
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.campaign_id == campaign_id,
                ),
            )
            .where(
                and_(
                    User.referred_by_id == user_id,
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                )
            )
            .group_by(User.id)
            .order_by(desc(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)))
            .limit(TOP_REFERRALS_LIMIT)
        )
        top_rows = top_result.all()

        # Check active subscriptions for top referrals
        top_user_ids = [row.id for row in top_rows]
        active_subs: set[int] = set()
        if top_user_ids:
            active_result = await db.execute(
                select(Subscription.user_id).where(
                    and_(
                        Subscription.user_id.in_(top_user_ids),
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.end_date > now,
                    )
                )
            )
            active_subs = {row.user_id for row in active_result.all()}

        top_referrals = []
        for row in top_rows:
            full_name = f'{row.first_name or ""} {row.last_name or ""}'.strip() or row.username or f'User #{row.id}'
            top_referrals.append(
                {
                    'id': row.id,
                    'full_name': full_name,
                    'created_at': row.created_at,
                    'has_paid': row.has_made_first_topup,
                    'is_active': row.id in active_subs,
                    'total_earnings_kopeks': int(row.total_earnings),
                }
            )

        # --- Conversion rate ---
        reg_count = summary['registrations_count']
        ref_count = summary['referrals_count']
        conversion_rate = round((ref_count / reg_count * 100), 2) if reg_count > 0 else 0.0

        return {
            'campaign_id': campaign_id,
            'registrations_count': reg_count,
            'referrals_count': ref_count,
            'earnings_kopeks': summary['earnings_kopeks'],
            'conversion_rate': conversion_rate,
            'earnings_today': int(pe_row.today),
            'earnings_week': int(pe_row.week),
            'earnings_month': int(pe_row.month),
            'daily_stats': daily_stats,
            'period_comparison': period_comparison,
            'top_referrals': top_referrals,
        }

    @classmethod
    async def get_admin_campaign_chart_data(
        cls,
        db: AsyncSession,
        campaign_id: int,
    ) -> dict[str, Any]:
        """Chart data for admin campaign analytics (no partner filter).

        Unlike get_campaign_detailed_stats which is partner-isolated,
        this method returns ALL registrations and revenue for a campaign.
        Revenue is based on actual user transactions (deposits + subscription payments),
        not referral earnings.
        """
        now = datetime.now(UTC)
        start_date = now - timedelta(days=DAILY_STATS_DAYS)
        week_ago = now - timedelta(days=PERIOD_COMPARISON_DAYS)
        previous_start = week_ago - timedelta(days=PERIOD_COMPARISON_DAYS)

        # Subquery: user_ids registered via this campaign
        campaign_user_ids_sq = (
            select(AdvertisingCampaignRegistration.user_id)
            .where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
            .scalar_subquery()
        )

        # --- Daily registrations (DAILY_STATS_DAYS days) ---
        registrations_by_day = await db.execute(
            select(
                func.date(AdvertisingCampaignRegistration.created_at).label('date'),
                func.count(AdvertisingCampaignRegistration.id).label('count'),
            )
            .where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    AdvertisingCampaignRegistration.created_at >= start_date,
                )
            )
            .group_by(func.date(AdvertisingCampaignRegistration.created_at))
        )
        registrations_dict = {str(row.date): int(row.count) for row in registrations_by_day.all()}

        # --- Daily revenue (DAILY_STATS_DAYS days) ---
        # Revenue = real deposits only (exclude bonus/promo balance spending on subscriptions)
        revenue_amount_expr = func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            Transaction.type == TransactionType.DEPOSIT.value,
                            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                        ),
                        Transaction.amount_kopeks,
                    ),
                    else_=0,
                )
            ),
            0,
        )

        revenue_by_day = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                revenue_amount_expr.label('revenue'),
            )
            .where(
                and_(
                    Transaction.user_id.in_(campaign_user_ids_sq),
                    Transaction.is_completed.is_(True),
                    Transaction.created_at >= start_date,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
            .group_by(func.date(Transaction.created_at))
        )
        revenue_dict = {str(row.date): int(row.revenue) for row in revenue_by_day.all()}

        # --- Combine into daily_stats ---
        daily_stats: list[dict[str, Any]] = []
        for i in range(DAILY_STATS_DAYS):
            date = (start_date + timedelta(days=i)).date()
            date_str = str(date)
            daily_stats.append(
                {
                    'date': date_str,
                    'referrals_count': registrations_dict.get(date_str, 0),
                    'earnings_kopeks': revenue_dict.get(date_str, 0),
                }
            )

        # --- Period comparison (this week vs last week) ---
        # Current period registrations
        current_reg_result = await db.execute(
            select(func.count(AdvertisingCampaignRegistration.id)).where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    AdvertisingCampaignRegistration.created_at >= week_ago,
                )
            )
        )
        current_registrations = current_reg_result.scalar() or 0

        # Previous period registrations
        previous_reg_result = await db.execute(
            select(func.count(AdvertisingCampaignRegistration.id)).where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id == campaign_id,
                    AdvertisingCampaignRegistration.created_at >= previous_start,
                    AdvertisingCampaignRegistration.created_at < week_ago,
                )
            )
        )
        previous_registrations = previous_reg_result.scalar() or 0

        # Current period revenue
        current_rev_result = await db.execute(
            select(revenue_amount_expr.label('revenue')).where(
                and_(
                    Transaction.user_id.in_(campaign_user_ids_sq),
                    Transaction.is_completed.is_(True),
                    Transaction.created_at >= week_ago,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
        )
        current_revenue = int(current_rev_result.scalar() or 0)

        # Previous period revenue
        previous_rev_result = await db.execute(
            select(revenue_amount_expr.label('revenue')).where(
                and_(
                    Transaction.user_id.in_(campaign_user_ids_sq),
                    Transaction.is_completed.is_(True),
                    Transaction.created_at >= previous_start,
                    Transaction.created_at < week_ago,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
        )
        previous_revenue = int(previous_rev_result.scalar() or 0)

        period_comparison = {
            'current': {
                'days': PERIOD_COMPARISON_DAYS,
                'referrals_count': current_registrations,
                'earnings_kopeks': current_revenue,
            },
            'previous': {
                'days': PERIOD_COMPARISON_DAYS,
                'referrals_count': previous_registrations,
                'earnings_kopeks': previous_revenue,
            },
            'referrals_change': _calc_change(current_registrations, previous_registrations),
            'earnings_change': _calc_change(current_revenue, previous_revenue),
        }

        # --- Total deposits & spending (separate aggregates) ---
        totals_result = await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    Transaction.type == TransactionType.DEPOSIT.value,
                                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                                ),
                                Transaction.amount_kopeks,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('deposits'),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                                func.abs(Transaction.amount_kopeks),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('spending'),
            ).where(
                and_(
                    Transaction.user_id.in_(campaign_user_ids_sq),
                    Transaction.is_completed.is_(True),
                    Transaction.type.in_(
                        [
                            TransactionType.DEPOSIT.value,
                            TransactionType.SUBSCRIPTION_PAYMENT.value,
                        ]
                    ),
                )
            )
        )
        totals_row = totals_result.one()
        total_deposits_kopeks = int(totals_row.deposits)
        total_spending_kopeks = int(totals_row.spending)

        # --- Top registrations (top 5 users by spending) ---
        top_result = await db.execute(
            select(
                User.id,
                User.username,
                User.first_name,
                User.last_name,
                User.created_at,
                User.has_had_paid_subscription,
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    Transaction.type == TransactionType.DEPOSIT.value,
                                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                                ),
                                Transaction.amount_kopeks,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('total_spending'),
            )
            .join(AdvertisingCampaignRegistration, AdvertisingCampaignRegistration.user_id == User.id)
            .outerjoin(
                Transaction,
                and_(
                    Transaction.user_id == User.id,
                    Transaction.is_completed.is_(True),
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                ),
            )
            .where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
            .group_by(User.id)
            .order_by(
                desc(
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    and_(
                                        Transaction.type == TransactionType.DEPOSIT.value,
                                        Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                                    ),
                                    Transaction.amount_kopeks,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    )
                )
            )
            .limit(TOP_REFERRALS_LIMIT)
        )
        top_rows = top_result.all()

        # Batch-check active subscriptions to avoid N+1
        top_user_ids = [row.id for row in top_rows]
        active_subs: set[int] = set()
        if top_user_ids:
            active_result = await db.execute(
                select(Subscription.user_id).where(
                    and_(
                        Subscription.user_id.in_(top_user_ids),
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.end_date > now,
                    )
                )
            )
            active_subs = {row.user_id for row in active_result.all()}

        top_registrations: list[dict[str, Any]] = []
        for row in top_rows:
            full_name = f'{row.first_name or ""} {row.last_name or ""}'.strip() or row.username or f'User #{row.id}'
            top_registrations.append(
                {
                    'id': row.id,
                    'full_name': full_name,
                    'created_at': row.created_at,
                    'has_paid': row.has_had_paid_subscription,
                    'is_active': row.id in active_subs,
                    'total_earnings_kopeks': int(row.total_spending),
                }
            )

        return {
            'campaign_id': campaign_id,
            'total_deposits_kopeks': total_deposits_kopeks,
            'total_spending_kopeks': total_spending_kopeks,
            'daily_stats': daily_stats,
            'period_comparison': period_comparison,
            'top_registrations': top_registrations,
        }

    @classmethod
    async def _get_earnings_for_period(
        cls,
        db: AsyncSession,
        user_id: int,
        start_date: datetime | None,
        end_date: datetime | None = None,
    ) -> int:
        """Получить заработки за период."""
        query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0)).where(
            ReferralEarning.user_id == user_id
        )

        if start_date:
            query = query.where(ReferralEarning.created_at >= start_date)
        if end_date:
            query = query.where(ReferralEarning.created_at < end_date)

        result = await db.execute(query)
        return int(result.scalar() or 0)

    @classmethod
    async def _get_total_earnings(
        cls,
        db: AsyncSession,
        start_date: datetime | None,
    ) -> int:
        """Получить общие выплаты за период."""
        query = select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0))

        if start_date:
            query = query.where(ReferralEarning.created_at >= start_date)

        result = await db.execute(query)
        return int(result.scalar() or 0)
