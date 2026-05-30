from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import Bot
from sqlalchemy import and_, extract, or_, select

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.crud.user import add_user_balance
from app.database.database import AsyncSessionLocal
from app.database.models import TransactionType, User, UserStatus
from app.services.birthday_settings_service import BirthdaySettingsService


logger = structlog.get_logger(__name__)

SYNC_STALE_DAYS = 30
_SENTINEL_YEAR = 1900


def should_sync_birthday(user: User) -> bool:
    """True if we have never synced this user's birthday, or the sync is stale."""
    synced = getattr(user, 'birthday_synced_at', None)
    if synced is None:
        return True
    return (datetime.now(UTC) - synced) >= timedelta(days=SYNC_STALE_DAYS)


class BirthdayService:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._running = False

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_enabled(self) -> bool:
        return bool(settings.BIRTHDAY_BONUS_ENABLED)

    async def sync_user_birthday(self, user_id: int, telegram_id: int) -> None:
        """Fire-and-forget: pull birthdate from Telegram profile, store it.

        Swallows all errors — never breaks the triggering interaction.
        """
        if self._bot is None or not telegram_id:
            return
        try:
            chat = await self._bot.get_chat(telegram_id)
        except Exception as exc:
            logger.debug('birthday.get_chat_failed', telegram_id=telegram_id, err=str(exc))
            return

        bd = getattr(chat, 'birthdate', None)
        now = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as db:
                user = await db.get(User, user_id)
                if user is None:
                    return
                if bd is None:
                    user.birthday_synced_at = now
                    await db.commit()
                    return
                try:
                    new_date = date(bd.year or _SENTINEL_YEAR, bd.month, bd.day)
                except (ValueError, TypeError) as exc:
                    logger.debug('birthday.bad_date', telegram_id=telegram_id, err=str(exc))
                    user.birthday_synced_at = now
                    await db.commit()
                    return
                if user.birth_date != new_date:
                    user.birth_date = new_date
                    user.birthday_changed_at = now
                user.birthday_synced_at = now
                await db.commit()
        except Exception as exc:
            logger.warning('birthday.sync_failed', user_id=user_id, err=str(exc))

    async def _select_birthday_users(self, db, today: date) -> list[User]:
        is_leap = (today.year % 4 == 0 and today.year % 100 != 0) or (today.year % 400 == 0)
        if today.month == 2 and today.day == 28 and not is_leap:
            result = await db.execute(
                select(User).where(
                    User.birth_date.isnot(None),
                    User.status == UserStatus.ACTIVE.value,
                    or_(
                        and_(extract('month', User.birth_date) == 2, extract('day', User.birth_date) == 28),
                        and_(extract('month', User.birth_date) == 2, extract('day', User.birth_date) == 29),
                    ),
                )
            )
            return list(result.scalars().all())
        result = await db.execute(
            select(User).where(
                and_(
                    User.birth_date.isnot(None),
                    extract('month', User.birth_date) == today.month,
                    extract('day', User.birth_date) == today.day,
                    User.status == UserStatus.ACTIVE.value,
                )
            )
        )
        return list(result.scalars().all())

    async def _grant_birthday_rewards(self, db) -> None:
        now = datetime.now(UTC)
        today = now.date()
        try:
            users = await self._select_birthday_users(db, today)
        except Exception as exc:
            logger.error('birthday.select_failed', err=str(exc))
            return

        min_age = BirthdaySettingsService.get_min_account_age_days()
        dob_stable = BirthdaySettingsService.get_dob_stable_days()
        granted = 0
        for user in users:
            try:
                if user.last_birthday_reward_year == today.year:
                    continue
                created = getattr(user, 'created_at', None)
                if created is not None and (now - created) < timedelta(days=min_age):
                    continue
                changed = getattr(user, 'birthday_changed_at', None)
                if changed is not None and (now - changed) < timedelta(days=dob_stable):
                    continue

                ok, rewarded = await self._apply_reward(db, user)
                if not ok:
                    # Грант не прошёл (например, add_user_balance вернул False) —
                    # откатываем частичное состояние и НЕ ставим year-метку,
                    # чтобы попробовать снова в следующий тик.
                    await db.rollback()
                    continue
                # Year-метка и сам грант коммитятся одной транзакцией —
                # crash между ними невозможен, повторная выдача исключена.
                user.last_birthday_reward_year = today.year
                await db.commit()
                await self._notify(user, rewarded)
                granted += 1
            except Exception as exc:
                logger.warning('birthday.grant_failed', user_id=getattr(user, 'id', None), err=str(exc))
                try:
                    await db.rollback()
                except Exception:
                    pass
        if granted:
            logger.info('birthday.granted', count=granted)

    async def _apply_reward(self, db, user) -> tuple[bool, str]:
        """Apply the configured reward WITHOUT committing.

        Returns (ok, description). The caller commits the reward together with
        the year-marker in a single transaction (atomicity → no double-grant).
        `ok=False` means nothing was granted (caller must rollback + skip the
        year-marker). The subscription_days 'skip' fallback returns (True, '')
        — congratulation without a gift, year-marker intentionally consumed.
        """
        reward_type = BirthdaySettingsService.get_reward_type()
        amount = BirthdaySettingsService.get_reward_amount()

        if reward_type == 'subscription_days':
            subs = await get_active_subscriptions_by_user_id(db, user.id)
            if subs:
                from app.database.crud.subscription import extend_subscription

                await extend_subscription(db, subs[0], amount, commit=False)
                return True, f'+{amount} дней подписки'
            fallback = BirthdaySettingsService.get_subscription_days_fallback()
            if fallback == 'skip':
                return True, ''
            ok = await add_user_balance(
                db, user, amount, description='🎂 Подарок на день рождения',
                transaction_type=TransactionType.DEPOSIT, commit=False,
            )
            return ok, (f'{amount / 100:.0f} ₽ на баланс' if ok else '')

        # 'promocode' currently credits balance (real promocode minting is a follow-up).
        ok = await add_user_balance(
            db, user, amount, description='🎂 Подарок на день рождения',
            transaction_type=TransactionType.DEPOSIT, commit=False,
        )
        return ok, (f'{amount / 100:.0f} ₽ на баланс' if ok else '')

    async def _notify(self, user, reward_desc: str) -> None:
        if self._bot is None or not getattr(user, 'telegram_id', None):
            return
        gift_line = f'\n\nВаш подарок: <b>{reward_desc}</b> 🎁' if reward_desc else ''
        text = f'🎂 <b>С днём рождения!</b>{gift_line}'
        try:
            await self._bot.send_message(user.telegram_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('birthday.notify_failed', user_id=user.id, err=str(exc))

    async def start_monitoring(self) -> None:
        self._running = True
        logger.info('birthday.scheduler.start')
        while self._running:
            interval = 3600
            try:
                if self.is_enabled() and BirthdaySettingsService.is_enabled():
                    async with AsyncSessionLocal() as db:
                        await self._grant_birthday_rewards(db)
            except Exception as exc:
                logger.error('birthday.scheduler.error', err=str(exc), exc_info=True)
            await asyncio.sleep(interval)

    def stop_monitoring(self) -> None:
        self._running = False
        logger.info('birthday.scheduler.stop')


birthday_service = BirthdayService()
