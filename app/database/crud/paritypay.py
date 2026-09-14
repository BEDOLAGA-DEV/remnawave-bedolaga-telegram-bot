"""CRUD операции для платежей ParityPay (api.paritypay.net v2)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ParityPayPayment


logger = structlog.get_logger(__name__)


async def create_paritypay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    paritypay_payment_id: str | None = None,
    credited_kopeks: int | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> ParityPayPayment:
    """Создаёт запись о платеже ParityPay."""
    payment = ParityPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        paritypay_payment_id=paritypay_payment_id,
        credited_kopeks=credited_kopeks,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж ParityPay', order_id=order_id, user_id=user_id)
    return payment


async def get_paritypay_payment_by_order_id(db: AsyncSession, order_id: str) -> ParityPayPayment | None:
    """Получает платеж по order_id (наш)."""
    result = await db.execute(select(ParityPayPayment).where(ParityPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_paritypay_payment_by_invoice_id(db: AsyncSession, paritypay_payment_id: str) -> ParityPayPayment | None:
    """Получает платёж по идентификатору, выданному ParityPay."""
    result = await db.execute(
        select(ParityPayPayment).where(ParityPayPayment.paritypay_payment_id == paritypay_payment_id)
    )
    return result.scalar_one_or_none()


async def get_paritypay_payment_by_id(db: AsyncSession, payment_id: int) -> ParityPayPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(ParityPayPayment).where(ParityPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_paritypay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> ParityPayPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(ParityPayPayment)
        .where(ParityPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_paritypay_payment_status(
    db: AsyncSession,
    payment: ParityPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    paritypay_payment_id: str | None = None,
    payment_method: str | None = None,
    credited_kopeks: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> ParityPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if paritypay_payment_id is not None:
        payment.paritypay_payment_id = paritypay_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if credited_kopeks is not None:
        payment.credited_kopeks = credited_kopeks
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа ParityPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


def is_paritypay_event_processed(payment: ParityPayPayment, event_key: str) -> bool:
    """Обрабатывалась ли уже пара (id, status) из вебхука.

    ParityPay повторяет доставку до 7 раз одним и тем же телом, а поздняя оплата
    присылает SUCCESS после EXPIRED/FAILED. Поэтому «уже обработано» считается
    по конкретному событию, а не по факту оплаты платежа.
    """
    return event_key in (payment.processed_events or [])


def remember_paritypay_event(payment: ParityPayPayment, event_key: str) -> None:
    """Помечает пару (id, status) обработанной.

    Список пересобирается, а не мутируется на месте: SQLAlchemy отслеживает
    изменения JSON-колонки по присваиванию, иначе апдейт не попал бы в UPDATE.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_paritypay_payments(db: AsyncSession, user_id: int) -> list[ParityPayPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(ParityPayPayment).where(
            ParityPayPayment.user_id == user_id,
            ParityPayPayment.status == 'pending',
            ParityPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_paritypay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: ParityPayPayment,
    transaction_id: int,
) -> ParityPayPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
