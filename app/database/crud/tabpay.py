"""CRUD операции для платежей TabPay (tabpay.org)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TabPayPayment


logger = structlog.get_logger(__name__)


async def create_tabpay_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    tabpay_payment_id: str | None = None,
    commission_kopeks: int | None = None,
    is_test: bool = False,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> TabPayPayment:
    """Создаёт запись о платеже TabPay."""
    payment = TabPayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        tabpay_payment_id=tabpay_payment_id,
        commission_kopeks=commission_kopeks,
        is_test=is_test,
        expires_at=expires_at,
        metadata_json=metadata_json,
        processed_events=[],
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж TabPay', order_id=order_id, user_id=user_id)
    return payment


async def get_tabpay_payment_by_order_id(db: AsyncSession, order_id: str) -> TabPayPayment | None:
    """Получает платеж по order_id (наш)."""
    result = await db.execute(select(TabPayPayment).where(TabPayPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_tabpay_payment_by_invoice_id(db: AsyncSession, tabpay_payment_id: str) -> TabPayPayment | None:
    """Получает платёж по идентификатору, выданному TabPay."""
    result = await db.execute(select(TabPayPayment).where(TabPayPayment.tabpay_payment_id == tabpay_payment_id))
    return result.scalar_one_or_none()


async def get_tabpay_payment_by_id(db: AsyncSession, payment_id: int) -> TabPayPayment | None:
    """Получает платеж по локальному ID."""
    result = await db.execute(select(TabPayPayment).where(TabPayPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_tabpay_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> TabPayPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(TabPayPayment)
        .where(TabPayPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_tabpay_payment_status(
    db: AsyncSession,
    payment: TabPayPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    tabpay_payment_id: str | None = None,
    payment_method: str | None = None,
    commission_kopeks: int | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> TabPayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if tabpay_payment_id is not None:
        payment.tabpay_payment_id = tabpay_payment_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if commission_kopeks is not None:
        payment.commission_kopeks = commission_kopeks
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлён статус платежа TabPay',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


def is_tabpay_event_processed(payment: TabPayPayment, event_key: str) -> bool:
    """Обрабатывалась ли уже пара (id, status) из вебхука.

    TabPay повторяет доставку до 7 раз одним и тем же телом, а поздняя оплата
    присылает SUCCESS после EXPIRED/FAILED. Поэтому «уже обработано» считается
    по конкретному событию, а не по факту оплаты платежа.
    """
    return event_key in (payment.processed_events or [])


def remember_tabpay_event(payment: TabPayPayment, event_key: str) -> None:
    """Помечает пару (id, status) обработанной.

    Список пересобирается, а не мутируется на месте: SQLAlchemy отслеживает
    изменения JSON-колонки по присваиванию, иначе апдейт не попал бы в UPDATE.
    """
    processed = list(payment.processed_events or [])
    if event_key not in processed:
        processed.append(event_key)
    payment.processed_events = processed


async def get_pending_tabpay_payments(db: AsyncSession, user_id: int) -> list[TabPayPayment]:
    """Возвращает незавершённые платежи пользователя."""
    result = await db.execute(
        select(TabPayPayment).where(
            TabPayPayment.user_id == user_id,
            TabPayPayment.status == 'pending',
            TabPayPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def link_tabpay_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: TabPayPayment,
    transaction_id: int,
) -> TabPayPayment:
    """Связывает платёж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
