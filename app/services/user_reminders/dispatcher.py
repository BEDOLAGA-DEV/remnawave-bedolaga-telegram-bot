"""Один проход отправки напоминаний в бот.

Кандидаты — одним SQL-запросом на напоминание: условия, есть Telegram, окно повтора и
лимит отправок, общий лимит «одно напоминание в сутки». Отправка — через общий сток
уведомлений (блокировки, rich-вид, повторы). Состояние коммитится после каждой отправки,
чтобы сбой посреди прохода не дал дубль.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user_reminder import CHANNELS_BOT, list_active_reminders, record_bot_attempt
from app.database.models import User, UserReminderState
from app.services.notification_types import NotificationType
from app.services.user_reminders.conditions import condition_clauses, parse_conditions
from app.services.user_reminders.texts import render_bot_message, validate_texts
from app.utils.notification_prefs import is_promo_offers_enabled
from app.utils.timezone import get_local_timezone


logger = structlog.get_logger(__name__)

SEND_BATCH = 25
SEND_BATCH_PAUSE_SECONDS = 1.0
DAILY_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class PassResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


def is_quiet_time(now: datetime, *, start_hour: int, end_hour: int, tz) -> bool:
    if start_hour == end_hour:
        return False
    hour = now.astimezone(tz).hour
    if start_hour > end_hour:  # через полночь: 21 → 10
        return hour >= start_hour or hour < end_hour
    return start_hour <= hour < end_hour


Deliver = Callable[[Any, Any, Any], Awaitable[bool]]


def bot_delivery(delivery_service) -> Deliver:
    """Отправка напоминания через общий сток уведомлений.

    Сток передаёт вызывающий (мониторинг): импорт отсюда замыкал кольцо
    «мониторинг → напоминания → сток уведомлений → … → мониторинг» (CodeQL
    py/cyclic-import).
    """

    async def deliver(user, reminder, bot) -> bool:
        text, markup = render_bot_message(reminder, user.language)
        return await delivery_service.send_notification(
            user,
            NotificationType.USER_REMINDER,
            {'reminder_id': reminder.id},
            bot=bot,
            telegram_message=text,
            telegram_markup=markup,
            use_websocket=False,
        )

    return deliver


def _candidates_query(
    reminder, conditions, *, now: datetime, exclude: set[int], limit: int, after_id: int | None = None
):
    repeat_cutoff = now - timedelta(days=reminder.repeat_every_days)
    blocked_by_own_state = exists().where(
        UserReminderState.reminder_id == reminder.id,
        UserReminderState.user_id == User.id,
        or_(
            UserReminderState.sends_count >= reminder.max_sends,
            and_(UserReminderState.last_sent_at.is_not(None), UserReminderState.last_sent_at > repeat_cutoff),
        ),
    )
    clauses = [*condition_clauses(conditions, now=now), User.telegram_id.is_not(None), ~blocked_by_own_state]
    if settings.USER_REMINDERS_DAILY_LIMIT_ENABLED:
        clauses.append(
            ~exists().where(
                UserReminderState.user_id == User.id, UserReminderState.last_success_at > now - DAILY_WINDOW
            )
        )
    if exclude:
        clauses.append(User.id.not_in(exclude))
    if after_id is not None:
        clauses.append(User.id > after_id)
    return select(User).where(*clauses).order_by(User.id).limit(limit)


async def run_reminder_pass(
    db: AsyncSession, bot, *, deliver: Deliver, now: datetime | None = None, sleep=asyncio.sleep
) -> PassResult:
    now = now or datetime.now(UTC)
    if is_quiet_time(
        now,
        start_hour=settings.USER_REMINDERS_QUIET_HOURS_START,
        end_hour=settings.USER_REMINDERS_QUIET_HOURS_END,
        tz=get_local_timezone(),
    ):
        return PassResult()

    budget = max(1, int(settings.USER_REMINDERS_MAX_PER_PASS))
    touched: set[int] = set()
    sent = failed = skipped = attempts = 0

    for reminder in await list_active_reminders(db, CHANNELS_BOT):
        if budget <= 0:
            break
        try:
            conditions = parse_conditions(reminder.conditions)
        except ValidationError as error:
            logger.warning('Напоминание с битыми условиями пропущено', reminder_id=reminder.id, error=str(error))
            continue
        try:
            validate_texts(reminder.texts)
        except ValueError as error:
            # render_bot_message ждёт texts['ru'] безусловно — без этой проверки
            # KeyError вылетал бы на каждом кандидате этого напоминания.
            logger.warning('Напоминание с битыми текстами пропущено', reminder_id=reminder.id, error=str(error))
            continue

        # Порциями по курсору, пока не кончится бюджет или кандидаты: отписанные от
        # промо бюджет не тратят, и одна порция размером в бюджет могла целиком из
        # них состоять — дальше по списку в этот проход не доходил никто, а после
        # окна повтора та же голова очереди снова всё закрывала (ревью PR #3280).
        after_id: int | None = None
        while budget > 0:
            users = list(
                (
                    await db.execute(
                        _candidates_query(
                            reminder, conditions, now=now, exclude=touched, limit=budget, after_id=after_id
                        )
                    )
                ).scalars()
            )
            if not users:
                break
            after_id = users[-1].id
            for user in users:
                touched.add(user.id)
                if reminder.category == 'marketing' and not is_promo_offers_enabled(user):
                    # Отписан от промо: отмечаем попыткой, чтобы он не занимал голову очереди каждый проход.
                    await record_bot_attempt(db, reminder.id, user.id, now=now, success=False)
                    skipped += 1
                    continue
                try:
                    ok = await deliver(user, reminder, bot)
                except Exception as error:
                    logger.warning(
                        'Сбой отправки напоминания', reminder_id=reminder.id, user_id=user.id, error=str(error)
                    )
                    ok = False
                await record_bot_attempt(db, reminder.id, user.id, now=now, success=ok)
                sent, failed = (sent + 1, failed) if ok else (sent, failed + 1)
                budget -= 1
                attempts += 1
                if attempts % SEND_BATCH == 0:
                    await sleep(SEND_BATCH_PAUSE_SECONDS)
                if budget <= 0:
                    break

    if sent or failed:
        logger.info('Проход напоминаний', sent=sent, failed=failed, skipped=skipped)
    return PassResult(sent=sent, failed=failed, skipped=skipped)
