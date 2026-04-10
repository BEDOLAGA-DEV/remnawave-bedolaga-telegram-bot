from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AchievementTemplate,
    Subscription,
    SubscriptionStatus,
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
                )
            )
        )
        return abs(int(result.scalar() or 0))

    elif condition_type == 'days_active':
        if user.created_at:
            delta = datetime.now(UTC) - user.created_at
            return max(0, delta.days)
        return 0

    elif condition_type == 'referral_count':
        result = await db.execute(
            select(func.count(User.id)).where(User.referred_by_id == user.id)
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
        result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.user_id == user.id,
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed.is_(True),
                )
            )
        )
        return int(result.scalar() or 0)

    elif condition_type == 'review_left':
        result = await db.execute(
            select(func.count(UserReview.id)).where(UserReview.user_id == user.id)
        )
        return int(result.scalar() or 0)

    return 0


async def check_and_unlock_all(
    db: AsyncSession,
    user_id: int,
    bot=None,
) -> list[AchievementTemplate]:
    """Check all active templates against user's stats. Unlock and claim rewards for new achievements.
    Returns list of newly unlocked templates (for notification)."""
    from app.database.crud.transaction import create_transaction
    from app.database.models import PaymentMethod

    user_result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        return []

    templates = await get_active_templates(db)

    # Get already unlocked template IDs
    existing_result = await db.execute(
        select(UserAchievement.template_id).where(UserAchievement.user_id == user_id)
    )
    unlocked_ids = set(existing_result.scalars().all())

    newly_unlocked: list[AchievementTemplate] = []

    for template in templates:
        if template.id in unlocked_ids:
            continue

        current_value = await _get_user_stat(db, user, template.condition_type)
        if current_value < template.condition_value:
            continue

        # Unlock
        await unlock_achievement(db, user_id, template.id)

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

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f'{template.emoji} <b>\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 \u0440\u0430\u0437\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u043e!</b>\n\n'
                        f'{template.emoji} <b>{template.name}</b>'
                        f'{reward_text}'
                    ),
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.warning('Failed to notify user about achievement', user_id=user.id, error=str(e))

    if newly_unlocked:
        await db.commit()

    return newly_unlocked
