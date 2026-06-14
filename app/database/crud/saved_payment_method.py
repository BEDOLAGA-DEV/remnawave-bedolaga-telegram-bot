from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, select, update
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
    yookassa_scope: str | None = None,
) -> SavedPaymentMethod | None:
    """Создаёт или реактивирует сохранённый метод оплаты."""

    # Проверяем, есть ли уже такой метод (включая деактивированные)
    conditions = [
        SavedPaymentMethod.yookassa_payment_method_id == yookassa_payment_method_id,
        SavedPaymentMethod.user_id == user_id,
    ]
    if yookassa_scope is not None:
        conditions.append(SavedPaymentMethod.yookassa_scope == yookassa_scope)

    update_values = {
        'is_active': True,
        'method_type': method_type,
        'card_first6': card_first6,
        'card_last4': card_last4,
        'card_type': card_type,
        'card_expiry_month': card_expiry_month,
        'card_expiry_year': card_expiry_year,
        'title': title,
        'updated_at': datetime.now(UTC),
    }
    if yookassa_scope is not None:
        update_values['yookassa_scope'] = yookassa_scope

    result = await db.execute(
        update(SavedPaymentMethod)
        .where(and_(*conditions))
        .values(**update_values)
        .returning(SavedPaymentMethod)
    )
    reactivated = result.scalar_one_or_none()
    if reactivated:
        await db.commit()
        logger.info(
            'Реактивирован сохранённый метод оплаты',
            saved_method_id=reactivated.id,
            user_id=user_id,
            yookassa_scope=yookassa_scope,
            method_type=method_type,
            card_last4=card_last4,
        )
        return reactivated

    method = SavedPaymentMethod(
        user_id=user_id,
        yookassa_scope=yookassa_scope,
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
            yookassa_scope=yookassa_scope,
            user_id=user_id,
            e=e,
        )
        return None
    await db.refresh(method)

    logger.info(
        'Создан сохранённый метод оплаты',
        saved_method_id=method.id,
        user_id=user_id,
        yookassa_scope=yookassa_scope,
        method_type=method_type,
        card_last4=card_last4,
    )
    return method


async def get_active_payment_methods_by_user(
    db: AsyncSession,
    user_id: int,
    yookassa_scope: str | None = None,
) -> list[SavedPaymentMethod]:
    """Получить все активные сохранённые методы оплаты пользователя."""
    conditions = [
        SavedPaymentMethod.user_id == user_id,
        SavedPaymentMethod.is_active == True,
    ]
    if yookassa_scope is not None:
        conditions.append(SavedPaymentMethod.yookassa_scope == yookassa_scope)

    result = await db.execute(
        select(SavedPaymentMethod)
        .where(and_(*conditions))
        .order_by(SavedPaymentMethod.created_at.desc())
    )
    return list(result.scalars().all())


async def get_user_ids_with_active_payment_methods(
    db: AsyncSession,
    user_ids: list[int],
    yookassa_scope: str | None = None,
) -> set[int]:
    """Вернуть подмножество user_ids, у которых есть хотя бы один активный метод оплаты."""
    if not user_ids:
        return set()
    conditions = [
        SavedPaymentMethod.user_id.in_(user_ids),
        SavedPaymentMethod.is_active == True,
    ]
    if yookassa_scope is not None:
        conditions.append(SavedPaymentMethod.yookassa_scope == yookassa_scope)

    result = await db.execute(
        select(SavedPaymentMethod.user_id)
        .where(and_(*conditions))
        .distinct()
    )
    return set(result.scalars().all())


async def get_payment_method_by_yookassa_id(
    db: AsyncSession,
    yookassa_payment_method_id: str,
    include_inactive: bool = False,
    yookassa_scope: str | None = None,
) -> SavedPaymentMethod | None:
    """Найти сохранённый метод по YooKassa payment_method.id."""
    query = select(SavedPaymentMethod).where(
        SavedPaymentMethod.yookassa_payment_method_id == yookassa_payment_method_id,
    )
    if yookassa_scope is not None:
        query = query.where(SavedPaymentMethod.yookassa_scope == yookassa_scope)
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
