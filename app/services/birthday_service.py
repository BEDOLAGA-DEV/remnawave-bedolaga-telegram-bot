from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from aiogram import Bot

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
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


birthday_service = BirthdayService()
