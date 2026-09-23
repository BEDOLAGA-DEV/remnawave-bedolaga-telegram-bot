"""Напоминания пользователям: выборки, состояние по человеку, статистика, аудитория."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserReminder, UserReminderState
from app.services.user_reminders.conditions import ReminderConditions, condition_clauses


CHANNELS_BOT = ('bot', 'both')
CHANNELS_CABINET = ('cabinet', 'both')
# Встроенные первыми: builtin_key IS NULL даёт False (0) у встроенных.
_ORDER = (UserReminder.builtin_key.is_(None), UserReminder.id)


async def list_reminders(db: AsyncSession) -> list[UserReminder]:
    return list((await db.execute(select(UserReminder).order_by(*_ORDER))).scalars())


async def list_active_reminders(db: AsyncSession, channels: tuple[str, ...]) -> list[UserReminder]:
    query = (
        select(UserReminder)
        .where(UserReminder.is_active.is_(True), UserReminder.channels.in_(channels))
        .order_by(*_ORDER)
    )
    return list((await db.execute(query)).scalars())


async def get_reminder(db: AsyncSession, reminder_id: int) -> UserReminder | None:
    return await db.get(UserReminder, reminder_id)


async def _find_state(db: AsyncSession, reminder_id: int, user_id: int) -> UserReminderState | None:
    query = select(UserReminderState).where(
        UserReminderState.reminder_id == reminder_id, UserReminderState.user_id == user_id
    )
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_state(db: AsyncSession, reminder_id: int, user_id: int) -> UserReminderState | None:
    state = await _find_state(db, reminder_id, user_id)
    if state is not None:
        return state
    try:
        async with db.begin_nested():
            state = UserReminderState(reminder_id=reminder_id, user_id=user_id, sends_count=0)
            db.add(state)
            await db.flush((state,))
        return state
    except IntegrityError:
        # Либо параллельный запрос успел создать строку (пара уникальна) — берём её.
        # Либо это FOREIGN KEY: user/reminder удалены между select-кандидатом и этой
        # вставкой — тогда повторный поиск тоже вернёт None, и вызывающий сам решает,
        # что делать (savepoint уже откатился, внешняя транзакция жива).
        return await _find_state(db, reminder_id, user_id)


async def record_bot_attempt(db: AsyncSession, reminder_id: int, user_id: int, *, now: datetime, success: bool) -> None:
    state = await get_or_create_state(db, reminder_id, user_id)
    if state is None:
        # Пользователь/напоминание удалены между отбором кандидата и записью попытки —
        # писать некуда, но проход не должен падать целиком из-за одного кандидата.
        return
    state.last_sent_at = now
    if success:
        state.sends_count = (state.sends_count or 0) + 1
        state.last_success_at = now
    await db.commit()


async def reminder_stats(db: AsyncSession) -> dict[int, dict]:
    query = select(
        UserReminderState.reminder_id,
        func.coalesce(func.sum(UserReminderState.sends_count), 0),
        func.count(UserReminderState.dismissed_at),
    ).group_by(UserReminderState.reminder_id)
    return {
        reminder_id: {'sent_total': int(sent), 'dismissed_total': int(dismissed)}
        for reminder_id, sent, dismissed in (await db.execute(query)).all()
    }


async def count_audience(
    db: AsyncSession,
    conditions: ReminderConditions,
    *,
    now: datetime,
    telegram_only: bool,
    exclude_promo_opt_out: bool = False,
) -> int:
    clauses = condition_clauses(conditions, now=now)
    if telegram_only:
        clauses.append(User.telegram_id.is_not(None))
    if exclude_promo_opt_out:
        # notification_settings — JSONB (JSON на SQLite в тестах); ключа может не быть
        # вовсе (старые пользователи) или всего поля — тогда считаем «не отписан».
        promo_flag = User.notification_settings['promo_offers_enabled'].as_boolean()
        clauses.append(or_(User.notification_settings.is_(None), promo_flag.is_(None), promo_flag.is_(True)))
    return int((await db.execute(select(func.count(User.id)).where(*clauses))).scalar() or 0)
