from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import Date, Integer, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AchievementTemplate,
    PollResponse,
    PromoCodeUse,
    Subscription,
    SubscriptionConversion,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    Transaction,
    TransactionType,
    User,
    UserAchievement,
    UserReview,
)


logger = structlog.get_logger(__name__)


# ---- Template CRUD (admin) ----

async def get_active_templates(db: AsyncSession) -> list[AchievementTemplate]:
    result = await db.execute(
        select(AchievementTemplate)
        .where(AchievementTemplate.is_active.is_(True))
        .order_by(AchievementTemplate.display_order, AchievementTemplate.id)
    )
    return list(result.scalars().all())


async def get_all_templates(db: AsyncSession) -> list[AchievementTemplate]:
    result = await db.execute(
        select(AchievementTemplate).order_by(AchievementTemplate.display_order, AchievementTemplate.id)
    )
    return list(result.scalars().all())


async def get_template_by_id(db: AsyncSession, template_id: int) -> AchievementTemplate | None:
    result = await db.execute(
        select(AchievementTemplate).where(AchievementTemplate.id == template_id)
    )
    return result.scalar_one_or_none()


async def create_template(
    db: AsyncSession,
    name: str,
    emoji: str,
    condition_type: str,
    condition_value: int,
    reward_type: str,
    reward_value: int,
    reward_duration_days: int | None = None,
    description: str | None = None,
    is_active: bool = True,
    display_order: int = 0,
    group_name: str | None = None,
    level: int = 1,
) -> AchievementTemplate:
    template = AchievementTemplate(
        name=name,
        description=description,
        emoji=emoji,
        condition_type=condition_type,
        condition_value=condition_value,
        reward_type=reward_type,
        reward_value=reward_value,
        reward_duration_days=reward_duration_days,
        is_active=is_active,
        display_order=display_order,
        group_name=group_name,
        level=level,
    )
    db.add(template)
    await db.flush()
    return template


async def update_template(
    db: AsyncSession,
    template_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    emoji: str | None = None,
    condition_type: str | None = None,
    condition_value: int | None = None,
    reward_type: str | None = None,
    reward_value: int | None = None,
    reward_duration_days: int | None = None,
    is_active: bool | None = None,
    display_order: int | None = None,
    _description_set: bool = False,
    _reward_duration_set: bool = False,
) -> AchievementTemplate | None:
    template = await get_template_by_id(db, template_id)
    if not template:
        return None
    if name is not None:
        template.name = name
    if _description_set:
        template.description = description
    if emoji is not None:
        template.emoji = emoji
    if condition_type is not None:
        template.condition_type = condition_type
    if condition_value is not None:
        template.condition_value = condition_value
    if reward_type is not None:
        template.reward_type = reward_type
    if reward_value is not None:
        template.reward_value = reward_value
    if _reward_duration_set:
        template.reward_duration_days = reward_duration_days
    if is_active is not None:
        template.is_active = is_active
    if display_order is not None:
        template.display_order = display_order
    await db.flush()
    return template


async def delete_template(db: AsyncSession, template_id: int) -> bool:
    result = await db.execute(
        select(AchievementTemplate).where(AchievementTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        return False
    await db.delete(template)
    await db.flush()
    return True


async def count_unlocks_per_template(db: AsyncSession) -> dict[int, int]:
    """Return a dict mapping template_id -> count of UserAchievement rows."""
    result = await db.execute(
        select(UserAchievement.template_id, func.count(UserAchievement.id))
        .group_by(UserAchievement.template_id)
    )
    return {int(tid): int(cnt) for tid, cnt in result.all()}


async def count_total_unlocks(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(UserAchievement.id)))
    return int(result.scalar() or 0)


async def count_unique_users_with_achievements(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(func.distinct(UserAchievement.user_id)))
    )
    return int(result.scalar() or 0)


# ---- User achievement CRUD ----

async def get_user_achievements(db: AsyncSession, user_id: int) -> list[UserAchievement]:
    result = await db.execute(
        select(UserAchievement)
        .where(UserAchievement.user_id == user_id)
        .options(selectinload(UserAchievement.template))
        .order_by(UserAchievement.unlocked_at)
    )
    return list(result.scalars().all())


async def get_achievement_sweep_user_ids(db: AsyncSession, active_days: int) -> list[int]:
    """User ids to evaluate in the background achievement sweep.

    Union of:
      - users with an active/trial subscription
      - active users updated within ``active_days`` days
    Both restricted to telegram_id IS NOT NULL. Returns a deduped list.

    Uses id-only selects (no SELECT DISTINCT on User rows — the users table has a
    json column with no equality operator, which breaks DISTINCT on whole rows).
    """
    cutoff = datetime.now(UTC) - timedelta(days=active_days)

    sub_ids_result = await db.execute(
        select(Subscription.user_id)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            User.telegram_id.isnot(None),
        )
    )
    recent_ids_result = await db.execute(
        select(User.id).where(
            User.status == 'active',
            User.telegram_id.isnot(None),
            User.updated_at >= cutoff,
        )
    )

    ids: set[int] = set(sub_ids_result.scalars().all())
    ids.update(recent_ids_result.scalars().all())
    return list(ids)


async def unlock_achievement(db: AsyncSession, user_id: int, template_id: int) -> UserAchievement:
    ua = UserAchievement(
        user_id=user_id,
        template_id=template_id,
        reward_claimed=True,
    )
    db.add(ua)
    await db.flush()
    return ua


async def _get_user_stat(db: AsyncSession, user: User, condition_type: str) -> int:
    """Get the current stat value for a user based on condition_type."""
    if condition_type == 'total_spent_kopeks':
        result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                and_(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                    Transaction.is_refunded.is_(False),
                )
            )
        )
        return abs(int(result.scalar() or 0))

    elif condition_type == 'registered':
        # 1 if user account exists. Pure registration achievement — no
        # deposit gate, no time delay. Use sparingly; pairs well with
        # tiny non-cash rewards.
        return 1 if user.created_at else 0

    elif condition_type == 'days_active':
        # Anti-farm: gate by first completed deposit. Idle never-paying account
        # would otherwise unlock "Старожил" after 365 days for free.
        if not user.created_at:
            return 0
        paid_check = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                )
            )
        )
        if (paid_check.scalar() or 0) == 0:
            return 0
        delta = datetime.now(UTC) - user.created_at
        return max(0, delta.days)

    elif condition_type == 'referral_count':
        # Anti-farm: count only referrals who actually paid (have at least one
        # completed DEPOSIT). 25 fake unfunded accounts no longer unlock the
        # Ambassador chain.
        paid_refs_subq = (
            select(Transaction.user_id)
            .where(
                and_(
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                )
            )
            .distinct()
            .subquery()
        )
        result = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.referred_by_id == user.id,
                    User.id.in_(select(paid_refs_subq.c.user_id)),
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'traffic_gb':
        result = await db.execute(
            select(Subscription).where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.status.in_([
                        SubscriptionStatus.ACTIVE.value,
                        SubscriptionStatus.TRIAL.value,
                    ]),
                )
            )
        )
        subs = list(result.scalars().all())
        total_used = sum(s.traffic_used_gb or 0 for s in subs)
        return int(total_used)

    elif condition_type == 'topup_count':
        # Anti-abuse: only count deposits >= ACHIEVEMENT_MIN_TOPUP_KOPEKS.
        # Without filter, user could spam 1₽ topups to farm "N deposits"
        # milestones (each milestone triggers reward payout).
        from app.config import settings as _settings

        min_kopeks = _settings.ACHIEVEMENT_MIN_TOPUP_KOPEKS
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                    Transaction.amount_kopeks >= min_kopeks,
                    Transaction.is_refunded.is_(False),
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'review_left':
        # Only count approved reviews. Otherwise spam/troll review (later
        # rejected by admin) would still pay out — UserAchievement row stays
        # forever once granted.
        result = await db.execute(
            select(func.count(UserReview.id)).where(
                and_(
                    UserReview.user_id == user.id,
                    UserReview.is_approved.is_(True),
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'first_paid_subscription':
        # 1 if user has any non-trial subscription, else 0. Triggered by first
        # paid purchase (single-tier badge / starter reward).
        result = await db.execute(
            select(func.count(Subscription.id)).where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.is_trial.is_(False),
                )
            )
        )
        return 1 if (result.scalar() or 0) > 0 else 0

    elif condition_type == 'autopay_enabled':
        # 1 if any subscription has autopay on. Encourages retention setup.
        result = await db.execute(
            select(func.count(Subscription.id)).where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.autopay_enabled.is_(True),
                )
            )
        )
        return 1 if (result.scalar() or 0) > 0 else 0

    elif condition_type == 'single_topup_max_kopeks':
        # Max amount of any single completed deposit. Reward big single payments.
        result = await db.execute(
            select(func.coalesce(func.max(Transaction.amount_kopeks), 0)).where(
                and_(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                    Transaction.is_refunded.is_(False),
                )
            )
        )
        return abs(int(result.scalar() or 0))

    elif condition_type == 'promocode_used_count':
        result = await db.execute(
            select(func.count(PromoCodeUse.id)).where(PromoCodeUse.user_id == user.id)
        )
        return int(result.scalar() or 0)

    elif condition_type == 'poll_completed_count':
        # Only fully completed polls (completed_at not null). Skips abandoned.
        result = await db.execute(
            select(func.count(PollResponse.id)).where(
                and_(
                    PollResponse.user_id == user.id,
                    PollResponse.completed_at.isnot(None),
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'referral_revenue_kopeks':
        # Sum of completed deposits across all referrals invited by this user.
        # Anti-farm: real revenue, not just signup count. The Ambassador chain
        # already counts paid refs; this measures actual generated income.
        ref_user_ids = (
            select(User.id).where(User.referred_by_id == user.id).subquery()
        )
        result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                and_(
                    Transaction.user_id.in_(select(ref_user_ids)),
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                    Transaction.is_refunded.is_(False),
                )
            )
        )
        return abs(int(result.scalar() or 0))

    elif condition_type == 'tickets_resolved_count':
        # User-side metric: how many of their own tickets reached CLOSED.
        result = await db.execute(
            select(func.count(Ticket.id)).where(
                and_(
                    Ticket.user_id == user.id,
                    Ticket.status == TicketStatus.CLOSED.value,
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'subscription_period_days':
        # Longest paid period the user committed to. Rewards long-term planners
        # (180d / 360d buys).
        #
        # Two sources, take the max:
        #
        # 1. SubscriptionConversion.first_paid_period_days — populated only on
        #    trial → paid conversions (subscription_purchase_service.py:1083).
        # 2. (end_date - start_date) on any non-trial subscription — covers
        #    direct paid purchases that skip the trial step (no conversion row
        #    is created in that path). Renewals grow end_date but keep
        #    start_date, so this metric reflects accumulated commitment, which
        #    matches the achievement's "long-term planner" intent.
        conv_result = await db.execute(
            select(func.coalesce(func.max(SubscriptionConversion.first_paid_period_days), 0)).where(
                SubscriptionConversion.user_id == user.id
            )
        )
        from_conversion = int(conv_result.scalar() or 0)

        # Date subtraction returns Integer days directly. EXTRACT('epoch', ...)
        # / 86400 overflowed int4 for fixed-far-future end_dates (e.g. 2100-01-01
        # placeholders for "lifetime" subscriptions): 74y * 31.5M sec > 2.1B.
        span_expr = func.cast(Subscription.end_date, Date) - func.cast(
            Subscription.start_date, Date
        )
        span_result = await db.execute(
            select(func.coalesce(func.max(span_expr), 0)).where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.is_trial.is_(False),
                )
            )
        )
        from_span = int(span_result.scalar() or 0)

        return max(from_conversion, from_span)

    return 0


async def _compute_topup_window_payout(
    db: AsyncSession,
    user: User,
    template: AchievementTemplate,
    min_topup_kopeks: int,
) -> int:
    """Variant D: reward = (reward_value %) × sum(deposits ≥ min) in window
    since previous level unlock (or user.created_at for level 1 / standalone)."""
    window_start = user.created_at
    group = getattr(template, 'group_name', None)
    level = getattr(template, 'level', 1)
    if group and level > 1:
        prev_result = await db.execute(
            select(UserAchievement.unlocked_at)
            .join(AchievementTemplate, AchievementTemplate.id == UserAchievement.template_id)
            .where(
                and_(
                    UserAchievement.user_id == user.id,
                    AchievementTemplate.group_name == group,
                    AchievementTemplate.level == level - 1,
                )
            )
            .order_by(UserAchievement.unlocked_at.desc())
            .limit(1)
        )
        prev_time = prev_result.scalar_one_or_none()
        if prev_time:
            window_start = prev_time

    sum_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.user_id == user.id,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed.is_(True),
                Transaction.amount_kopeks >= min_topup_kopeks,
                Transaction.created_at >= window_start,
                Transaction.is_refunded.is_(False),
            )
        )
    )
    sum_kopeks = int(sum_result.scalar() or 0)
    percent = max(0, template.reward_value or 0)
    return int(sum_kopeks * percent / 100)


async def check_and_unlock_all(
    db: AsyncSession,
    user_id: int,
    bot=None,
) -> list[AchievementTemplate]:
    from app.config import settings

    if not settings.ACHIEVEMENTS_ENABLED:
        return []
    """Check all active templates against user's stats. Unlock and claim rewards for new achievements.
    Returns list of newly unlocked templates (for notification)."""
    from app.database.crud.transaction import create_transaction
    from app.database.models import PaymentMethod

    # Lock the user row up-front: any reward path mutates user.balance_kopeks
    # without going through the locked add_user_balance wrapper, so concurrent
    # invocations (e.g. webhook + cabinet page load) could double-credit.
    # The unique constraint on (user_id, template_id) caps the blast radius
    # but the in-flight balance increment is still a race window.
    from app.database.crud.user import lock_user_for_update

    user_result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        return []

    user = await lock_user_for_update(db, user)

    templates = await get_active_templates(db)

    # Get already unlocked template IDs
    existing_result = await db.execute(
        select(UserAchievement.template_id).where(UserAchievement.user_id == user_id)
    )
    unlocked_ids = set(existing_result.scalars().all())

    # Build group → unlocked-levels map for multi-level check
    _group_unlocked: dict[str, set[int]] = {}
    for t in templates:
        group = getattr(t, 'group_name', None)
        if group and t.id in unlocked_ids:
            _group_unlocked.setdefault(group, set()).add(getattr(t, 'level', 1))

    newly_unlocked: list[AchievementTemplate] = []

    for template in templates:
        if template.id in unlocked_ids:
            continue

        # Multi-level gate: require previous level unlocked
        group = getattr(template, 'group_name', None)
        level = getattr(template, 'level', 1)
        if group and level > 1:
            prev_levels = _group_unlocked.get(group, set())
            if (level - 1) not in prev_levels:
                continue  # Previous level not unlocked yet

        current_value = await _get_user_stat(db, user, template.condition_type)
        if current_value < template.condition_value:
            continue

        # For rewards requiring subscription (traffic/days), defer if no active sub
        if template.reward_type in ('traffic_gb', 'wl_traffic_gb', 'subscription_days') and template.reward_value > 0:
            _sub_check = await db.execute(
                select(Subscription).where(
                    and_(
                        Subscription.user_id == user_id,
                        Subscription.status.in_([
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                        ]),
                    )
                ).limit(1)
            )
            if _sub_check.scalar_one_or_none() is None:
                # No active subscription — skip, will unlock when user has one
                continue

        # Unlock
        await unlock_achievement(db, user_id, template.id)

        # Per-template dynamic payout (variant D). 0 means no dynamic reward.
        dynamic_payout_kopeks = 0

        # Apply reward
        if template.reward_type == 'balance_kopeks' and template.reward_value > 0:
            user.balance_kopeks += template.reward_value
            await create_transaction(
                db,
                user_id=user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=template.reward_value,
                description=f'\U0001f3c6 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: {template.name}',
                commit=False,
            )
        elif template.reward_type == 'topup_window_sum_percent' and template.reward_value > 0:
            # Variant D: % of sum of qualifying deposits since prev level unlock
            payout = await _compute_topup_window_payout(
                db, user, template, settings.ACHIEVEMENT_MIN_TOPUP_KOPEKS
            )
            if payout > 0:
                user.balance_kopeks += payout
                await create_transaction(
                    db,
                    user_id=user_id,
                    type=TransactionType.DEPOSIT,
                    amount_kopeks=payout,
                    description=(
                        f'\U0001f3c6 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: {template.name} '
                        f'({template.reward_value}% \u043e\u0442 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439)'
                    ),
                    commit=False,
                )
                dynamic_payout_kopeks = payout
        elif template.reward_type == 'subscription_days' and template.reward_value > 0:
            sub_result = await db.execute(
                select(Subscription).where(
                    and_(
                        Subscription.user_id == user_id,
                        Subscription.status.in_([
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                        ]),
                    )
                ).order_by(Subscription.created_at.desc()).limit(1)
            )
            sub = sub_result.scalar_one_or_none()
            if sub and sub.end_date:
                sub.end_date = sub.end_date + timedelta(days=template.reward_value)
        elif template.reward_type == 'traffic_gb' and template.reward_value > 0:
            sub_result = await db.execute(
                select(Subscription).where(
                    and_(
                        Subscription.user_id == user_id,
                        Subscription.status.in_([
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                        ]),
                    )
                ).order_by(Subscription.created_at.desc()).limit(1)
            )
            sub = sub_result.scalar_one_or_none()
            if sub:
                sub.traffic_limit_gb = (sub.traffic_limit_gb or 0) + template.reward_value
        elif template.reward_type == 'wl_traffic_gb' and template.reward_value > 0:
            sub_result = await db.execute(
                select(Subscription).where(
                    and_(
                        Subscription.user_id == user_id,
                        Subscription.status.in_([
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                        ]),
                    )
                ).order_by(Subscription.created_at.desc()).limit(1)
            )
            sub = sub_result.scalar_one_or_none()
            if sub:
                sub.wl_traffic_limit_gb = (sub.wl_traffic_limit_gb or 0) + template.reward_value

        newly_unlocked.append(template)

        # Notify user if bot provided
        if bot and user.telegram_id:
            try:
                reward_text = ''
                if template.reward_type == 'balance_kopeks' and template.reward_value > 0:
                    reward_text = f'\n\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: {template.reward_value / 100:.0f} \u20bd \u043d\u0430 \u0431\u0430\u043b\u0430\u043d\u0441'
                elif template.reward_type == 'subscription_days' and template.reward_value > 0:
                    reward_text = f'\n\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: +{template.reward_value} \u0434\u043d\u0435\u0439 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438'
                elif template.reward_type == 'traffic_gb' and template.reward_value > 0:
                    reward_text = f'\n\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: +{template.reward_value} \u0413\u0411 \u0442\u0440\u0430\u0444\u0438\u043a\u0430'
                elif template.reward_type == 'wl_traffic_gb' and template.reward_value > 0:
                    reward_text = f'\n\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: +{template.reward_value} \u0413\u0411 WL-\u0442\u0440\u0430\u0444\u0438\u043a\u0430'
                elif template.reward_type == 'topup_window_sum_percent' and dynamic_payout_kopeks > 0:
                    reward_text = (
                        f'\n\U0001f381 \u041d\u0430\u0433\u0440\u0430\u0434\u0430: {dynamic_payout_kopeks / 100:.0f} \u20bd '
                        f'\u043d\u0430 \u0431\u0430\u043b\u0430\u043d\u0441 ({template.reward_value}% \u043e\u0442 \u043f\u043e\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0439)'
                    )

                desc = template.description or ''
                desc_line = f'\n{desc}\n' if desc else '\n'

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f'\U0001f389 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 \u0440\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u043e!</b>\n\n'
                        f'{template.emoji} <b>{template.name}</b>'
                        f'{desc_line}'
                        f'{reward_text}\n\n'
                        f'\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \U0001f3c6 \u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f \u0432 \u043c\u0435\u043d\u044e, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c.'
                    ),
                    parse_mode='HTML',
                    # Low-urgency badge unlock \u2014 send silently so the background
                    # sweep can't produce a burst of buzzing notifications.
                    disable_notification=True,
                )
            except Exception as e:
                logger.warning('Failed to notify user about achievement', user_id=user.id, error=str(e))

            # Notify admin chat
            try:
                from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

                admin_service = AdminNotificationService(bot)
                user_link = f'@{user.username}' if user.username else f'#{user.telegram_id}'
                await admin_service.send_admin_notification(
                    text=(
                        f'\U0001f3c6 <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435</b>\n\n'
                        f'\U0001f464 \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c: {user_link}\n'
                        f'{template.emoji} {template.name}\n'
                        f'{reward_text if reward_text else "Без награды"}'
                    ),
                    category=NotificationCategory.ACHIEVEMENTS,
                )
            except Exception as admin_err:
                logger.warning('Failed to send admin achievement notification', error=str(admin_err))

    if newly_unlocked:
        await db.commit()

    return newly_unlocked
