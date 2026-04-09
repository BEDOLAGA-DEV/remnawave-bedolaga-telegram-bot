from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    Transaction,
    User,
    WeeklyDigestRecord,
)


logger = structlog.get_logger(__name__)


class WeeklyDigestService:
    def __init__(self, bot: Bot, session_factory: async_sessionmaker[AsyncSession]):
        self.bot = bot
        self.session_factory = session_factory

    async def run_once(self) -> None:
        if not settings.WEEKLY_DIGEST_ENABLED:
            return

        now = datetime.now(UTC)
        if now.weekday() != settings.WEEKLY_DIGEST_DAY:
            return

        week_year = now.strftime('%G-W%V')
        logger.info('Weekly digest check started', week_year=week_year)

        async with self.session_factory() as db:
            # Get users with active subscription, digest enabled, and telegram_id
            stmt = (
                select(User)
                .join(Subscription, Subscription.user_id == User.id)
                .where(
                    and_(
                        User.digest_enabled.is_(True),
                        User.telegram_id.isnot(None),
                        Subscription.status.in_([
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                        ]),
                    )
                )
                .options(selectinload(User.subscriptions))
                .distinct()
            )
            result = await db.execute(stmt)
            users = list(result.scalars().all())

            sent_count = 0
            for user in users:
                try:
                    # Check if already sent for this week
                    existing = await db.execute(
                        select(WeeklyDigestRecord).where(
                            and_(
                                WeeklyDigestRecord.user_id == user.id,
                                WeeklyDigestRecord.week_year == week_year,
                            )
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue

                    sub = user.subscription
                    if not sub:
                        continue

                    # Calculate days remaining
                    days_remaining = 0
                    if sub.end_date:
                        delta = sub.end_date - now
                        days_remaining = max(0, delta.days)

                    # Traffic info
                    used_gb = round(sub.traffic_used_gb or 0, 1)
                    limit_gb = sub.traffic_limit_gb or 0

                    # Balance
                    balance = round(user.balance_kopeks / 100, 2)

                    # Referral count
                    ref_result = await db.execute(
                        select(func.count(User.id)).where(User.referred_by_id == user.id)
                    )
                    ref_count = ref_result.scalar() or 0

                    # Personalized tip
                    if limit_gb > 0 and used_gb / limit_gb > 0.8:
                        tip = '\u26a0\ufe0f \u0422\u0440\u0430\u0444\u0438\u043a \u0437\u0430\u043a\u0430\u043d\u0447\u0438\u0432\u0430\u0435\u0442\u0441\u044f! \u0414\u043e\u043a\u0443\u043f\u0438\u0442\u0435 \u0432 \u043c\u0435\u043d\u044e.'
                    elif days_remaining <= 7:
                        tip = '\u23f0 \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u0441\u043a\u043e\u0440\u043e \u0437\u0430\u043a\u043e\u043d\u0447\u0438\u0442\u0441\u044f! \u041f\u0440\u043e\u0434\u043b\u0438\u0442\u0435 \u0441\u0435\u0439\u0447\u0430\u0441.'
                    else:
                        tip = '\u2705 \u0412\u0441\u0451 \u0432 \u043f\u043e\u0440\u044f\u0434\u043a\u0435! \u041f\u0440\u0438\u044f\u0442\u043d\u043e\u0433\u043e \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u044f.'

                    limit_display = f'{limit_gb}' if limit_gb > 0 else '\u221e'

                    message_text = (
                        '\U0001f4ca <b>\u0415\u0436\u0435\u043d\u0435\u0434\u0435\u043b\u044c\u043d\u0430\u044f \u0441\u0432\u043e\u0434\u043a\u0430</b>\n\n'
                        f'\U0001f4c5 \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430: \u043e\u0441\u0442\u0430\u043b\u043e\u0441\u044c {days_remaining} \u0434\u043d\u0435\u0439\n'
                        f'\U0001f4c8 \u0422\u0440\u0430\u0444\u0438\u043a: {used_gb} / {limit_display} \u0413\u0411\n'
                        f'\U0001f4b0 \u0411\u0430\u043b\u0430\u043d\u0441: {balance} \u20bd\n'
                        f'\U0001f465 \u0420\u0435\u0444\u0435\u0440\u0430\u043b\u043e\u0432: {ref_count}\n\n'
                        f'{tip}'
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text='\U0001f515 \u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c',
                                    callback_data='nz!_digest_off',
                                )
                            ]
                        ]
                    )

                    await self.bot.send_message(
                        chat_id=user.telegram_id,
                        text=message_text,
                        parse_mode='HTML',
                        reply_markup=keyboard,
                    )

                    # Record sent digest
                    record = WeeklyDigestRecord(
                        user_id=user.id,
                        week_year=week_year,
                    )
                    db.add(record)
                    await db.commit()
                    sent_count += 1

                except Exception as e:
                    logger.warning('Failed to send digest to user', user_id=user.id, error=str(e))
                    continue

            logger.info('Weekly digest completed', sent_count=sent_count)

    async def start(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error('Weekly digest service error', error=str(e))
            await asyncio.sleep(24 * 3600)  # check daily
