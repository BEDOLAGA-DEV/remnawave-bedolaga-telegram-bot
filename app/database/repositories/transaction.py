"""
Репозиторий транзакций (финансовых операций).

Не делает commit - это ответственность UnitOfWork.
Side effects (события, промо-группы) - на уровне сервиса.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select

from app.database.models import PaymentMethod, Transaction, TransactionType
from app.database.repositories.base import BaseRepository


logger = logging.getLogger(__name__)


# Реальные платёжные методы (исключая MANUAL, BALANCE, NULL)
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


class TransactionRepository(BaseRepository[Transaction]):
    """Репозиторий для работы с транзакциями."""

    model_class = Transaction

    async def create_transaction(
        self,
        user_id: int,
        transaction_type: TransactionType,
        amount_kopeks: int,
        description: str,
        payment_method: PaymentMethod | None = None,
        external_id: str | None = None,
        is_completed: bool = True,
    ) -> Transaction:
        """
        Создать транзакцию.

        НЕ делает commit.
        НЕ эмитит события - это на уровне сервиса.
        """
        transaction = Transaction(
            user_id=user_id,
            type=transaction_type.value,
            amount_kopeks=amount_kopeks,
            description=description,
            payment_method=payment_method.value if payment_method else None,
            external_id=external_id,
            is_completed=is_completed,
            completed_at=datetime.utcnow() if is_completed else None,
        )

        self.add(transaction)
        await self._session.flush()
        await self._session.refresh(transaction)

        logger.info(
            'Создана транзакция %s: %s на %s коп. для пользователя %s',
            transaction.id,
            transaction_type.value,
            amount_kopeks,
            user_id,
        )

        return transaction

    async def get_by_external_id(self, external_id: str) -> Transaction | None:
        """Получить транзакцию по внешнему ID."""
        result = await self._session.execute(
            select(Transaction).where(Transaction.external_id == external_id)
        )
        return result.scalar_one_or_none()

    async def get_user_transactions(
        self,
        user_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
        transaction_type: TransactionType | None = None,
    ) -> list[Transaction]:
        """Получить транзакции пользователя."""
        query = select(Transaction).where(Transaction.user_id == user_id)

        if transaction_type:
            query = query.where(Transaction.type == transaction_type.value)

        query = query.order_by(Transaction.created_at.desc()).offset(offset).limit(limit)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_user_total_spent(self, user_id: int) -> int:
        """
        Получить общую сумму трат пользователя (в копейках).

        Считает только SUBSCRIPTION_PAYMENT с реальными платёжными методами.
        """
        result = await self._session.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
        )
        return result.scalar_one()

    async def get_user_purchase_count(self, user_id: int) -> int:
        """Количество покупок пользователя."""
        result = await self._session.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                )
            )
        )
        return result.scalar_one()

    async def get_revenue_for_period(
        self,
        days: int = 30,
        payment_methods: list[str] | None = None,
    ) -> int:
        """
        Доход за период (в копейках).

        Args:
            days: Количество дней
            payment_methods: Список методов оплаты (по умолчанию - все реальные)
        """
        if payment_methods is None:
            payment_methods = REAL_PAYMENT_METHODS

        since = datetime.utcnow() - timedelta(days=days)

        result = await self._session.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0))
            .where(
                and_(
                    Transaction.type.in_([
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    ]),
                    Transaction.is_completed == True,
                    Transaction.payment_method.in_(payment_methods),
                    Transaction.created_at >= since,
                )
            )
        )
        return result.scalar_one()

    async def mark_completed(self, transaction: Transaction) -> Transaction:
        """Отметить транзакцию как завершённую."""
        transaction.is_completed = True
        transaction.completed_at = datetime.utcnow()
        await self._session.flush()
        return transaction
