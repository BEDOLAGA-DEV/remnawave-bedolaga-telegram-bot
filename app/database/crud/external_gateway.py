"""CRUD операции для платежей через внешний шлюз."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ExternalGatewayPayment


logger = structlog.get_logger(__name__)


async def create_external_gateway_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    redirect_url: str | None = None,
    gateway_order_id: str | None = None,
    metadata_json: dict | None = None,
) -> ExternalGatewayPayment:
    """Создает запись о платеже через внешний шлюз."""
    payment = ExternalGatewayPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        redirect_url=redirect_url,
        gateway_order_id=str(gateway_order_id) if gateway_order_id is not None else None,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж External Gateway', order_id=order_id, user_id=user_id)
    return payment


async def get_external_gateway_payment_by_order_id(
    db: AsyncSession, order_id: str
) -> ExternalGatewayPayment | None:
    """Получает платеж по order_id (наш ID)."""
    result = await db.execute(
        select(ExternalGatewayPayment).where(ExternalGatewayPayment.order_id == order_id)
    )
    return result.scalar_one_or_none()


async def get_external_gateway_payment_by_id(
    db: AsyncSession, payment_id: int
) -> ExternalGatewayPayment | None:
    """Получает платеж по внутреннему ID."""
    result = await db.execute(
        select(ExternalGatewayPayment).where(ExternalGatewayPayment.id == payment_id)
    )
    return result.scalar_one_or_none()


async def get_external_gateway_payment_by_id_for_update(
    db: AsyncSession, payment_id: int
) -> ExternalGatewayPayment | None:
    """Получает платеж с блокировкой для обновления."""
    result = await db.execute(
        select(ExternalGatewayPayment)
        .where(ExternalGatewayPayment.id == payment_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def update_external_gateway_payment_status(
    db: AsyncSession,
    payment: ExternalGatewayPayment,
    *,
    status: str,
    is_paid: bool = False,
    gateway_order_id: str | None = None,
    gateway_payment_id: str | None = None,
    payment_method_name: str | None = None,
    amount_converted: float | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> ExternalGatewayPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.is_paid = is_paid
    payment.updated_at = datetime.now(UTC)

    if is_paid:
        payment.paid_at = datetime.now(UTC)
    if gateway_order_id is not None:
        payment.gateway_order_id = str(gateway_order_id)
    if gateway_payment_id is not None:
        payment.gateway_payment_id = gateway_payment_id
    if payment_method_name is not None:
        payment.payment_method_name = payment_method_name
    if amount_converted is not None:
        payment.amount_converted = amount_converted
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа External Gateway',
        order_id=payment.order_id,
        status=status,
        is_paid=is_paid,
    )
    return payment


async def get_pending_external_gateway_payments(
    db: AsyncSession, user_id: int
) -> list[ExternalGatewayPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(ExternalGatewayPayment).where(
            ExternalGatewayPayment.user_id == user_id,
            ExternalGatewayPayment.status == 'pending',
            ExternalGatewayPayment.is_paid == False,  # noqa: E712
        )
    )
    return list(result.scalars().all())
