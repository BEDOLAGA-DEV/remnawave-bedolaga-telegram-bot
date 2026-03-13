from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.runtime_context import get_current_bot_id


_bots_by_id: dict[int, Bot] = {}
_primary_bot_id: int | None = None


def init_runtime_bots(bots: Iterable[Bot], primary_bot_id: int | None) -> None:
    global _bots_by_id, _primary_bot_id
    mapped: dict[int, Bot] = {}
    for bot in bots:
        bot_id = getattr(bot, 'id', None)
        if isinstance(bot_id, int):
            mapped[bot_id] = bot
    _bots_by_id = mapped
    _primary_bot_id = primary_bot_id


def get_primary_bot() -> Bot | None:
    if _primary_bot_id is not None and _primary_bot_id in _bots_by_id:
        return _bots_by_id[_primary_bot_id]
    return next(iter(_bots_by_id.values()), None)


def get_bot_by_id(bot_id: int | None) -> Bot | None:
    if bot_id is None:
        return None
    return _bots_by_id.get(int(bot_id))


def resolve_bot_in_context(default_bot: Bot | None = None) -> Bot | None:
    context_bot_id = get_current_bot_id()
    context_bot = get_bot_by_id(context_bot_id)
    if context_bot is not None:
        return context_bot
    if default_bot is not None:
        return default_bot
    return get_primary_bot()


async def _get_user_last_bot_id_by_telegram_id(
    telegram_id: int,
    db: AsyncSession | None = None,
) -> int | None:
    if db is not None:
        result = await db.execute(select(User.last_bot_id).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User.last_bot_id).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()


async def resolve_bot_for_user(
    *,
    user: Any | None = None,
    telegram_id: int | None = None,
    fallback_bot: Bot | None = None,
    db: AsyncSession | None = None,
    explicit_bot_id: int | None = None,
) -> Bot | None:
    if explicit_bot_id is not None:
        explicit_bot = get_bot_by_id(explicit_bot_id)
        if explicit_bot is not None:
            return explicit_bot

    context_bot_id = get_current_bot_id()
    if context_bot_id is not None:
        context_bot = get_bot_by_id(context_bot_id)
        if context_bot is not None:
            return context_bot

    last_bot_id = getattr(user, 'last_bot_id', None) if user is not None else None
    bot = get_bot_by_id(last_bot_id)
    if bot is not None:
        return bot

    effective_telegram_id = telegram_id
    if effective_telegram_id is None and user is not None:
        effective_telegram_id = getattr(user, 'telegram_id', None)

    if effective_telegram_id:
        db_last_bot_id = await _get_user_last_bot_id_by_telegram_id(int(effective_telegram_id), db=db)
        bot = get_bot_by_id(db_last_bot_id)
        if bot is not None:
            return bot

    if fallback_bot is not None:
        return fallback_bot
    return get_primary_bot()
