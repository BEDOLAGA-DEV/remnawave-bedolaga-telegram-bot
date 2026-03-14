from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

import structlog
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    ReferralContest,
    ReferralContestEvent,
    ReferralContestVirtualParticipant,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)


async def create_referral_contest(
    db: AsyncSession,
    *,
    title: str,
    description: str | None,
    prize_text: str | None,
    contest_type: str,
    start_at: datetime,
    end_at: datetime,
    daily_summary_time: time,
    daily_summary_times: str | None = None,
    timezone_name: str,
    created_by: int | None = None,
) -> ReferralContest:
    contest = ReferralContest(
        title=title,
        description=description,
        prize_text=prize_text,
        contest_type=contest_type,
        start_at=start_at,
        end_at=end_at,
        daily_summary_time=daily_summary_time,
        daily_summary_times=daily_summary_times,
        timezone=timezone_name or 'UTC',
        created_by=created_by,
    )
    db.add(contest)
    await db.commit()
    await db.refresh(contest)
    return contest


async def list_referral_contests(
    db: AsyncSession,
    *,
    limit: int = 10,
    offset: int = 0,
    contest_type: str | None = None,
) -> list[ReferralContest]:
    query = (
        select(ReferralContest)
        .options(selectinload(ReferralContest.creator))
        .order_by(desc(ReferralContest.start_at))
        .offset(offset)
        .limit(limit)
    )
    if contest_type:
        query = query.where(ReferralContest.contest_type == contest_type)

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_referral_contests_count(db: AsyncSession, contest_type: str | None = None) -> int:
    query = select(func.count(ReferralContest.id))
    if contest_type:
        query = query.where(ReferralContest.contest_type == contest_type)
    result = await db.execute(query)
    return int(result.scalar_one())


async def get_referral_contest(db: AsyncSession, contest_id: int) -> ReferralContest | None:
    result = await db.execute(
        select(ReferralContest)
        .options(
            selectinload(ReferralContest.creator),
            selectinload(ReferralContest.events),
        )
        .where(ReferralContest.id == contest_id)
    )
    return result.scalar_one_or_none()


async def update_referral_contest(
    db: AsyncSession,
    contest: ReferralContest,
    **fields: object,
) -> ReferralContest:
    for key, value in fields.items():
        if hasattr(contest, key):
            setattr(contest, key, value)
    await db.commit()
    await db.refresh(contest)
    return contest


async def toggle_referral_contest(
    db: AsyncSession,
    contest: ReferralContest,
    is_active: bool,
) -> ReferralContest:
    contest.is_active = is_active
    return await update_referral_contest(db, contest)


async def get_contests_for_events(
    db: AsyncSession,
    now_utc: datetime,
    *,
    contest_types: list[str] | None = None,
) -> list[ReferralContest]:
    # Расширяем SQL-фильтр на 1 день для полночных end_at (нормализуются в 23:59:59)
    query = select(ReferralContest).where(
        and_(
            ReferralContest.is_active.is_(True),
            ReferralContest.start_at <= now_utc,
            ReferralContest.end_at >= now_utc - timedelta(days=1),
        )
    )
    if contest_types:
        query = query.where(ReferralContest.contest_type.in_(contest_types))

    result = await db.execute(query)
    contests = list(result.scalars().all())

    # Точная фильтрация с нормализацией полночных end_at
    filtered = []
    for contest in contests:
        contest_end = contest.end_at
        if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
            contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)
        if contest_end >= now_utc:
            filtered.append(contest)
    return filtered


async def get_contests_for_summaries(db: AsyncSession) -> list[ReferralContest]:
    result = await db.execute(select(ReferralContest).where(ReferralContest.is_active.is_(True)))
    return list(result.scalars().all())


async def add_contest_event(
    db: AsyncSession,
    *,
    contest_id: int,
    referrer_id: int,
    referral_id: int,
    amount_kopeks: int = 0,
    event_type: str = 'subscription_purchase',
) -> ReferralContestEvent | None:
    existing_result = await db.execute(
        select(ReferralContestEvent).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.referral_id == referral_id,
            )
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        # Обновляем amount_kopeks если повторная покупка (upsert)
        if amount_kopeks and existing.amount_kopeks != amount_kopeks:
            existing.amount_kopeks = amount_kopeks
            await db.commit()
            await db.refresh(existing)
        return None

    event = ReferralContestEvent(
        contest_id=contest_id,
        referrer_id=referrer_id,
        referral_id=referral_id,
        amount_kopeks=amount_kopeks,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def get_contest_leaderboard(
    db: AsyncSession,
    contest_id: int,
    *,
    limit: int | None = None,
) -> Sequence[tuple[User, int, int]]:
    """Получить лидерборд конкурса.

    Учитывает только рефералов, зарегистрированных В ПЕРИОД конкурса.
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return []

    # Нормализуем границы дат
    contest_start = contest.start_at
    contest_end = contest.end_at
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    query = (
        select(
            User,
            func.count(ReferralContestEvent.id).label('referral_count'),
            func.coalesce(func.sum(func.abs(ReferralContestEvent.amount_kopeks)), 0).label('total_amount'),
        )
        .join(User, User.id == ReferralContestEvent.referrer_id)
        .where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
        .group_by(User.id)
        .order_by(desc('referral_count'), desc('total_amount'), User.id)
    )
    if limit:
        query = query.limit(limit)
    result = await db.execute(query)
    leaderboard = result.all()

    return leaderboard


async def get_contest_participants(
    db: AsyncSession,
    contest_id: int,
) -> Sequence[tuple[User, int]]:
    """Получить участников конкурса.

    Учитывает только рефералов, зарегистрированных В ПЕРИОД конкурса.
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return []

    contest_start = contest.start_at
    contest_end = contest.end_at
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    result = await db.execute(
        select(User, func.count(ReferralContestEvent.id).label('referral_count'))
        .join(User, User.id == ReferralContestEvent.referrer_id)
        .where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
        .group_by(User.id)
    )
    return result.all()


async def get_referrer_score(
    db: AsyncSession,
    contest_id: int,
    referrer_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    query = select(func.count(ReferralContestEvent.id)).where(
        and_(
            ReferralContestEvent.contest_id == contest_id,
            ReferralContestEvent.referrer_id == referrer_id,
        )
    )
    if start:
        query = query.where(ReferralContestEvent.occurred_at >= start)
    if end:
        query = query.where(ReferralContestEvent.occurred_at < end)

    result = await db.execute(query)
    return int(result.scalar_one())


async def get_contest_events_count(
    db: AsyncSession,
    contest_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    query = select(func.count(ReferralContestEvent.id)).where(ReferralContestEvent.contest_id == contest_id)
    if start:
        query = query.where(ReferralContestEvent.occurred_at >= start)
    if end:
        query = query.where(ReferralContestEvent.occurred_at < end)
    result = await db.execute(query)
    return int(result.scalar_one())


async def get_contest_events(
    db: AsyncSession,
    contest_id: int,
) -> list[ReferralContestEvent]:
    result = await db.execute(select(ReferralContestEvent).where(ReferralContestEvent.contest_id == contest_id))
    return list(result.scalars().all())


async def mark_daily_summary_sent(
    db: AsyncSession,
    contest: ReferralContest,
    summary_date: date,
    summary_dt_utc: datetime | None = None,
) -> ReferralContest:
    contest.last_daily_summary_date = summary_date
    if summary_dt_utc:
        contest.last_daily_summary_at = summary_dt_utc
    await db.commit()
    await db.refresh(contest)
    return contest


async def mark_final_summary_sent(
    db: AsyncSession,
    contest: ReferralContest,
) -> ReferralContest:
    contest.final_summary_sent = True
    contest.is_active = False
    await db.commit()
    await db.refresh(contest)
    return contest


async def delete_referral_contest(
    db: AsyncSession,
    contest: ReferralContest,
) -> None:
    await db.delete(contest)
    await db.commit()


async def get_contest_payment_stats(
    db: AsyncSession,
    contest_id: int,
) -> dict:
    """Получить статистику оплат по конкурсу.

    Учитывает только рефералов, зарегистрированных В ПЕРИОД конкурса.

    Returns:
        dict: {
            "paid_count": int,    # Рефералов с платежами > 0
            "unpaid_count": int,  # Рефералов без платежей
            "total_amount": int,  # Общая сумма платежей
        }
    """
    # Получаем даты конкурса для фильтрации
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return {'paid_count': 0, 'unpaid_count': 0, 'total_amount': 0}

    contest_start = contest.start_at
    contest_end = contest.end_at
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Считаем рефералов с платежами (только зарегистрированных в период конкурса)
    paid_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.amount_kopeks > 0,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
    )
    paid_count = int(paid_result.scalar_one() or 0)

    # Считаем рефералов без платежей (только зарегистрированных в период конкурса)
    unpaid_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.amount_kopeks == 0,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
    )
    unpaid_count = int(unpaid_result.scalar_one() or 0)

    # Общая сумма (только за рефералов зарегистрированных в период конкурса)
    total_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(ReferralContestEvent.amount_kopeks)), 0)).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
    )
    total_amount = int(total_result.scalar_one() or 0)

    return {
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'total_amount': total_amount,
    }


async def get_contest_transaction_breakdown(
    db: AsyncSession,
    contest_id: int,
) -> dict:
    """Получить разбивку транзакций по типам для конкурса.

    Учитывает только рефералов, зарегистрированных В ПЕРИОД конкурса.

    Returns:
        dict: {
            "subscription_total": int,  # Сумма покупок подписок (копейки)
            "deposit_total": int,       # Сумма пополнений баланса (копейки)
        }
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return {'subscription_total': 0, 'deposit_total': 0}

    contest_start = contest.start_at
    contest_end = contest.end_at
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Получаем referral_id только из событий в период конкурса
    events_result = await db.execute(
        select(ReferralContestEvent.referral_id).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
    )
    referral_ids = [r[0] for r in events_result.fetchall()]

    if not referral_ids:
        return {'subscription_total': 0, 'deposit_total': 0}

    # Сумма покупок подписок
    subscription_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
    )
    subscription_total = int(subscription_result.scalar_one() or 0)

    # Сумма пополнений баланса (ТОЛЬКО реальные платежи, БЕЗ бонусов)
    # Бонусы имеют payment_method = NULL, реальные платежи всегда имеют payment_method
    deposit_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.payment_method.is_not(None),  # Исключаем системные бонусы
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
    )
    deposit_total = int(deposit_result.scalar_one() or 0)

    return {
        'subscription_total': subscription_total,
        'deposit_total': deposit_total,
    }


async def upsert_contest_event(
    db: AsyncSession,
    *,
    contest_id: int,
    referrer_id: int,
    referral_id: int,
    amount_kopeks: int = 0,
    event_type: str = 'subscription_purchase',
) -> tuple[ReferralContestEvent, bool]:
    """Создать или обновить событие конкурса.

    Returns:
        Tuple[ReferralContestEvent, bool]: (событие, создано_новое)
    """
    result = await db.execute(
        select(ReferralContestEvent).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.referral_id == referral_id,
            )
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # Обновляем сумму если она изменилась
        if existing.amount_kopeks != amount_kopeks:
            existing.amount_kopeks = amount_kopeks
            await db.commit()
            await db.refresh(existing)
        return existing, False

    event = ReferralContestEvent(
        contest_id=contest_id,
        referrer_id=referrer_id,
        referral_id=referral_id,
        amount_kopeks=amount_kopeks,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event, True


async def debug_contest_transactions(
    db: AsyncSession,
    contest_id: int,
    limit: int = 20,
) -> dict:
    """Показать транзакции которые учитываются в конкурсе для отладки.

    Возвращает информацию о транзакциях рефералов конкурса,
    чтобы понять какие именно платежи считаются.
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return {'error': 'Contest not found'}

    # Нормализуем границы дат
    contest_start = contest.start_at
    contest_end = contest.end_at

    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Получаем referral_id ТОЛЬКО из событий которые произошли в период конкурса
    events_result = await db.execute(
        select(ReferralContestEvent.referral_id).where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                ReferralContestEvent.occurred_at >= contest_start,
                ReferralContestEvent.occurred_at <= contest_end,
            )
        )
    )
    referral_ids = [r[0] for r in events_result.fetchall()]

    # Также считаем сколько всего событий для сравнения
    all_events_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(ReferralContestEvent.contest_id == contest_id)
    )
    total_all_events = int(all_events_result.scalar_one() or 0)

    if not referral_ids:
        return {
            'contest_start': contest_start.isoformat(),
            'contest_end': contest_end.isoformat(),
            'referral_count': 0,
            'total_all_events': total_all_events,
            'transactions': [],
        }

    # Получаем транзакции этих рефералов ЗА период конкурса
    transactions_in_period = await db.execute(
        select(Transaction)
        .where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    [
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    ]
                ),
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
        .order_by(desc(Transaction.created_at))
        .limit(limit)
    )
    txs_in = transactions_in_period.scalars().all()

    # Также получаем транзакции ВНЕ периода для сравнения
    transactions_outside = await db.execute(
        select(Transaction)
        .where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    [
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    ]
                ),
                func.not_(
                    and_(
                        Transaction.created_at >= contest_start,
                        Transaction.created_at <= contest_end,
                    )
                ),
            )
        )
        .order_by(desc(Transaction.created_at))
        .limit(limit)
    )
    txs_out = transactions_outside.scalars().all()

    # Подсчёт общих сумм ПО ТИПАМ (исключаем бонусы без payment_method)
    deposit_in_period = sum(
        tx.amount_kopeks for tx in txs_in if tx.type == TransactionType.DEPOSIT.value and tx.payment_method is not None
    )
    subscription_in_period = sum(
        abs(tx.amount_kopeks) for tx in txs_in if tx.type == TransactionType.SUBSCRIPTION_PAYMENT.value
    )
    total_in_period = deposit_in_period + subscription_in_period
    total_outside = sum(abs(tx.amount_kopeks) for tx in txs_out)

    # Подсчёт ПОЛНЫХ сумм (не только sample, БЕЗ бонусов)
    full_deposit_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.payment_method.is_not(None),  # Исключаем системные бонусы
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
    )
    full_deposit_total = int(full_deposit_result.scalar_one() or 0)

    full_subscription_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.user_id.in_(referral_ids),
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
    )
    full_subscription_total = int(full_subscription_result.scalar_one() or 0)

    return {
        'contest_start': contest_start.isoformat(),
        'contest_end': contest_end.isoformat(),
        'referral_count': len(referral_ids),
        'total_all_events': total_all_events,
        'filtered_out': total_all_events - len(referral_ids),
        'transactions_in_period': [
            {
                'id': tx.id,
                'user_id': tx.user_id,
                'type': tx.type,
                'amount_kopeks': tx.amount_kopeks,
                'created_at': tx.created_at.isoformat() if tx.created_at else None,
                'payment_method': tx.payment_method,
            }
            for tx in txs_in
        ],
        'transactions_outside_period': [
            {
                'id': tx.id,
                'user_id': tx.user_id,
                'type': tx.type,
                'amount_kopeks': tx.amount_kopeks,
                'created_at': tx.created_at.isoformat() if tx.created_at else None,
                'payment_method': tx.payment_method,
            }
            for tx in txs_out
        ],
        'total_in_period_kopeks': total_in_period,
        'total_outside_period_kopeks': total_outside,
        'deposit_total_kopeks': full_deposit_total,
        'subscription_total_kopeks': full_subscription_total,
        'sample_size': limit,
    }


async def sync_contest_events(
    db: AsyncSession,
    contest_id: int,
) -> dict:
    """Синхронизировать события конкурса с реальными данными.

    Обновляет ВСЕ существующие события конкурса, пересчитывая платежи
    каждого реферала СТРОГО за период конкурса (start_at - end_at).

    Returns:
        dict: {
            "updated": int,  # Событий обновлено
            "skipped": int,  # Пропущено (нет изменений)
            "total_events": int,  # Всего событий проверено
            "total_amount": int,  # Общая сумма платежей
            "paid_count": int,  # Рефералов с платежами
            "unpaid_count": int,  # Рефералов без платежей
        }
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return {'error': 'Contest not found'}

    # Нормализуем границы дат для СТРОГОЙ фильтрации
    # start_at должен быть началом дня (00:00:00)
    # end_at должен быть концом дня (23:59:59.999999)
    contest_start = contest.start_at
    contest_end = contest.end_at

    # Если start_at содержит только дату (время 00:00), оставляем как есть
    # Если end_at содержит только дату, добавляем время до конца дня
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        # Конец дня: 23:59:59.999999
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    logger.info(
        'Синхронизация конкурса : период с по',
        contest_id=contest_id,
        contest_start=contest_start,
        contest_end=contest_end,
    )

    stats = {
        'updated': 0,
        'skipped': 0,
        'total_events': 0,
        'total_amount': 0,
        'paid_count': 0,
        'unpaid_count': 0,
        'contest_start': contest_start.isoformat(),
        'contest_end': contest_end.isoformat(),
    }

    # Получаем события конкурса ТОЛЬКО для рефералов, зарегистрированных в период конкурса
    # (проверяем User.created_at, а не ReferralContestEvent.occurred_at)
    events_result = await db.execute(
        select(ReferralContestEvent)
        .join(User, User.id == ReferralContestEvent.referral_id)
        .where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                User.created_at >= contest_start,
                User.created_at <= contest_end,
            )
        )
    )
    events = events_result.scalars().all()

    # Также считаем сколько всего событий (для отладки)
    all_events_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(ReferralContestEvent.contest_id == contest_id)
    )
    total_all_events = int(all_events_result.scalar_one() or 0)

    stats['total_events'] = len(events)
    stats['total_all_events'] = total_all_events
    stats['filtered_out_events'] = total_all_events - len(events)

    stats['deposit_total'] = 0
    stats['subscription_total'] = 0

    for event in events:
        # Считаем ТОЛЬКО покупки подписок (реальные траты на подписки)
        subscription_query = select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.user_id == event.referral_id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
        sub_result = await db.execute(subscription_query)
        subscription_paid = int(sub_result.scalar_one() or 0)

        # Также считаем пополнения баланса (ТОЛЬКО реальные платежи, БЕЗ бонусов)
        deposit_query = select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.user_id == event.referral_id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.payment_method.is_not(None),  # Исключаем системные бонусы
                Transaction.created_at >= contest_start,
                Transaction.created_at <= contest_end,
            )
        )
        dep_result = await db.execute(deposit_query)
        deposit_paid = int(dep_result.scalar_one() or 0)

        stats['subscription_total'] += subscription_paid
        stats['deposit_total'] += deposit_paid

        # Основная метрика — покупки подписок
        total_paid = subscription_paid

        # Считаем статистику
        if total_paid > 0:
            stats['total_amount'] += total_paid
            stats['paid_count'] += 1
        else:
            stats['unpaid_count'] += 1

        # Обновляем сумму если изменилась
        if event.amount_kopeks != total_paid:
            old_amount = event.amount_kopeks
            event.amount_kopeks = total_paid
            stats['updated'] += 1
            # Логируем значительные изменения
            if abs(old_amount - total_paid) > 10000:  # больше 100 руб разницы
                logger.debug(
                    'Событие (реферал): -> коп.',
                    event_id=event.id,
                    referral_id=event.referral_id,
                    old_amount=old_amount,
                    total_paid=total_paid,
                )
        else:
            stats['skipped'] += 1

    # Сохраняем изменения
    await db.commit()

    logger.info(
        'Синхронизация конкурса завершена: обновлено , пропущено , сумма коп.',
        contest_id=contest_id,
        stats=stats['updated'],
        stats_2=stats['skipped'],
        stats_3=stats['total_amount'],
    )

    return stats


async def cleanup_invalid_contest_events(
    db: AsyncSession,
    contest_id: int,
) -> dict:
    """Удалить события конкурса для рефералов, зарегистрированных ВНЕ периода конкурса.

    Эта функция очищает неправильные события, созданные до исправления бага.
    Удаляет события только для рефералов, чья дата регистрации (User.created_at)
    находится вне периода конкурса (contest.start_at - contest.end_at).

    Returns:
        dict: {
            "deleted": int,  # Количество удалённых событий
            "remaining": int,  # Осталось валидных событий
            "total_before": int,  # Было событий до очистки
        }
    """
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        return {'error': 'Contest not found'}

    # Нормализуем границы дат
    contest_start = contest.start_at
    contest_end = contest.end_at
    if contest_end.hour == 0 and contest_end.minute == 0 and contest_end.second == 0:
        contest_end = contest_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    logger.info(
        'Очистка конкурса : период с по', contest_id=contest_id, contest_start=contest_start, contest_end=contest_end
    )

    # Считаем сколько было событий до очистки
    total_before_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(ReferralContestEvent.contest_id == contest_id)
    )
    total_before = int(total_before_result.scalar_one() or 0)

    # Находим события для рефералов, зарегистрированных ВНЕ периода конкурса
    invalid_events_result = await db.execute(
        select(ReferralContestEvent.id)
        .join(User, User.id == ReferralContestEvent.referral_id)
        .where(
            and_(
                ReferralContestEvent.contest_id == contest_id,
                func.not_(
                    and_(
                        User.created_at >= contest_start,
                        User.created_at <= contest_end,
                    )
                ),
            )
        )
    )
    invalid_event_ids = [row[0] for row in invalid_events_result.fetchall()]

    deleted = 0
    if invalid_event_ids:
        # Удаляем невалидные события
        from sqlalchemy import delete as sql_delete

        delete_result = await db.execute(
            sql_delete(ReferralContestEvent).where(ReferralContestEvent.id.in_(invalid_event_ids))
        )
        deleted = delete_result.rowcount
        await db.commit()

    # Считаем сколько осталось валидных событий
    remaining_result = await db.execute(
        select(func.count(ReferralContestEvent.id)).where(ReferralContestEvent.contest_id == contest_id)
    )
    remaining = int(remaining_result.scalar_one() or 0)

    logger.info(
        'Очистка конкурса завершена: удалено невалидных событий, осталось валидных (было)',
        contest_id=contest_id,
        deleted=deleted,
        remaining=remaining,
        total_before=total_before,
    )

    return {
        'deleted': deleted,
        'remaining': remaining,
        'total_before': total_before,
        'contest_start': contest_start.isoformat(),
        'contest_end': contest_end.isoformat(),
    }


# ── Виртуальные участники ──────────────────────────────────────────────


async def add_virtual_participant(
    db: AsyncSession,
    contest_id: int,
    display_name: str,
    referral_count: int,
    total_amount_kopeks: int = 0,
) -> ReferralContestVirtualParticipant:
    vp = ReferralContestVirtualParticipant(
        contest_id=contest_id,
        display_name=display_name,
        referral_count=referral_count,
        total_amount_kopeks=total_amount_kopeks,
    )
    db.add(vp)
    await db.commit()
    await db.refresh(vp)
    return vp


async def list_virtual_participants(
    db: AsyncSession,
    contest_id: int,
) -> Sequence[ReferralContestVirtualParticipant]:
    result = await db.execute(
        select(ReferralContestVirtualParticipant)
        .where(ReferralContestVirtualParticipant.contest_id == contest_id)
        .order_by(ReferralContestVirtualParticipant.referral_count.desc())
    )
    return result.scalars().all()


async def delete_virtual_participant(
    db: AsyncSession,
    participant_id: int,
) -> bool:
    result = await db.execute(
        select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == participant_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        return False
    await db.delete(vp)
    await db.commit()
    return True


async def update_virtual_participant_count(
    db: AsyncSession,
    participant_id: int,
    referral_count: int,
) -> ReferralContestVirtualParticipant | None:
    result = await db.execute(
        select(ReferralContestVirtualParticipant).where(ReferralContestVirtualParticipant.id == participant_id)
    )
    vp = result.scalar_one_or_none()
    if not vp:
        return None
    vp.referral_count = referral_count
    await db.commit()
    await db.refresh(vp)
    return vp


async def get_contest_leaderboard_with_virtual(
    db: AsyncSession,
    contest_id: int,
    *,
    limit: int | None = None,
) -> list[tuple[str, int, int, bool]]:
    """Лидерборд с виртуальными участниками.

    Возвращает список кортежей (display_name, referral_count, total_amount, is_virtual).
    """
    real = await get_contest_leaderboard(db, contest_id)
    virtual = await list_virtual_participants(db, contest_id)

    merged: list[tuple[str, int, int, bool]] = []
    for user, score, amount in real:
        merged.append((user.full_name, score, amount, False))
    for vp in virtual:
        merged.append((vp.display_name, vp.referral_count, vp.total_amount_kopeks, True))

    merged.sort(key=lambda x: (-x[1], -x[2]))

    if limit:
        merged = merged[:limit]

    return merged
