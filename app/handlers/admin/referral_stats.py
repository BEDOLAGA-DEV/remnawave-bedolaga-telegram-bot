from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Transaction, TransactionType, User
from app.localization.texts import get_texts
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_username

logger = structlog.get_logger(__name__)


@admin_required
@error_handler
async def show_referral_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)

    # Top 10 referrers by count of referred users
    top_referrers_query = (
        select(
            User.referred_by_id,
            func.count(User.id).label('referral_count'),
        )
        .where(User.referred_by_id.isnot(None))
        .group_by(User.referred_by_id)
        .order_by(func.count(User.id).desc())
        .limit(10)
    )
    top_referrers_result = await db.execute(top_referrers_query)
    top_referrers = top_referrers_result.all()

    if not top_referrers:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
            ]
        )
        await callback.message.edit_text(
            '<b>👥 Реферальная статистика</b>\n\nНет данных о рефералах.',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    # Fetch referrer user objects
    referrer_ids = [r[0] for r in top_referrers]
    users_query = select(User).where(User.id.in_(referrer_ids))
    users_result = await db.execute(users_query)
    users_map = {u.id: u for u in users_result.scalars().all()}

    # Fetch total referral reward amounts for each referrer
    rewards_query = (
        select(
            Transaction.user_id,
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_rewards'),
        )
        .where(
            Transaction.user_id.in_(referrer_ids),
            Transaction.type == TransactionType.REFERRAL_REWARD.value,
            Transaction.is_completed.is_(True),
        )
        .group_by(Transaction.user_id)
    )
    rewards_result = await db.execute(rewards_query)
    rewards_map = {row[0]: row[1] for row in rewards_result.all()}

    # Build message
    lines = [
        '<b>👥 Реферальная статистика</b>',
        '',
        '<b>🏆 Топ-10 рефереров:</b>',
        '',
    ]

    for i, (referrer_id, referral_count) in enumerate(top_referrers, 1):
        user = users_map.get(referrer_id)
        if user:
            username = format_username(user.username, user.telegram_id, user.full_name)
            user_display = html.escape(username)
            user_id_display = user.telegram_id or user.email or f'#{user.id}'
        else:
            user_display = f'ID {referrer_id}'
            user_id_display = referrer_id

        total_rewards = rewards_map.get(referrer_id, 0)
        reward_text = settings.format_price(total_rewards) if total_rewards > 0 else '0'

        lines.append(
            f'{i}. {user_display} (<code>{user_id_display}</code>)\n'
            f'   👥 Рефералов: <b>{referral_count}</b> | 💰 Награды: <b>{reward_text}</b>'
        )

    # Total referral stats
    total_referrals_query = select(func.count(User.id)).where(User.referred_by_id.isnot(None))
    total_referrals_result = await db.execute(total_referrals_query)
    total_referrals = total_referrals_result.scalar() or 0

    total_rewards_query = select(
        func.coalesce(func.sum(Transaction.amount_kopeks), 0),
    ).where(
        Transaction.type == TransactionType.REFERRAL_REWARD.value,
        Transaction.is_completed.is_(True),
    )
    total_rewards_result = await db.execute(total_rewards_query)
    total_rewards_all = total_rewards_result.scalar() or 0

    lines.append('')
    lines.append(f'📊 Всего рефералов: <b>{total_referrals}</b>')
    lines.append(f'💵 Всего реф. наград: <b>{settings.format_price(total_rewards_all)}</b>')

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BACK, callback_data='admin_panel')],
        ]
    )

    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_referral_stats, F.data == 'admin_referral_stats')
