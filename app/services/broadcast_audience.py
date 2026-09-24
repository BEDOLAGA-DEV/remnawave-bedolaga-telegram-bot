"""Selection shared by broadcast preview and delivery."""

from datetime import UTC, date, datetime, time, timedelta
from math import isfinite

from sqlalchemy import and_, case, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from app.cabinet.schemas.broadcasts import BroadcastAudience
from app.database.constants import POSTGRES_INT4_MAX
from app.database.models import Subscription, SubscriptionStatus, Tariff, User, UserStatus
from app.utils.notification_prefs import filter_users_by_broadcast_category
from app.utils.timezone import get_local_timezone, local_day_start


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

# New rows are intentionally separate predicates. With several subscriptions,
# "active AND trial AND tariff X" may be satisfied by different subscriptions.
COMMON_ATOMIC_FIELDS: dict[str, set[str]] = {
    'subscription_status': {'active', 'expired'},
    'subscription_type': {'trial', 'paid'},
    'traffic_zero': {'zero'},
    'traffic_gt': set(),  # Numeric value in GB.
    'traffic_lt': set(),
    'subscription_end_date': set(),
    'registration_date': set(),
    'activity_date': set(),
    'paid_history': {'yes', 'no'},
    'source': {'custom_referrals', 'custom_direct'},
    'registration': {'custom_today', 'custom_week', 'custom_month'},
    'activity': {'custom_active_today', 'custom_inactive_week', 'custom_inactive_month'},
    'subscription_end_preset': {'expiring'},
    'tariff': set(),
    'auth_type': {'email_only', 'telegram_with_email'},
}
TELEGRAM_FIELDS.update(COMMON_ATOMIC_FIELDS)
TELEGRAM_FIELDS.update({'telegram_id': set(), 'telegram_username': set()})
EMAIL_FIELDS.update(COMMON_ATOMIC_FIELDS)
EMAIL_FIELDS['email_user'] = set()
EMAIL_FIELDS['promo_group'] = set()

DATE_FIELDS = {'subscription_end_date', 'registration_date', 'activity_date'}
NUMBER_FIELDS = {'traffic_gt', 'traffic_lt'}
USER_FIELDS = {'telegram_id', 'telegram_username', 'email_user'}


def _has_subscription(*conditions: ColumnElement[bool]) -> ColumnElement[bool]:
    return select(Subscription.id).where(Subscription.user_id == User.id, *conditions).correlate(User).exists()


def _expiring_subscription(active: ColumnElement[bool], now: datetime) -> ColumnElement[bool]:
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


def _date_start(raw: str) -> datetime:
    return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=get_local_timezone()).astimezone(UTC)


def _date_predicate(condition) -> ColumnElement[bool]:
    column = {
        'registration_date': User.created_at,
        'activity_date': User.last_activity,
        'subscription_end_date': Subscription.end_date,
    }[condition.field]
    start = _date_start(condition.value)
    if condition.operator == 'before':
        predicate = column < start
    elif condition.operator == 'after':
        predicate = column >= _date_start((date.fromisoformat(condition.value) + timedelta(days=1)).isoformat())
    else:
        end = _date_start((date.fromisoformat(condition.value_to) + timedelta(days=1)).isoformat())
        predicate = and_(column >= start, column < end)
    return _has_subscription(predicate) if condition.field == 'subscription_end_date' else predicate


def _condition_predicate(condition, now: datetime) -> ColumnElement[bool]:
    field, value = condition.field, condition.value
    live = and_(
        Subscription.status.in_((SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)),
        Subscription.end_date > now,
    )
    if field in DATE_FIELDS:
        return _date_predicate(condition)
    if field in NUMBER_FIELDS:
        amount = float(value)
        traffic_used = func.coalesce(Subscription.traffic_used_gb, 0.0)
        comparison = traffic_used > amount if field == 'traffic_gt' else traffic_used < amount
        return _has_subscription(comparison)
    if field == 'traffic_zero':
        return _has_subscription(or_(Subscription.traffic_used_gb.is_(None), Subscription.traffic_used_gb <= 0))
    if field in USER_FIELDS:
        return User.id == int(value)
    if field == 'promo_group':
        return User.promo_group_id == int(value.removeprefix('promo_group_'))
    if field == 'subscription_status':
        if value == 'active':
            return _has_subscription(live)
        expired = _has_subscription(
            or_(
                Subscription.status.in_((SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value)),
                Subscription.end_date <= now,
            )
        )
        return and_(
            ~_has_subscription(live),
            or_(expired, and_(~_has_subscription(), User.has_had_paid_subscription.is_(True))),
        )
    if field == 'subscription_type':
        return _has_subscription(Subscription.is_trial.is_(value == 'trial'))
    if field == 'paid_history':
        return User.has_had_paid_subscription.is_(value == 'yes')
    if field == 'tariff':
        return _has_subscription(live, Subscription.tariff_id == int(value.removeprefix('tariff_')))
    if field == 'subscription_end_preset':
        return _expiring_subscription(live, now)
    return _target_predicate(value, now)


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
        return _expiring_subscription(active, now)
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


def validate_audience(
    audience: BroadcastAudience,
    channel: str,
    tariff_ids: set[int],
    promo_group_ids: set[int] | None = None,
) -> None:
    """Reject forged field/value combinations and unknown tariff or promo group IDs."""
    fields = TELEGRAM_FIELDS if channel == 'telegram' else EMAIL_FIELDS
    for condition in audience.conditions:
        if condition.field not in fields:
            raise ValueError('Invalid audience filter')
        if condition.field in DATE_FIELDS:
            if condition.operator not in ('before', 'after', 'between'):
                raise ValueError('Invalid date comparison')
            try:
                start = date.fromisoformat(condition.value)
                if start.isoformat() != condition.value:
                    raise ValueError
                if condition.operator == 'between':
                    end = date.fromisoformat(condition.value_to or '')
                    if end.isoformat() != condition.value_to or end < start or end == date.max:
                        raise ValueError
                elif condition.value_to is not None:
                    raise ValueError
                if condition.operator == 'after' and start == date.max:
                    raise ValueError
            except ValueError as exc:
                raise ValueError('Invalid audience date') from exc
            continue
        if condition.operator not in ('eq', 'ne') or condition.value_to is not None:
            raise ValueError('Invalid audience comparison')
        if condition.field in NUMBER_FIELDS:
            if condition.operator != 'eq':
                raise ValueError('Invalid traffic comparison')
            try:
                amount = float(condition.value)
            except ValueError as exc:
                raise ValueError('Invalid traffic amount') from exc
            if not isfinite(amount) or amount < 0 or amount > 1_000_000:
                raise ValueError('Invalid traffic amount')
            continue
        if condition.field == 'traffic_zero' and condition.operator != 'eq':
            raise ValueError('Invalid traffic comparison')
        if condition.field in USER_FIELDS:
            if (
                not condition.value.isascii()
                or not condition.value.isdigit()
                or not 0 < int(condition.value) <= POSTGRES_INT4_MAX
            ):
                raise ValueError('Invalid user filter')
            continue
        if condition.field == 'tariff':
            if not condition.value.startswith('tariff_'):
                raise ValueError('Invalid tariff filter')
            raw_id = condition.value.removeprefix('tariff_')
            if not raw_id.isdigit() or int(raw_id) not in tariff_ids:
                raise ValueError('Invalid tariff filter')
        elif condition.field == 'promo_group':
            if not condition.value.startswith('promo_group_'):
                raise ValueError('Invalid promo group filter')
            raw_id = condition.value.removeprefix('promo_group_')
            if not raw_id.isascii() or not raw_id.isdigit() or int(raw_id) not in (promo_group_ids or set()):
                raise ValueError('Invalid promo group filter')
        elif condition.value not in fields[condition.field]:
            raise ValueError('Invalid audience filter')


def audience_predicate(audience: BroadcastAudience, now: datetime | None = None) -> ColumnElement[bool]:
    """Combine rows strictly from top to bottom, including mixed AND/OR rows."""
    current_time = now or datetime.now(UTC)

    def matches(condition) -> ColumnElement[bool]:
        predicate = _condition_predicate(condition, current_time)
        if condition.operator == 'ne':
            # SQL NOT NULL is still NULL; an unset activity date must also
            # satisfy the opposite of an activity condition.
            return predicate.is_not(true())
        return predicate.is_(true())

    first, *rest = audience.conditions
    if not rest:
        return matches(first)

    # In a left-to-right fold, a true OR row sets the result to true, and a
    # false AND row sets it to false. The last such row wins. Checking these
    # rows in reverse order yields a flat CASE instead of a deeply nested SQL
    # expression, while retaining the same result for every row sequence.
    branches: list[tuple[ColumnElement[bool], ColumnElement[bool]]] = []
    for condition in reversed(rest):
        predicate = matches(condition)
        if condition.join == 'or':
            branches.append((predicate, true()))
        else:
            branches.append((predicate.is_not(true()), false()))
    return case(*branches, else_=matches(first))


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
