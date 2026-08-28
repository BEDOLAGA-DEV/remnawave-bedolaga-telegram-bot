import secrets
import string
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.referral import not_referee_directed
from app.database.models import ReferralEarning, Subscription, SubscriptionStatus, Transaction, TransactionType, User
from app.utils.formatters import format_username_link


logger = structlog.get_logger(__name__)


def format_referrer_info(user: User) -> str:
    """Return formatted referrer info for admin notifications."""

    referred_by_id = getattr(user, 'referred_by_id', None)

    if not referred_by_id:
        return 'Нет'

    try:
        # Проверяем, является ли referrer обычным объектом или InstrumentedList
        # getattr default does NOT catch MissingGreenlet (not an AttributeError),
        # so we wrap in try/except to handle lazy-load failures in async context.
        referrer = getattr(user, 'referrer', None)

        # Если referrer это InstrumentedList или None, то возвращаем информацию по ID
        if referrer is None:
            return f'ID {referred_by_id} (не найден)'

        # Пытаемся получить атрибуты referrer, если они доступны
        referrer_username = getattr(referrer, 'username', None)
        referrer_telegram_id = getattr(referrer, 'telegram_id', None)

        if referrer_username:
            # Тот же вид, что и у близнеца AdminNotificationService._get_referrer_info:
            # иначе в одном уведомлении о пополнении плательщик кликабелен, а реферер нет.
            return f'{format_username_link(referrer_username)} (ID: {referred_by_id})'

        return f'ID {referrer_telegram_id or referred_by_id}'

    except Exception:
        # MissingGreenlet is not a subclass of AttributeError/TypeError,
        # so we must catch broadly to handle lazy-load failures in async context.
        return f'ID {referred_by_id} (ошибка загрузки)'


async def generate_unique_referral_code(db: AsyncSession, telegram_id: int) -> str:
    max_attempts = 10

    for _ in range(max_attempts):
        code = f'ref{"".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))}'

        result = await db.execute(select(User).where(User.referral_code == code))
        if not result.scalar_one_or_none():
            return code

    timestamp = str(int(datetime.now(UTC).timestamp()))[-6:]
    return f'ref{timestamp}'


def get_effective_referral_commission_percent(user: User) -> int:
    """Возвращает индивидуальный процент комиссии пользователя или дефолтное значение."""

    percent = getattr(user, 'referral_commission_percent', None)
    source = 'user' if percent is not None else 'default'

    if percent is None:
        percent = settings.REFERRAL_COMMISSION_PERCENT

    if percent < 0 or percent > 100:
        user_id_display = (
            getattr(user, 'telegram_id', None) or getattr(user, 'email', None) or f'#{getattr(user, "id", "unknown")}'
        )
        logger.error('❌ Некорректный процент комиссии', user_id_display=user_id_display, percent=percent)
        return max(0, min(100, settings.REFERRAL_COMMISSION_PERCENT))

    logger.debug(
        'Определён процент комиссии',
        user_id=getattr(user, 'id', None),
        commission_percent=percent,
        source=source,
    )
    return percent


async def mark_user_as_had_paid_subscription(db: AsyncSession, user: User) -> bool:
    try:
        if user.has_had_paid_subscription:
            logger.debug('Пользователь уже отмечен как имевший платную подписку', user_id=user.id)
            return True

        async with db.begin_nested():
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(has_had_paid_subscription=True, updated_at=datetime.now(UTC))
            )

        logger.info('✅ Пользователь отмечен как имевший платную подписку', user_id=user.id)
        return True

    except Exception as e:
        logger.error('Ошибка отметки пользователя как имевшего платную подписку', user_id=user.id, error=e)
        return False


async def get_user_referral_summary(db: AsyncSession, user_id: int) -> dict:
    try:
        invited_count_result = await db.execute(select(func.count(User.id)).where(User.referred_by_id == user_id))
        invited_count = invited_count_result.scalar() or 0

        referrals_result = await db.execute(select(User).where(User.referred_by_id == user_id))
        referrals = referrals_result.scalars().all()

        paid_referrals_count = sum(1 for ref in referrals if ref.has_made_first_topup)

        # Дни идут отдельной суммой: в копейках такая награда равна нулю, и без
        # своего итога пользователь, которому программа платит днями, видит на
        # экране «заработано 0 ₽» при регулярно приходящих начислениях.
        # not_referee_directed() обязателен именно из-за дней. Строка награды
        # ПРИГЛАШЁННОМУ принадлежит ему самому, и без фильтра пользователь,
        # не пригласивший никого, видит «Приглашено: 0 · Заработано: 7 дн.»,
        # где вторая сторона пары — его собственный пригласивший.
        total_earnings_result = await db.execute(
            select(
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                func.coalesce(func.sum(ReferralEarning.days_granted), 0),
            ).where(ReferralEarning.user_id == user_id, not_referee_directed())
        )
        total_earned_kopeks, total_earned_days = total_earnings_result.one()

        month_ago = datetime.now(UTC) - timedelta(days=30)
        month_earnings_result = await db.execute(
            select(
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                func.coalesce(func.sum(ReferralEarning.days_granted), 0),
            ).where(
                and_(
                    ReferralEarning.user_id == user_id,
                    ReferralEarning.created_at >= month_ago,
                    not_referee_directed(),
                )
            )
        )
        month_earned_kopeks, month_earned_days = month_earnings_result.one()

        recent_earnings_result = await db.execute(
            select(ReferralEarning)
            .options(selectinload(ReferralEarning.referral))
            .where(ReferralEarning.user_id == user_id, not_referee_directed())
            .order_by(ReferralEarning.created_at.desc())
            .limit(5)
        )
        recent_earnings_raw = recent_earnings_result.scalars().all()

        recent_earnings = []
        for earning in recent_earnings_raw:
            if earning.referral:
                recent_earnings.append(
                    {
                        'amount_kopeks': earning.amount_kopeks,
                        'reason': earning.reason,
                        'referral_name': earning.referral.full_name,
                        'created_at': earning.created_at,
                        'days_granted': int(getattr(earning, 'days_granted', 0) or 0),
                        'level': int(getattr(earning, 'level', 1) or 1),
                        'reward_type': getattr(earning, 'reward_type', 'money'),
                        'tariff_id': getattr(earning, 'tariff_id', None),
                    }
                )

        earnings_by_type = {}
        earnings_by_type_result = await db.execute(
            select(
                ReferralEarning.reason,
                func.count(ReferralEarning.id).label('count'),
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('total_amount'),
                func.coalesce(func.sum(ReferralEarning.days_granted), 0).label('total_days'),
            )
            .where(ReferralEarning.user_id == user_id, not_referee_directed())
            .group_by(ReferralEarning.reason)
        )

        for row in earnings_by_type_result:
            earnings_by_type[row.reason] = {
                'count': row.count,
                'total_amount_kopeks': row.total_amount,
                'total_days': int(row.total_days or 0),
            }

        active_result = await db.execute(
            select(func.count(func.distinct(User.id)))
            .join(Subscription, User.id == Subscription.user_id)
            .where(
                User.referred_by_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > func.now(),
            )
        )
        active_referrals_count = active_result.scalar() or 0

        return {
            'invited_count': invited_count,
            'paid_referrals_count': paid_referrals_count,
            'active_referrals_count': active_referrals_count,
            'total_earned_kopeks': total_earned_kopeks,
            'total_earned_days': int(total_earned_days or 0),
            'month_earned_kopeks': month_earned_kopeks,
            'month_earned_days': int(month_earned_days or 0),
            'recent_earnings': recent_earnings,
            'earnings_by_type': earnings_by_type,
            'conversion_rate': round((paid_referrals_count / invited_count * 100) if invited_count > 0 else 0, 1),
        }

    except Exception as e:
        logger.error('Ошибка получения статистики рефералов для пользователя', user_id=user_id, error=e)
        return {
            'invited_count': 0,
            'paid_referrals_count': 0,
            'active_referrals_count': 0,
            'total_earned_kopeks': 0,
            'total_earned_days': 0,
            'month_earned_kopeks': 0,
            'month_earned_days': 0,
            'recent_earnings': [],
            'earnings_by_type': {},
            'conversion_rate': 0.0,
        }


async def get_detailed_referral_list(db: AsyncSession, user_id: int, limit: int = 20, offset: int = 0) -> dict:
    try:
        referrals_result = await db.execute(
            select(User)
            .where(User.referred_by_id == user_id)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        referrals = referrals_result.scalars().all()

        total_count_result = await db.execute(select(func.count(User.id)).where(User.referred_by_id == user_id))
        total_count = total_count_result.scalar() or 0

        detailed_referrals = []
        for referral in referrals:
            earnings_result = await db.execute(
                select(
                    func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                    func.coalesce(func.sum(ReferralEarning.days_granted), 0),
                ).where(
                    and_(
                        ReferralEarning.user_id == user_id,
                        ReferralEarning.referral_id == referral.id,
                        not_referee_directed(),
                    )
                )
            )
            total_earned_from_referral, days_earned_from_referral = earnings_result.one()

            topups_result = await db.execute(
                select(func.count(Transaction.id)).where(
                    and_(
                        Transaction.user_id == referral.id,
                        Transaction.type == TransactionType.DEPOSIT.value,
                        Transaction.is_completed.is_(True),
                    )
                )
            )
            topups_count = topups_result.scalar() or 0

            days_since_registration = (datetime.now(UTC) - referral.created_at).days

            days_since_activity = None
            if referral.last_activity:
                days_since_activity = (datetime.now(UTC) - referral.last_activity).days

            detailed_referrals.append(
                {
                    'id': referral.id,
                    'telegram_id': referral.telegram_id,
                    'full_name': referral.full_name,
                    'username': referral.username,
                    'created_at': referral.created_at,
                    'days_earned': int(days_earned_from_referral or 0),
                    'last_activity': referral.last_activity,
                    'has_made_first_topup': referral.has_made_first_topup,
                    'balance_kopeks': referral.balance_kopeks,
                    'total_earned_kopeks': total_earned_from_referral,
                    'topups_count': topups_count,
                    'days_since_registration': days_since_registration,
                    'days_since_activity': days_since_activity,
                    'status': 'active' if days_since_activity is not None and days_since_activity <= 30 else 'inactive',
                }
            )

        return {
            'referrals': detailed_referrals,
            'total_count': total_count,
            'has_next': offset + limit < total_count,
            'has_prev': offset > 0,
            'current_page': (offset // limit) + 1,
            'total_pages': (total_count + limit - 1) // limit,
        }

    except Exception as e:
        logger.error('Ошибка получения списка рефералов для пользователя', user_id=user_id, error=e)
        return {
            'referrals': [],
            'total_count': 0,
            'has_next': False,
            'has_prev': False,
            'current_page': 1,
            'total_pages': 1,
        }


async def get_referral_analytics(db: AsyncSession, user_id: int) -> dict:
    try:
        now = datetime.now(UTC)
        periods = {
            'today': now.replace(hour=0, minute=0, second=0, microsecond=0),
            'week': now - timedelta(days=7),
            'month': now - timedelta(days=30),
            'quarter': now - timedelta(days=90),
        }

        # Дни считаются рядом с деньгами: на «дневной» программе все периоды
        # показывали ноль при регулярно приходящих начислениях.
        earnings_by_period = {}
        days_by_period = {}
        for period_name, start_date in periods.items():
            result = await db.execute(
                select(
                    func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                    func.coalesce(func.sum(ReferralEarning.days_granted), 0),
                ).where(
                    and_(
                        ReferralEarning.user_id == user_id,
                        ReferralEarning.created_at >= start_date,
                        not_referee_directed(),
                    )
                )
            )
            period_money, period_days = result.one()
            earnings_by_period[period_name] = period_money or 0
            days_by_period[period_name] = int(period_days or 0)

        top_referrals_result = await db.execute(
            select(
                ReferralEarning.referral_id,
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0).label('total_earned'),
                func.coalesce(func.sum(ReferralEarning.days_granted), 0).label('total_days'),
                func.count(ReferralEarning.id).label('earnings_count'),
            )
            .where(ReferralEarning.user_id == user_id, not_referee_directed())
            .group_by(ReferralEarning.referral_id)
            # Дни участвуют в сортировке: на «дневной» программе все суммы равны
            # нулю, и в топ-5 попадали случайные пятеро, а реферал, принёсший
            # больше всех дней, из списка выпадал.
            .order_by(
                func.sum(ReferralEarning.amount_kopeks).desc(),
                func.sum(ReferralEarning.days_granted).desc(),
            )
            .limit(5)
        )

        top_referrals = []
        for row in top_referrals_result:
            referral_result = await db.execute(select(User).where(User.id == row.referral_id))
            referral = referral_result.scalar_one_or_none()
            if referral:
                top_referrals.append(
                    {
                        'referral_name': referral.full_name,
                        'total_earned_kopeks': row.total_earned,
                        'total_earned_days': int(row.total_days or 0),
                        'earnings_count': row.earnings_count,
                    }
                )

        return {
            'earnings_by_period': earnings_by_period,
            'days_by_period': days_by_period,
            'top_referrals': top_referrals,
        }

    except Exception as e:
        logger.error('Ошибка получения аналитики рефералов для пользователя', user_id=user_id, error=e)
        return {
            'earnings_by_period': {'today': 0, 'week': 0, 'month': 0, 'quarter': 0},
            'days_by_period': {'today': 0, 'week': 0, 'month': 0, 'quarter': 0},
            'top_referrals': [],
        }
