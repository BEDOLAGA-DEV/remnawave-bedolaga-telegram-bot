from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Subscription,
    SubscriptionConversion,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)


async def create_campaign(
    db: AsyncSession,
    *,
    name: str,
    start_parameter: str,
    bonus_type: str,
    created_by: int | None = None,
    balance_bonus_kopeks: int = 0,
    subscription_duration_days: int | None = None,
    subscription_traffic_gb: int | None = None,
    subscription_device_limit: int | None = None,
    subscription_squads: list[str] | None = None,
    # Поля для типа "tariff"
    tariff_id: int | None = None,
    tariff_duration_days: int | None = None,
    is_active: bool = True,
    partner_user_id: int | None = None,
) -> AdvertisingCampaign:
    campaign = AdvertisingCampaign(
        name=name,
        start_parameter=start_parameter,
        bonus_type=bonus_type,
        balance_bonus_kopeks=balance_bonus_kopeks or 0,
        subscription_duration_days=subscription_duration_days,
        subscription_traffic_gb=subscription_traffic_gb,
        subscription_device_limit=subscription_device_limit,
        subscription_squads=subscription_squads or [],
        tariff_id=tariff_id,
        tariff_duration_days=tariff_duration_days,
        created_by=created_by,
        is_active=is_active,
        partner_user_id=partner_user_id,
    )

    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    logger.info(
        '📣 Создана рекламная кампания (start bonus=)',
        campaign_name=campaign.name,
        start_parameter=campaign.start_parameter,
        bonus_type=campaign.bonus_type,
    )
    return campaign


async def get_campaign_by_id(db: AsyncSession, campaign_id: int) -> AdvertisingCampaign | None:
    result = await db.execute(
        select(AdvertisingCampaign)
        .options(
            selectinload(AdvertisingCampaign.registrations),
            selectinload(AdvertisingCampaign.tariff),
            selectinload(AdvertisingCampaign.partner),
        )
        .where(AdvertisingCampaign.id == campaign_id)
    )
    return result.scalar_one_or_none()


async def get_campaign_by_start_parameter(
    db: AsyncSession,
    start_parameter: str,
    *,
    only_active: bool = False,
) -> AdvertisingCampaign | None:
    stmt = select(AdvertisingCampaign).where(AdvertisingCampaign.start_parameter == start_parameter)
    if only_active:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(True))

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_campaigns_list(
    db: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 20,
    include_inactive: bool = True,
) -> list[AdvertisingCampaign]:
    stmt = (
        select(AdvertisingCampaign)
        .options(
            selectinload(AdvertisingCampaign.tariff),
            selectinload(AdvertisingCampaign.partner),
            selectinload(AdvertisingCampaign.registrations),
        )
        .order_by(AdvertisingCampaign.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if not include_inactive:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(True))

    result = await db.execute(stmt)
    return result.scalars().all()


async def get_campaigns_count(db: AsyncSession, *, is_active: bool | None = None) -> int:
    stmt = select(func.count(AdvertisingCampaign.id))
    if is_active is not None:
        stmt = stmt.where(AdvertisingCampaign.is_active.is_(is_active))

    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def update_campaign(
    db: AsyncSession,
    campaign: AdvertisingCampaign,
    **kwargs,
) -> AdvertisingCampaign:
    allowed_fields = {
        'name',
        'start_parameter',
        'bonus_type',
        'balance_bonus_kopeks',
        'subscription_duration_days',
        'subscription_traffic_gb',
        'subscription_device_limit',
        'subscription_squads',
        'tariff_id',
        'tariff_duration_days',
        'is_active',
        'partner_user_id',
    }

    nullable_fields = {
        'partner_user_id',
        'tariff_id',
        'subscription_duration_days',
        'subscription_traffic_gb',
        'subscription_device_limit',
        'tariff_duration_days',
    }

    update_data = {}
    for key, value in kwargs.items():
        if key not in allowed_fields:
            continue
        if value is None and key not in nullable_fields:
            continue
        update_data[key] = value

    if not update_data:
        return campaign

    update_data['updated_at'] = datetime.now(UTC)

    await db.execute(update(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign.id).values(**update_data))
    await db.commit()
    await db.refresh(campaign)

    logger.info('✏️ Обновлена рекламная кампания', campaign_name=campaign.name, update_data=update_data)
    return campaign


async def delete_campaign(db: AsyncSession, campaign: AdvertisingCampaign) -> bool:
    await db.execute(delete(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign.id))
    await db.commit()
    logger.info('🗑️ Удалена рекламная кампания', campaign_name=campaign.name)
    return True


async def get_campaign_registration_by_user(
    db: AsyncSession,
    user_id: int,
) -> AdvertisingCampaignRegistration | None:
    result = await db.execute(
        select(AdvertisingCampaignRegistration)
        .options(selectinload(AdvertisingCampaignRegistration.campaign))
        .where(AdvertisingCampaignRegistration.user_id == user_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def record_campaign_registration(
    db: AsyncSession,
    *,
    campaign_id: int,
    user_id: int,
    bonus_type: str,
    balance_bonus_kopeks: int = 0,
    subscription_duration_days: int | None = None,
    tariff_id: int | None = None,
    tariff_duration_days: int | None = None,
) -> AdvertisingCampaignRegistration:
    existing = await db.execute(
        select(AdvertisingCampaignRegistration).where(
            and_(
                AdvertisingCampaignRegistration.campaign_id == campaign_id,
                AdvertisingCampaignRegistration.user_id == user_id,
            )
        )
    )
    registration = existing.scalar_one_or_none()
    if registration:
        return registration

    registration = AdvertisingCampaignRegistration(
        campaign_id=campaign_id,
        user_id=user_id,
        bonus_type=bonus_type,
        balance_bonus_kopeks=balance_bonus_kopeks or 0,
        subscription_duration_days=subscription_duration_days,
        tariff_id=tariff_id,
        tariff_duration_days=tariff_duration_days,
    )
    db.add(registration)
    await db.commit()
    await db.refresh(registration)

    logger.info('📈 Регистрируем пользователя в кампании', user_id=user_id, campaign_id=campaign_id)
    return registration


async def get_campaign_statistics(
    db: AsyncSession,
    campaign_id: int,
) -> dict[str, int | None]:
    registrations_query = select(AdvertisingCampaignRegistration.user_id).where(
        AdvertisingCampaignRegistration.campaign_id == campaign_id
    )
    registrations_subquery = registrations_query.subquery()

    result = await db.execute(
        select(
            func.count(AdvertisingCampaignRegistration.id),
            func.coalesce(func.sum(AdvertisingCampaignRegistration.balance_bonus_kopeks), 0),
            func.max(AdvertisingCampaignRegistration.created_at),
        ).where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
    )
    count, total_balance, last_registration = result.one()
    count = count or 0
    total_balance = total_balance or 0

    subscription_count_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            and_(
                AdvertisingCampaignRegistration.campaign_id == campaign_id,
                AdvertisingCampaignRegistration.bonus_type == 'subscription',
            )
        )
    )
    subscription_bonuses_issued = subscription_count_result.scalar() or 0

    # Only count real deposits (exclude promo bonuses, wheel prizes, admin top-ups)
    deposits_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            Transaction.user_id.in_(select(registrations_subquery.c.user_id)),
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
    )
    deposits_total = deposits_result.scalar() or 0

    trials_result = await db.execute(
        select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.user_id.in_(select(registrations_subquery.c.user_id)),
            Subscription.is_trial.is_(True),
        )
    )
    trial_users_count = trials_result.scalar() or 0

    active_trials_result = await db.execute(
        select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.user_id.in_(select(registrations_subquery.c.user_id)),
            Subscription.is_trial.is_(True),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
        )
    )
    active_trials_count = active_trials_result.scalar() or 0

    conversions_result = await db.execute(
        select(func.count(func.distinct(SubscriptionConversion.user_id))).where(
            SubscriptionConversion.user_id.in_(select(registrations_subquery.c.user_id))
        )
    )
    conversion_count = conversions_result.scalar() or 0

    paid_users_result = await db.execute(
        select(func.count(User.id)).where(
            User.id.in_(select(registrations_subquery.c.user_id)),
            User.has_had_paid_subscription.is_(True),
        )
    )
    paid_users_from_flag = paid_users_result.scalar() or 0

    conversions_rows = await db.execute(
        select(
            SubscriptionConversion.user_id,
            SubscriptionConversion.first_payment_amount_kopeks,
            SubscriptionConversion.converted_at,
        )
        .where(SubscriptionConversion.user_id.in_(select(registrations_subquery.c.user_id)))
        .order_by(SubscriptionConversion.converted_at)
    )
    conversion_entries = conversions_rows.all()

    subscription_payments_rows = await db.execute(
        select(
            Transaction.user_id,
            Transaction.amount_kopeks,
            Transaction.created_at,
        )
        .where(
            Transaction.user_id.in_(select(registrations_subquery.c.user_id)),
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed.is_(True),
        )
        .order_by(Transaction.user_id, Transaction.created_at)
    )
    subscription_payments = subscription_payments_rows.all()

    subscription_payments_total = 0
    paid_users_from_transactions = set()
    conversion_user_ids = set()
    first_payment_amount_by_user: dict[int, int] = {}
    first_payment_time_by_user: dict[int, datetime | None] = {}

    for user_id, amount_kopeks, converted_at in conversion_entries:
        conversion_user_ids.add(user_id)
        amount_value = int(amount_kopeks or 0)
        first_payment_amount_by_user[user_id] = amount_value
        first_payment_time_by_user[user_id] = converted_at

    for user_id, amount_kopeks, created_at in subscription_payments:
        amount_value = abs(int(amount_kopeks or 0))
        subscription_payments_total += amount_value
        paid_users_from_transactions.add(user_id)

        if user_id not in first_payment_amount_by_user:
            first_payment_amount_by_user[user_id] = amount_value
            first_payment_time_by_user[user_id] = created_at
        else:
            existing_time = first_payment_time_by_user.get(user_id)
            if (existing_time is None and created_at is not None) or (
                existing_time is not None and created_at is not None and created_at < existing_time
            ):
                first_payment_amount_by_user[user_id] = amount_value
                first_payment_time_by_user[user_id] = created_at

    # Revenue = only real deposits (exclude bonus-funded subscription spending)
    total_revenue = deposits_total

    paid_user_ids = set(paid_users_from_transactions)
    paid_user_ids.update(conversion_user_ids)
    paid_users_count = max(len(paid_user_ids), paid_users_from_flag)

    conversion_count = conversion_count or len(paid_user_ids)
    conversion_count = max(conversion_count, len(paid_user_ids))

    avg_first_payment = 0
    if first_payment_amount_by_user:
        avg_first_payment = int(sum(first_payment_amount_by_user.values()) / len(first_payment_amount_by_user))

    conversion_rate = 0.0
    if count:
        conversion_rate = round((paid_users_count / count) * 100, 1)

    trial_conversion_rate = 0.0
    if trial_users_count:
        trial_conversion_rate = round((conversion_count / trial_users_count) * 100, 1)

    avg_revenue_per_user = 0
    if count:
        avg_revenue_per_user = int(total_revenue / count)

    return {
        'registrations': count,
        'balance_issued': total_balance,
        'subscription_issued': subscription_bonuses_issued,
        'last_registration': last_registration,
        'total_revenue_kopeks': total_revenue,
        'trial_users_count': trial_users_count,
        'active_trials_count': active_trials_count,
        'conversion_count': conversion_count,
        'paid_users_count': paid_users_count,
        'conversion_rate': conversion_rate,
        'trial_conversion_rate': trial_conversion_rate,
        'avg_revenue_per_user_kopeks': avg_revenue_per_user,
        'avg_first_payment_kopeks': avg_first_payment,
    }


async def get_campaigns_overview(db: AsyncSession) -> dict[str, int]:
    total = await get_campaigns_count(db)
    active = await get_campaigns_count(db, is_active=True)
    inactive = await get_campaigns_count(db, is_active=False)

    registrations_result = await db.execute(select(func.count(AdvertisingCampaignRegistration.id)))

    balance_result = await db.execute(
        select(func.coalesce(func.sum(AdvertisingCampaignRegistration.balance_bonus_kopeks), 0))
    )

    subscription_result = await db.execute(
        select(func.count(AdvertisingCampaignRegistration.id)).where(
            AdvertisingCampaignRegistration.bonus_type == 'subscription'
        )
    )

    return {
        'total': total,
        'active': active,
        'inactive': inactive,
        'registrations': registrations_result.scalar() or 0,
        'balance_total': balance_result.scalar() or 0,
        'subscription_total': subscription_result.scalar() or 0,
    }
