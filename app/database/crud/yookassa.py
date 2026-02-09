import logging
from datetime import datetime

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import YooKassaPayment


logger = logging.getLogger(__name__)


async def create_yookassa_payment(
    db: AsyncSession,
    user_id: int,
    yookassa_payment_id: str,
    amount_kopeks: int,
    currency: str,
    description: str,
    status: str,
    confirmation_url: str | None = None,
    metadata_json: dict | None = None,
    payment_method_type: str | None = None,
    yookassa_created_at: datetime | None = None,
    test_mode: bool = False,
) -> YooKassaPayment | None:
    payment = YooKassaPayment(
        user_id=user_id,
        yookassa_payment_id=yookassa_payment_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        status=status,
        confirmation_url=confirmation_url,
        metadata_json=metadata_json,
        payment_method_type=payment_method_type,
        yookassa_created_at=yookassa_created_at,
        test_mode=test_mode,
    )

    db.add(payment)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.error(
            'FK violation при создании платежа YooKassa %s: user_id=%s не существует в БД: %s',
            yookassa_payment_id,
            user_id,
            e,
        )
        return None
    await db.refresh(payment)

    logger.info(f'Создан платеж YooKassa: {yookassa_payment_id} на {amount_kopeks / 100}₽ для пользователя {user_id}')
    return payment


async def get_yookassa_payment_by_id(db: AsyncSession, yookassa_payment_id: str) -> YooKassaPayment | None:
    result = await db.execute(
        select(YooKassaPayment)
        .options(selectinload(YooKassaPayment.user))
        .where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id)
    )
    return result.scalar_one_or_none()


async def get_yookassa_payment_by_local_id(db: AsyncSession, local_id: int) -> YooKassaPayment | None:
    result = await db.execute(
        select(YooKassaPayment).options(selectinload(YooKassaPayment.user)).where(YooKassaPayment.id == local_id)
    )
    return result.scalar_one_or_none()


async def update_yookassa_payment_status(
    db: AsyncSession,
    yookassa_payment_id: str,
    status: str,
    is_paid: bool = False,
    is_captured: bool = False,
    captured_at: datetime | None = None,
    payment_method_type: str | None = None,
) -> YooKassaPayment | None:
    update_data = {'status': status, 'is_paid': is_paid, 'is_captured': is_captured, 'updated_at': datetime.utcnow()}

    if captured_at:
        update_data['captured_at'] = captured_at

    if payment_method_type:
        update_data['payment_method_type'] = payment_method_type

    await db.execute(
        update(YooKassaPayment).where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id).values(**update_data)
    )
    await db.commit()

    result = await db.execute(
        select(YooKassaPayment)
        .options(selectinload(YooKassaPayment.user))
        .where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id)
    )
    payment = result.scalar_one_or_none()

    if payment:
        logger.info(f'Обновлен статус платежа YooKassa {yookassa_payment_id}: {status}, paid={is_paid}')

    return payment


async def link_yookassa_payment_to_transaction(
    db: AsyncSession, yookassa_payment_id: str, transaction_id: int
) -> YooKassaPayment | None:
    await db.execute(
        update(YooKassaPayment)
        .where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id)
        .values(transaction_id=transaction_id, updated_at=datetime.utcnow())
    )
    await db.commit()

    result = await db.execute(
        select(YooKassaPayment)
        .options(selectinload(YooKassaPayment.user), selectinload(YooKassaPayment.transaction))
        .where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id)
    )
    payment = result.scalar_one_or_none()

    if payment:
        logger.info(f'Платеж YooKassa {yookassa_payment_id} связан с транзакцией {transaction_id}')

    return payment


async def get_user_yookassa_payments(
    db: AsyncSession, user_id: int, limit: int = 50, offset: int = 0
) -> list[YooKassaPayment]:
    result = await db.execute(
        select(YooKassaPayment)
        .options(selectinload(YooKassaPayment.transaction))
        .where(YooKassaPayment.user_id == user_id)
        .order_by(YooKassaPayment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def get_pending_yookassa_payments(
    db: AsyncSession, user_id: int | None = None, limit: int = 100
) -> list[YooKassaPayment]:
    query = select(YooKassaPayment).options(selectinload(YooKassaPayment.user))

    conditions = [YooKassaPayment.status.in_(['pending', 'waiting_for_capture'])]
    if user_id:
        conditions.append(YooKassaPayment.user_id == user_id)

    result = await db.execute(query.where(and_(*conditions)).order_by(YooKassaPayment.created_at.desc()).limit(limit))
    return result.scalars().all()


async def get_succeeded_yookassa_payments_without_transaction(
    db: AsyncSession, limit: int = 50
) -> list[YooKassaPayment]:
    result = await db.execute(
        select(YooKassaPayment)
        .options(selectinload(YooKassaPayment.user))
        .where(
            and_(
                YooKassaPayment.status == 'succeeded',
                YooKassaPayment.is_paid == True,
                YooKassaPayment.transaction_id == None,
            )
        )
        .order_by(YooKassaPayment.captured_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def delete_yookassa_payment(db: AsyncSession, yookassa_payment_id: str) -> bool:
    result = await db.execute(select(YooKassaPayment).where(YooKassaPayment.yookassa_payment_id == yookassa_payment_id))
    payment = result.scalar_one_or_none()

    if payment:
        await db.delete(payment)
        await db.commit()
        logger.info(f'Удален платеж YooKassa: {yookassa_payment_id}')
        return True

    return False


async def get_yookassa_payments_stats(db: AsyncSession, user_id: int | None = None) -> dict:
    from sqlalchemy import case, func

    query = select(
        func.count(YooKassaPayment.id).label('total_payments'),
        func.sum(YooKassaPayment.amount_kopeks).label('total_amount_kopeks'),
        func.sum(case((YooKassaPayment.status == 'succeeded', YooKassaPayment.amount_kopeks), else_=0)).label(
            'succeeded_amount_kopeks'
        ),
        func.count(case((YooKassaPayment.status == 'succeeded', 1), else_=None)).label('succeeded_count'),
        func.count(case((YooKassaPayment.status == 'pending', 1), else_=None)).label('pending_count'),
        func.count(case((YooKassaPayment.status.in_(['canceled', 'failed']), 1), else_=None)).label('failed_count'),
    ).select_from(YooKassaPayment)

    if user_id:
        query = query.where(YooKassaPayment.user_id == user_id)

    result = await db.execute(query)
    stats = result.first()

    return {
        'total_payments': stats.total_payments or 0,
        'total_amount_kopeks': stats.total_amount_kopeks or 0,
        'total_amount_rubles': (stats.total_amount_kopeks or 0) / 100,
        'succeeded_amount_kopeks': stats.succeeded_amount_kopeks or 0,
        'succeeded_amount_rubles': (stats.succeeded_amount_kopeks or 0) / 100,
        'succeeded_count': stats.succeeded_count or 0,
        'pending_count': stats.pending_count or 0,
        'failed_count': stats.failed_count or 0,
        'success_rate': (stats.succeeded_count / stats.total_payments * 100) if stats.total_payments > 0 else 0,
    }
