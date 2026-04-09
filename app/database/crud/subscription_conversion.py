from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SubscriptionConversion, User


logger = structlog.get_logger(__name__)


async def create_subscription_conversion(
    db: AsyncSession,
    user_id: int,
    trial_duration_days: int,
    payment_method: str,
    first_payment_amount_kopeks: int,
    first_paid_period_days: int,
) -> SubscriptionConversion:
    conversion = SubscriptionConversion(
        user_id=user_id,
        converted_at=datetime.now(UTC),
        trial_duration_days=trial_duration_days,
        payment_method=payment_method,
        first_payment_amount_kopeks=first_payment_amount_kopeks,
        first_paid_period_days=first_paid_period_days,
    )

    db.add(conversion)
    await db.commit()
    await db.refresh(conversion)

    logger.info(
        '✅ Создана запись о конверсии для пользователя дн. → дн. за ₽',
        user_id=user_id,
        trial_duration_days=trial_duration_days,
        first_paid_period_days=first_paid_period_days,
        first_payment_amount_kopeks=first_payment_amount_kopeks / 100,
    )

    return conversion


async def get_conversion_by_user_id(db: AsyncSession, user_id: int) -> SubscriptionConversion | None:
    result = await db.execute(
        select(SubscriptionConversion)
        .where(SubscriptionConversion.user_id == user_id)
        .order_by(SubscriptionConversion.converted_at.desc())
        .limit(1)
    )

    return result.scalar_one_or_none()


async def get_conversion_statistics(db: AsyncSession) -> dict:
    from app.database.models import Subscription

    # Получаем количество записей о конверсиях в таблице
    total_conversions_result = await db.execute(select(func.count(SubscriptionConversion.id)))
    total_conversions = total_conversions_result.scalar() or 0

    # Подсчитываем пользователей с платными подписками
    users_with_paid_result = await db.execute(
        select(func.count(User.id)).where(User.has_had_paid_subscription.is_(True))
    )
    users_with_paid = users_with_paid_result.scalar() or 0

    # Подсчитываем всех пользователей с подписками (использовавших триал)
    total_users_with_subscriptions_result = await db.execute(select(func.count(func.distinct(Subscription.user_id))))
    total_users_with_subscriptions = total_users_with_subscriptions_result.scalar() or 0

    # Расчёт конверсии: (оплатившие) / (всего с подписками) * 100
    # Знаменатель = все юзеры с подписками (включая уже конвертированных)
    if total_users_with_subscriptions > 0:
        conversion_rate = round((users_with_paid / total_users_with_subscriptions) * 100, 1)
    else:
        conversion_rate = 0.0

    avg_trial_duration_result = await db.execute(select(func.avg(SubscriptionConversion.trial_duration_days)))
    avg_trial_duration = avg_trial_duration_result.scalar() or 0

    avg_first_payment_result = await db.execute(select(func.avg(SubscriptionConversion.first_payment_amount_kopeks)))
    avg_first_payment = avg_first_payment_result.scalar() or 0

    month_ago = datetime.now(UTC) - timedelta(days=30)
    month_conversions_result = await db.execute(
        select(func.count(SubscriptionConversion.id)).where(SubscriptionConversion.converted_at >= month_ago)
    )
    month_conversions = month_conversions_result.scalar() or 0

    logger.info('📊 Статистика конверсий:')
    logger.info('Всего пользователей с подписками', total_users_with_subscriptions=total_users_with_subscriptions)
    logger.info('Оплативших подписку', users_with_paid=users_with_paid)
    logger.info('Рассчитанная конверсия', conversion_rate=conversion_rate)

    return {
        'total_conversions': total_conversions,
        'conversion_rate': conversion_rate,
        'users_with_trial': total_users_with_subscriptions,
        'converted_users': users_with_paid,
        'avg_trial_duration_days': round(avg_trial_duration, 1),
        'avg_first_payment_rubles': round((avg_first_payment or 0) / 100, 2),
        'month_conversions': month_conversions,
    }


async def get_users_had_trial_count(db: AsyncSession) -> int:
    conversions_count_result = await db.execute(select(func.count(func.distinct(SubscriptionConversion.user_id))))
    conversions_count = conversions_count_result.scalar()

    paid_users_result = await db.execute(select(func.count(User.id)).where(User.has_had_paid_subscription == True))
    paid_users_count = paid_users_result.scalar()

    return max(conversions_count, paid_users_count)
