from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SavedPaymentMethod


logger = structlog.get_logger(__name__)


async def create_saved_payment_method(
    db: AsyncSession,
    user_id: int,
    yookassa_payment_method_id: str,
    method_type: str = 'bank_card',
    card_first6: str | None = None,
    card_last4: str | None = None,
    card_type: str | None = None,
    card_expiry_month: str | None = None,
    card_expiry_year: str | None = None,
    title: str | None = None,
) -> SavedPaymentMethod | None:
    """Создаёт или реактивирует сохранённый метод оплаты."""

    # Проверяем, есть ли уже такой метод (включая деактивированные)
    existing = await get_payment_method_by_yookassa_id(db, yookassa_payment_method_id, include_inactive=True)
    if existing:
        # Реактивируем и обновляем данные
        existing.is_active = True
        existing.method_type = method_type
        existing.card_first6 = card_first6
        existing.card_last4 = card_last4
        existing.card_type = card_type
        existing.card_expiry_month = card_expiry_month
        existing.card_expiry_year = card_expiry_year
        existing.title = title
        existing.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(existing)
        logger.info(
            'Реактивирован сохранённый метод оплаты',
            saved_method_id=existing.id,
            user_id=user_id,
            method_type=method_type,
            card_last4=card_last4,
        )
        return existing

    method = SavedPaymentMethod(
        user_id=user_id,
        yookassa_payment_method_id=yookassa_payment_method_id,
        method_type=method_type,
        card_first6=card_first6,
        card_last4=card_last4,
        card_type=card_type,
        card_expiry_month=card_expiry_month,
        card_expiry_year=card_expiry_year,
        title=title,
    )

    db.add(method)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.error(
            'Ошибка создания сохранённого метода оплаты',
            yookassa_payment_method_id=yookassa_payment_method_id,
            user_id=user_id,
            e=e,
        )
        return None
    await db.refresh(method)

    logger.info(
        'Создан сохранённый метод оплаты',
        saved_method_id=method.id,
        user_id=user_id,
        method_type=method_type,
        card_last4=card_last4,
    )
    return method


async def get_active_payment_methods_by_user(
    db: AsyncSession,
    user_id: int,
) -> list[SavedPaymentMethod]:
    """Получить все активные сохранённые методы оплаты пользователя."""
    result = await db.execute(
        select(SavedPaymentMethod)
        .where(
            SavedPaymentMethod.user_id == user_id,
            SavedPaymentMethod.is_active == True,
        )
        .order_by(SavedPaymentMethod.created_at.desc())
    )
    return list(result.scalars().all())


async def get_payment_method_by_yookassa_id(
    db: AsyncSession,
    yookassa_payment_method_id: str,
    include_inactive: bool = False,
) -> SavedPaymentMethod | None:
    """Найти сохранённый метод по YooKassa payment_method.id."""
    query = select(SavedPaymentMethod).where(
        SavedPaymentMethod.yookassa_payment_method_id == yookassa_payment_method_id,
    )
    if not include_inactive:
        query = query.where(SavedPaymentMethod.is_active == True)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def deactivate_payment_method(
    db: AsyncSession,
    saved_method_id: int,
    user_id: int,
) -> bool:
    """Деактивировать (soft-delete) сохранённый метод оплаты."""
    result = await db.execute(
        update(SavedPaymentMethod)
        .where(
            SavedPaymentMethod.id == saved_method_id,
            SavedPaymentMethod.user_id == user_id,
            SavedPaymentMethod.is_active == True,
        )
        .values(is_active=False, updated_at=datetime.now(UTC))
    )
    await db.commit()

    if result.rowcount > 0:
        logger.info(
            'Метод оплаты деактивирован',
            saved_method_id=saved_method_id,
            user_id=user_id,
        )
        return True
    return False


async def deactivate_all_user_payment_methods(
    db: AsyncSession,
    user_id: int,
) -> int:
    """Деактивировать все методы оплаты пользователя. Возвращает количество деактивированных."""
    result = await db.execute(
        update(SavedPaymentMethod)
        .where(
            SavedPaymentMethod.user_id == user_id,
            SavedPaymentMethod.is_active == True,
        )
        .values(is_active=False, updated_at=datetime.now(UTC))
    )
    await db.commit()

    if result.rowcount > 0:
        logger.info(
            'Все методы оплаты пользователя деактивированы',
            user_id=user_id,
            count=result.rowcount,
        )
    return result.rowcount
