"""Selection shared by broadcast preview and delivery."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from app.cabinet.schemas.broadcasts import BroadcastAudience
from app.database.models import Subscription, SubscriptionStatus, Tariff, User, UserStatus
from app.utils.notification_prefs import filter_users_by_broadcast_category
from app.utils.timezone import local_day_start


TELEGRAM_FIELDS: dict[str, set[str]] = {
    'basic': {'all'},
    'subscription': {'active', 'trial', 'no', 'expiring', 'expired'},
    'traffic': {'zero', 'active_zero', 'trial_zero'},
    'registration': {'custom_today', 'custom_week', 'custom_month'},
    'activity': {'custom_active_today', 'custom_inactive_week', 'custom_inactive_month'},
    'source': {'custom_referrals', 'custom_direct'},
    'tariff': set(),  # Values are tariff_{id} and validated against the database.
}
EMAIL_FIELDS: dict[str, set[str]] = {
    'basic': {'all_email'},
    'auth_type': {'email_only', 'telegram_with_email'},
    'subscription': {'active_email', 'expired_email'},
}


def _has_subscription(*conditions: ColumnElement[bool]) -> ColumnElement[bool]:
    return (
        select(Subscription.id)
        .where(Subscription.user_id == User.id, *conditions)
        .correlate(User)
        .exists()
    )


def _target_predicate(value: str, now: datetime) -> ColumnElement[bool]:
    active = and_(Subscription.status == SubscriptionStatus.ACTIVE.value, Subscription.end_date > now)
    zero_traffic = or_(Subscription.traffic_used_gb.is_(None), Subscription.traffic_used_gb <= 0)

    if value in ('all', 'all_email'):
        return true()
    if value == 'active':
        return _has_subscription(active, Subscription.is_trial.is_(False))
    if value == 'trial':
        return _has_subscription(Subscription.is_trial.is_(True))
    if value == 'no':
        return ~_has_subscription(active)
    if value == 'expiring':
        daily_tariff = (
            select(Tariff.id)
            .where(Tariff.id == Subscription.tariff_id, Tariff.is_daily.is_(True))
            .correlate(Subscription)
            .exists()
        )
        return _has_subscription(
            active,
            Subscription.end_date <= now + timedelta(days=3),
            ~and_(daily_tariff, Subscription.is_daily_paused.is_(False)),
        )
    if value == 'expired':
        expired = _has_subscription(
            or_(
                Subscription.status.in_((SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value)),
                Subscription.end_date <= now,
            )
        )
        return and_(
            ~_has_subscription(active),
            or_(expired, and_(~_has_subscription(), User.has_had_paid_subscription.is_(True))),
        )
    if value == 'zero':
        return _has_subscription(active, zero_traffic)
    if value == 'active_zero':
        return _has_subscription(active, Subscription.is_trial.is_(False), zero_traffic)
    if value == 'trial_zero':
        return _has_subscription(active, Subscription.is_trial.is_(True), zero_traffic)
    if value == 'custom_today':
        return User.created_at >= local_day_start(now)
    if value == 'custom_week':
        return User.created_at >= now - timedelta(days=7)
    if value == 'custom_month':
        return User.created_at >= now - timedelta(days=30)
    if value == 'custom_active_today':
        return User.last_activity >= local_day_start(now)
    if value == 'custom_inactive_week':
        return User.last_activity < now - timedelta(days=7)
    if value == 'custom_inactive_month':
        return User.last_activity < now - timedelta(days=30)
    if value == 'custom_referrals':
        return User.referred_by_id.isnot(None)
    if value == 'custom_direct':
        return User.referred_by_id.is_(None)
    if value.startswith('tariff_'):
        return _has_subscription(active, Subscription.tariff_id == int(value.removeprefix('tariff_')))
    if value == 'email_only':
        return User.auth_type == 'email'
    if value == 'telegram_with_email':
        return and_(User.auth_type == 'telegram', User.telegram_id.isnot(None))
    if value == 'active_email':
        return _has_subscription(Subscription.status == SubscriptionStatus.ACTIVE.value)
    if value == 'expired_email':
        return _has_subscription(
            Subscription.status.in_((SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value))
        )
    return false()  # Unreachable after validate_audience.


def validate_audience(audience: BroadcastAudience, channel: str, tariff_ids: set[int]) -> None:
    """Reject forged field/value combinations and unknown tariffs."""
    fields = TELEGRAM_FIELDS if channel == 'telegram' else EMAIL_FIELDS
    for condition in audience.conditions:
        if condition.field == 'tariff' and channel == 'telegram':
            if not condition.value.startswith('tariff_'):
                raise ValueError('Invalid tariff filter')
            raw_id = condition.value.removeprefix('tariff_')
            if not raw_id.isdigit() or int(raw_id) not in tariff_ids:
                raise ValueError('Invalid tariff filter')
        elif condition.field not in fields or condition.value not in fields[condition.field]:
            raise ValueError('Invalid audience filter')


def audience_predicate(audience: BroadcastAudience, now: datetime | None = None) -> ColumnElement[bool]:
    """Combine rows strictly from top to bottom, including mixed AND/OR rows."""
    current_time = now or datetime.now(UTC)
    expression: ColumnElement[bool] | None = None
    for condition in audience.conditions:
        predicate = _target_predicate(condition.value, current_time)
        if condition.operator == 'ne':
            predicate = ~predicate
        if expression is None:
            expression = predicate
        elif condition.join == 'or':
            expression = or_(expression, predicate)
        else:
            expression = and_(expression, predicate)
    assert expression is not None  # BroadcastAudience requires at least one row.
    return expression


def audience_user_query(audience: BroadcastAudience, channel: str):
    """One user row per recipient, independent of subscription count."""
    base = [User.status == UserStatus.ACTIVE.value]
    if channel == 'telegram':
        base.append(User.telegram_id.isnot(None))
    elif channel == 'email':
        base.extend((User.email.isnot(None), User.email_verified.is_(True)))
    else:
        raise ValueError('Invalid broadcast channel')
    return (
        select(User)
        .options(
            load_only(
                User.id,
                User.telegram_id,
                User.email,
                User.username,
                User.first_name,
                User.last_name,
                User.language,
                User.notification_settings,
            )
        )
        .where(*base, audience_predicate(audience))
        .order_by(User.id)
    )


async def select_audience_users(
    db: AsyncSession, audience: BroadcastAudience, channel: str, category: str
) -> list[User]:
    """Return the same ordered, unique people for preview and delivery."""
    rows = await db.execute(audience_user_query(audience, channel))
    return filter_users_by_broadcast_category(list(rows.scalars().all()), category)


async def preview_audience_users(
    db: AsyncSession,
    audience: BroadcastAudience,
    channel: str,
    category: str,
    offset: int,
    limit: int,
) -> tuple[int, list[User]]:
    """Count exactly while keeping only one page of users in memory."""
    query = audience_user_query(audience, channel)
    if category == 'system':
        count_query = select(func.count()).select_from(query.with_only_columns(User.id).order_by(None).subquery())
        count = await db.scalar(count_query) or 0
        page = (await db.execute(query.offset(offset).limit(limit))).scalars().all()
        return count, list(page)

    count = 0
    page: list[User] = []
    last_id = 0
    while True:
        batch = (await db.execute(query.where(User.id > last_id).limit(500))).scalars().all()
        if not batch:
            break
        last_id = batch[-1].id
        for user in filter_users_by_broadcast_category(list(batch), category):
            if offset <= count < offset + limit:
                page.append(user)
            count += 1
    return count, page
