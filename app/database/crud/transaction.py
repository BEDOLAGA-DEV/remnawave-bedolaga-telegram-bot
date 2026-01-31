import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import PaymentMethod, Transaction, TransactionType, User


logger = logging.getLogger(__name__)

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
) -> Transaction:
    transaction = Transaction(
        user_id=user_id,
        type=type.value,
        amount_kopeks=amount_kopeks,
        description=description,
        payment_method=payment_method.value if payment_method else None,
        external_id=external_id,
        is_completed=is_completed,
        completed_at=datetime.utcnow() if is_completed else None,
    )

    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)

    logger.info(f'💳 Создана транзакция: {type.value} на {amount_kopeks / 100}₽ для пользователя {user_id}')

    # Отправляем событие о транзакции
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'payment.completed' if type == TransactionType.DEPOSIT else 'transaction.created',
            {
                'transaction_id': transaction.id,
                'user_id': user_id,
                'type': type.value,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_kopeks / 100,
                'payment_method': payment_method.value if payment_method else None,
                'external_id': external_id,
                'is_completed': is_completed,
                'description': description,
            },
            db=db,
        )
    except Exception as error:
        logger.warning('Failed to emit transaction event: %s', error)

    try:
        from app.services.promo_group_assignment import (
            maybe_assign_promo_group_by_total_spent,
        )

        await maybe_assign_promo_group_by_total_spent(db, user_id)
    except Exception as exc:
        logger.debug(
            'Не удалось проверить автовыдачу промогруппы для пользователя %s: %s',
            user_id,
            exc,
        )
    if type == TransactionType.SUBSCRIPTION_PAYMENT:
        try:
            from app.services.referral_contest_service import referral_contest_service

            await referral_contest_service.on_subscription_payment(
                db,
                user_id,
                amount_kopeks,
            )
        except Exception as exc:
            logger.debug(
                'Не удалось записать событие конкурса для пользователя %s: %s',
                user_id,
                exc,
            )

    return transaction


async def create_transaction_idempotent(
    db: AsyncSession,
    user_id: int,
    type: TransactionType,
    amount_kopeks: int,
    description: str,
    payment_method: PaymentMethod,
    external_id: str,
    is_completed: bool = True,
) -> tuple[Transaction, bool]:
    """Создаёт транзакцию идемпотентно — защита от дублирования при race condition webhooks.

    Если транзакция с таким external_id и payment_method уже существует,
    возвращает существующую транзакцию вместо создания дубликата.

    Returns:
        tuple[Transaction, bool]: (транзакция, created) — created=True если создана новая, False если вернули существующую
    """
    # Сначала проверим, нет ли уже такой транзакции
    existing = await get_transaction_by_external_id(db, external_id, payment_method)
    if existing:
        logger.info(
            '⚠️ Транзакция с external_id=%s payment_method=%s уже существует (id=%s), пропускаем дубликат',
            external_id,
            payment_method.value,
            existing.id,
        )
        return existing, False

    try:
        transaction = await create_transaction(
            db=db,
            user_id=user_id,
            type=type,
            amount_kopeks=amount_kopeks,
            description=description,
            payment_method=payment_method,
            external_id=external_id,
            is_completed=is_completed,
        )
        return transaction, True
    except IntegrityError as e:
        # Race condition — между проверкой и созданием другой webhook успел создать транзакцию
        await db.rollback()
        logger.warning(
            '⚠️ Race condition при создании транзакции external_id=%s: %s',
            external_id,
            e,
        )
        # Возвращаем существующую транзакцию
        existing = await get_transaction_by_external_id(db, external_id, payment_method)
        if existing:
            return existing, False
        # Если транзакции всё ещё нет — IntegrityError был по другой причине
        logger.error(
            '❌ IntegrityError при создании транзакции, но дубликат не найден. external_id=%s, payment_method=%s',
            external_id,
            payment_method.value,
        )
        raise ValueError(
            f'Не удалось создать транзакцию: IntegrityError, но дубликат не найден (external_id={external_id})'
        ) from e


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
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            )
        )
    )
    return int(result.scalar_one())


async def complete_transaction(db: AsyncSession, transaction: Transaction) -> Transaction:
    transaction.is_completed = True
    transaction.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(transaction)

    logger.info(f'✅ Транзакция {transaction.id} завершена')

    try:
        from app.services.promo_group_assignment import (
            maybe_assign_promo_group_by_total_spent,
        )

        await maybe_assign_promo_group_by_total_spent(db, transaction.user_id)
    except Exception as exc:
        logger.debug(
            'Не удалось проверить автовыдачу промогруппы для пользователя %s: %s',
            transaction.user_id,
            exc,
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
        start_date = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if not end_date:
        end_date = datetime.utcnow()

    # Доход считаем только по реальным платежам (исключаем колесо, промокоды, админские пополнения)
    income_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed == True,
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
    )
    total_income = income_result.scalar()

    expenses_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
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
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
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
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_amount'),
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
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_amount'),
        )
        .where(
            and_(
                Transaction.type == TransactionType.DEPOSIT.value,
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

    today = datetime.utcnow().date()
    today_result = await db.execute(
        select(func.count(Transaction.id)).where(
            and_(Transaction.is_completed == True, Transaction.created_at >= today)
        )
    )
    transactions_today = today_result.scalar()

    # Доход за сегодня - только реальные платежи
    today_income_result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            and_(
                Transaction.type == TransactionType.DEPOSIT.value,
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
    """Доход по дням - только реальные платежи."""
    start_date = datetime.utcnow() - timedelta(days=days)

    result = await db.execute(
        select(
            func.date(Transaction.created_at).label('date'),
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('amount'),
        )
        .where(
            and_(
                Transaction.type == TransactionType.DEPOSIT.value,
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
    cutoff_time = datetime.utcnow() - timedelta(hours=24)

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
        logger.info(f'🔍 Найден дубликат платежа в течение 24ч: {transaction.id}')

    return transaction


async def create_unique_tribute_transaction(
    db: AsyncSession, user_id: int, payment_id: str, amount_kopeks: int, description: str
) -> Transaction:
    external_id = f'donation_{payment_id}'

    existing = await get_transaction_by_external_id(db, external_id, PaymentMethod.TRIBUTE)

    if existing:
        timestamp = int(datetime.utcnow().timestamp())
        external_id = f'donation_{payment_id}_{amount_kopeks}_{timestamp}'

        logger.info(f'Создан уникальный external_id для избежания дубликатов: {external_id}')

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
