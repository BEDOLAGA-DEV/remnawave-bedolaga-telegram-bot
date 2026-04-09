from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import PaymentMethod, Transaction, TransactionType, User


logger = structlog.get_logger(__name__)

# Реальные платёжные методы для подсчёта дохода
# Исключены: MANUAL (админские), BALANCE (оплата с баланса), NULL (колесо, промокоды, бонусы)
REAL_PAYMENT_METHODS = [
    PaymentMethod.TELEGRAM_STARS.value,
    PaymentMethod.TRIBUTE.value,
    PaymentMethod.YOOKASSA.value,
    PaymentMethod.CRYPTOBOT.value,
    PaymentMethod.HELEKET.value,
    PaymentMethod.MULENPAY.value,
    PaymentMethod.PAL24.value,
    PaymentMethod.WATA.value,
    PaymentMethod.PLATEGA.value,
    PaymentMethod.CLOUDPAYMENTS.value,
    PaymentMethod.FREEKASSA.value,
    PaymentMethod.KASSA_AI.value,
    PaymentMethod.RIOPAY.value,
    PaymentMethod.SEVERPAY.value,
]


async def create_transaction(
    db: AsyncSession,
    user_id: int,
    type: TransactionType,
    amount_kopeks: int,
    description: str,
    payment_method: PaymentMethod | None = None,
    external_id: str | None = None,
    is_completed: bool = True,
    created_at: datetime | None = None,
    *,
    commit: bool = True,
) -> Transaction:
    # SUBSCRIPTION_PAYMENT / GIFT_PAYMENT — always store as negative (debit from user balance)
    # Keep original for downstream consumers (events, contests)
    stored_amount = (
        -amount_kopeks
        if type in (TransactionType.SUBSCRIPTION_PAYMENT, TransactionType.GIFT_PAYMENT) and amount_kopeks > 0
        else amount_kopeks
    )

    # Default payment_method to BALANCE for subscription/gift payments from bot (not landing)
    # to avoid double-counting with DEPOSIT in revenue calculations
    if payment_method is None and type in (TransactionType.SUBSCRIPTION_PAYMENT, TransactionType.GIFT_PAYMENT):
        payment_method = PaymentMethod.BALANCE

    transaction = Transaction(
        user_id=user_id,
        type=type.value,
        amount_kopeks=stored_amount,
        description=description,
        payment_method=payment_method.value if payment_method else None,
        external_id=external_id,
        is_completed=is_completed,
        completed_at=datetime.now(UTC) if is_completed else None,
        **({'created_at': created_at} if created_at else {}),
    )

    db.add(transaction)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(transaction)

    logger.info(
        '💳 Создана транзакция: на ₽ для пользователя',
        type_value=type.value,
        amount_kopeks=stored_amount / 100,
        user_id=user_id,
    )

    # Side-effects skipped when commit=False to preserve caller's transaction atomicity.
    # Callers using commit=False should call emit_transaction_side_effects() after their own db.commit().
    if commit:
        try:
            from app.services.event_emitter import event_emitter

            await event_emitter.emit(
                'payment.completed' if type == TransactionType.DEPOSIT else 'transaction.created',
                {
                    'transaction_id': transaction.id,
                    'user_id': user_id,
                    'type': type.value,
                    'amount_kopeks': abs(amount_kopeks),
                    'amount_rubles': abs(amount_kopeks) / 100,
                    'payment_method': payment_method.value if payment_method else None,
                    'external_id': external_id,
                    'is_completed': is_completed,
                    'description': description,
                },
                db=db,
            )
        except Exception as error:
            logger.warning('Failed to emit transaction event', error=error)

        try:
            from app.services.promo_group_assignment import (
                maybe_assign_promo_group_by_total_spent,
            )

            await maybe_assign_promo_group_by_total_spent(db, user_id)
        except Exception as exc:
            logger.warning('Не удалось проверить автовыдачу промогруппы для пользователя', user_id=user_id, exc=exc)
        if type == TransactionType.SUBSCRIPTION_PAYMENT and is_completed:
            try:
                from app.services.referral_contest_service import referral_contest_service

                await referral_contest_service.on_subscription_payment(
                    db,
                    user_id,
                    abs(amount_kopeks),
                )
            except Exception as exc:
                logger.debug('Не удалось записать событие конкурса для пользователя', user_id=user_id, exc=exc)

    return transaction


async def emit_transaction_side_effects(
    db: AsyncSession,
    transaction: Transaction,
    *,
    amount_kopeks: int,
    user_id: int,
    type: TransactionType,
    payment_method: PaymentMethod | None = None,
    external_id: str | None = None,
    is_completed: bool = True,
    description: str = '',
) -> None:
    """Fire side-effects that were deferred when create_transaction(commit=False) was used.

    Call this AFTER db.commit() to emit events and run promo checks.
    """
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'payment.completed' if type == TransactionType.DEPOSIT else 'transaction.created',
            {
                'transaction_id': transaction.id,
                'user_id': user_id,
                'type': type.value,
                'amount_kopeks': abs(amount_kopeks),
                'amount_rubles': abs(amount_kopeks) / 100,
                'payment_method': payment_method.value if payment_method else None,
                'external_id': external_id,
                'is_completed': is_completed,
                'description': description,
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit deferred transaction event', error=error)

    try:
        from app.services.promo_group_assignment import (
            maybe_assign_promo_group_by_total_spent,
        )

        await maybe_assign_promo_group_by_total_spent(db, user_id)
    except Exception as exc:
        logger.warning('Не удалось проверить автовыдачу промогруппы для пользователя', user_id=user_id, exc=exc)

    if type == TransactionType.SUBSCRIPTION_PAYMENT and is_completed:
        try:
            from app.services.referral_contest_service import referral_contest_service

            await referral_contest_service.on_subscription_payment(
                db,
                user_id,
                abs(amount_kopeks),
            )
        except Exception as exc:
            logger.debug('Не удалось записать событие конкурса для пользователя', user_id=user_id, exc=exc)


async def get_transaction_by_id(db: AsyncSession, transaction_id: int) -> Transaction | None:
    result = await db.execute(
        select(Transaction).options(selectinload(Transaction.user)).where(Transaction.id == transaction_id)
    )
    return result.scalar_one_or_none()


async def get_transaction_by_external_id(
    db: AsyncSession, external_id: str, payment_method: PaymentMethod
) -> Transaction | None:
    result = await db.execute(
        select(Transaction).where(
            and_(Transaction.external_id == external_id, Transaction.payment_method == payment_method.value)
        )
    )
    return result.scalar_one_or_none()


async def get_user_transactions(db: AsyncSession, user_id: int, limit: int = 50, offset: int = 0) -> list[Transaction]:
    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()


async def get_user_transactions_count(
    db: AsyncSession, user_id: int, transaction_type: TransactionType | None = None
) -> int:
    query = select(func.count(Transaction.id)).where(Transaction.user_id == user_id)

    if transaction_type:
        query = query.where(Transaction.type == transaction_type.value)

    result = await db.execute(query)
    return result.scalar()


async def get_user_total_spent_kopeks(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.is_completed.is_(True),
                Transaction.type.in_(
                    [
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                        TransactionType.GIFT_PAYMENT.value,
                    ]
                ),
            )
        )
    )
    return int(result.scalar_one())


async def complete_transaction(db: AsyncSession, transaction: Transaction) -> Transaction:
    transaction.is_completed = True
    transaction.completed_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(transaction)

    logger.info('✅ Транзакция завершена', transaction_id=transaction.id)

    try:
        from app.services.promo_group_assignment import (
            maybe_assign_promo_group_by_total_spent,
        )

        await maybe_assign_promo_group_by_total_spent(db, transaction.user_id)
    except Exception as exc:
        logger.warning(
            'Не удалось проверить автовыдачу промогруппы для пользователя', user_id=transaction.user_id, exc=exc
        )

    return transaction


async def get_pending_transactions(db: AsyncSession) -> list[Transaction]:
    result = await db.execute(
        select(Transaction)
        .options(selectinload(Transaction.user))
        .where(Transaction.is_completed == False)
        .order_by(Transaction.created_at)
    )
    return result.scalars().all()


async def get_transactions_statistics(
    db: AsyncSession, start_date: datetime | None = None, end_date: datetime | None = None
) -> dict:
    if not start_date:
        start_date = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not end_date:
        end_date = datetime.now(UTC)

    # Доход считаем по реальным платежам + прямые покупки подписок (лендинги)
    income_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
    )
    total_income = income_result.scalar()

    expenses_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type == TransactionType.WITHDRAWAL.value,
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
            )
        )
    )
    total_expenses = expenses_result.scalar()

    subscription_income_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
            )
        )
    )
    subscription_income = subscription_income_result.scalar()

    transactions_count_result = await db.execute(
        select(
            Transaction.type,
            func.count(Transaction.id).label('count'),
            func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('total_amount'),
        )
        .where(
            and_(
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
            )
        )
        .group_by(Transaction.type)
    )
    transactions_by_type = {
        row.type: {'count': row.count, 'amount': row.total_amount} for row in transactions_count_result
    }

    payment_methods_result = await db.execute(
        select(
            Transaction.payment_method,
            func.count(Transaction.id).label('count'),
            func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('total_amount'),
        )
        .where(
            and_(
                Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
            )
        )
        .group_by(Transaction.payment_method)
    )
    payment_methods = {
        row.payment_method: {'count': row.count, 'amount': row.total_amount} for row in payment_methods_result
    }

    today = datetime.now(UTC).date()
    today_result = await db.execute(
        select(func.count(Transaction.id)).where(
            and_(Transaction.is_completed == True, Transaction.created_at >= today)
        )
    )
    transactions_today = today_result.scalar()

    # Доход за сегодня — реальные платежи + прямые покупки подписок (лендинги)
    today_income_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                Transaction.is_completed == True,
                Transaction.created_at >= today,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
    )
    income_today = today_income_result.scalar()

    return {
        'period': {'start_date': start_date, 'end_date': end_date},
        'totals': {
            'income_kopeks': total_income,
            'expenses_kopeks': total_expenses,
            'profit_kopeks': total_income - total_expenses,
            'subscription_income_kopeks': subscription_income,
        },
        'today': {'transactions_count': transactions_today, 'income_kopeks': income_today},
        'by_type': transactions_by_type,
        'by_payment_method': payment_methods,
    }


async def get_revenue_by_period(db: AsyncSession, days: int = 30) -> list[dict]:
    """Доход по дням — реальные платежи + прямые покупки подписок (лендинги)."""
    start_date = datetime.now(UTC) - timedelta(days=days)

    result = await db.execute(
        select(
            func.date(Transaction.created_at).label('date'),
            func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('amount'),
        )
        .where(
            and_(
                Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
        .group_by(func.date(Transaction.created_at))
        .order_by(func.date(Transaction.created_at))
    )

    return [{'date': row.date, 'amount_kopeks': row.amount} for row in result]


async def find_tribute_transactions_by_payment_id(
    db: AsyncSession, payment_id: str, user_telegram_id: int | None = None
) -> list[Transaction]:
    query = select(Transaction).options(selectinload(Transaction.user))

    conditions = [
        Transaction.external_id == f'donation_{payment_id}',
        Transaction.external_id == payment_id,
        Transaction.external_id.like(f'%{payment_id}%'),
    ]

    query = query.where(and_(Transaction.payment_method == PaymentMethod.TRIBUTE.value, or_(*conditions)))

    if user_telegram_id:
        from app.database.models import User

        query = query.join(User).where(User.telegram_id == user_telegram_id)

    result = await db.execute(query.order_by(Transaction.created_at.desc()))
    return result.scalars().all()


async def check_tribute_payment_duplicate(
    db: AsyncSession, payment_id: str, amount_kopeks: int, user_telegram_id: int
) -> Transaction | None:
    cutoff_time = datetime.now(UTC) - timedelta(hours=24)

    exact_external_id = f'donation_{payment_id}'

    query = (
        select(Transaction)
        .options(selectinload(Transaction.user))
        .where(
            and_(
                Transaction.payment_method == PaymentMethod.TRIBUTE.value,
                Transaction.external_id == exact_external_id,
                Transaction.amount_kopeks == amount_kopeks,
                Transaction.is_completed == True,
                Transaction.created_at >= cutoff_time,
            )
        )
        .join(User)
        .where(User.telegram_id == user_telegram_id)
    )

    result = await db.execute(query)
    transaction = result.scalar_one_or_none()

    if transaction:
        logger.info('🔍 Найден дубликат платежа в течение 24ч', transaction_id=transaction.id)

    return transaction


async def create_unique_tribute_transaction(
    db: AsyncSession, user_id: int, payment_id: str, amount_kopeks: int, description: str
) -> Transaction:
    external_id = f'donation_{payment_id}'

    existing = await get_transaction_by_external_id(db, external_id, PaymentMethod.TRIBUTE)

    if existing:
        timestamp = int(datetime.now(UTC).timestamp())
        external_id = f'donation_{payment_id}_{amount_kopeks}_{timestamp}'

        logger.info('Создан уникальный external_id для избежания дубликатов', external_id=external_id)

    return await create_transaction(
        db=db,
        user_id=user_id,
        type=TransactionType.DEPOSIT,
        amount_kopeks=amount_kopeks,
        description=description,
        payment_method=PaymentMethod.TRIBUTE,
        external_id=external_id,
        is_completed=True,
    )
