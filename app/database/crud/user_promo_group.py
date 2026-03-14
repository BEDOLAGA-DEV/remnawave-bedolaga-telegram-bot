"""CRUD операции для связи пользователей с промогруппами (Many-to-Many)."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import PromoGroup, User, UserPromoGroup


logger = structlog.get_logger(__name__)


async def _sync_user_primary_promo_group(
    db: AsyncSession,
    user_id: int,
) -> None:
    """Синхронизирует колонку users.promo_group_id с primary промогруппой."""

    try:
        result = await db.execute(
            select(UserPromoGroup.promo_group_id)
            .join(PromoGroup, UserPromoGroup.promo_group_id == PromoGroup.id)
            .where(UserPromoGroup.user_id == user_id)
            .order_by(desc(PromoGroup.priority), PromoGroup.id)
        )

        first = result.first()
        new_primary_id = first[0] if first else None

        user = await db.get(User, user_id)
        if not user:
            return

        if user.promo_group_id != new_primary_id:
            user.promo_group_id = new_primary_id
            user.updated_at = datetime.now(UTC)

    except Exception as error:
        logger.error('Ошибка синхронизации primary промогруппы пользователя', user_id=user_id, error=error)
        raise


async def sync_user_primary_promo_group(
    db: AsyncSession,
    user_id: int,
) -> None:
    """Публичная обертка для синхронизации primary промогруппы пользователя."""

    await _sync_user_primary_promo_group(db, user_id)


async def add_user_to_promo_group(
    db: AsyncSession, user_id: int, promo_group_id: int, assigned_by: str = 'admin'
) -> UserPromoGroup | None:
    """
    Добавляет пользователю промогруппу.

    Args:
        db: Сессия БД
        user_id: ID пользователя
        promo_group_id: ID промогруппы
        assigned_by: Кто назначил ('admin', 'system', 'auto', 'promocode')

    Returns:
        UserPromoGroup или None если уже существует
    """
    try:
        # Проверяем существование связи
        existing = await has_user_promo_group(db, user_id, promo_group_id)
        if existing:
            logger.info('Пользователь уже имеет промогруппу', user_id=user_id, promo_group_id=promo_group_id)
            return None

        # Создаем новую связь
        user_promo_group = UserPromoGroup(
            user_id=user_id,
            promo_group_id=promo_group_id,
            assigned_by=assigned_by,
        )
        db.add(user_promo_group)
        await db.flush()

        await _sync_user_primary_promo_group(db, user_id)

        await db.commit()
        await db.refresh(user_promo_group)

        logger.info(
            'Пользователю добавлена промогруппа',
            user_id=user_id,
            promo_group_id=promo_group_id,
            assigned_by=assigned_by,
        )
        return user_promo_group

    except Exception as error:
        logger.error('Ошибка добавления промогруппы пользователю', error=error)
        await db.rollback()
        return None


async def remove_user_from_promo_group(db: AsyncSession, user_id: int, promo_group_id: int) -> bool:
    """
    Удаляет промогруппу у пользователя.

    Args:
        db: Сессия БД
        user_id: ID пользователя
        promo_group_id: ID промогруппы

    Returns:
        True если удалено, False если связи не было
    """
    try:
        result = await db.execute(
            select(UserPromoGroup).where(
                and_(UserPromoGroup.user_id == user_id, UserPromoGroup.promo_group_id == promo_group_id)
            )
        )
        user_promo_group = result.scalar_one_or_none()

        if not user_promo_group:
            logger.warning(
                'Связь пользователя с промогруппой не найдена', user_id=user_id, promo_group_id=promo_group_id
            )
            return False

        await db.delete(user_promo_group)
        await db.flush()

        await _sync_user_primary_promo_group(db, user_id)

        await db.commit()

        logger.info('У пользователя удалена промогруппа', user_id=user_id, promo_group_id=promo_group_id)
        return True

    except Exception as error:
        logger.error('Ошибка удаления промогруппы у пользователя', error=error)
        await db.rollback()
        return False


async def get_user_promo_groups(db: AsyncSession, user_id: int) -> list[UserPromoGroup]:
    """
    Получает все промогруппы пользователя, отсортированные по приоритету.

    Args:
        db: Сессия БД
        user_id: ID пользователя

    Returns:
        Список UserPromoGroup с загруженными PromoGroup, отсортированный по приоритету DESC
    """
    try:
        result = await db.execute(
            select(UserPromoGroup)
            .options(selectinload(UserPromoGroup.promo_group))
            .where(UserPromoGroup.user_id == user_id)
            .join(PromoGroup, UserPromoGroup.promo_group_id == PromoGroup.id)
            .order_by(desc(PromoGroup.priority), PromoGroup.id)
        )
        return list(result.scalars().all())

    except Exception as error:
        logger.error('Ошибка получения промогрупп пользователя', user_id=user_id, error=error)
        return []


async def get_primary_user_promo_group(db: AsyncSession, user_id: int) -> PromoGroup | None:
    """
    Получает промогруппу пользователя с максимальным приоритетом.

    Args:
        db: Сессия БД
        user_id: ID пользователя

    Returns:
        PromoGroup с максимальным приоритетом или None
    """
    try:
        user_promo_groups = await get_user_promo_groups(db, user_id)

        if not user_promo_groups:
            return None

        # Первая в списке имеет максимальный приоритет (список уже отсортирован)
        return user_promo_groups[0].promo_group or None

    except Exception as error:
        logger.error('Ошибка получения primary промогруппы пользователя', user_id=user_id, error=error)
        return None


async def has_user_promo_group(db: AsyncSession, user_id: int, promo_group_id: int) -> bool:
    """
    Проверяет наличие промогруппы у пользователя.

    Args:
        db: Сессия БД
        user_id: ID пользователя
        promo_group_id: ID промогруппы

    Returns:
        True если пользователь уже имеет эту промогруппу
    """
    try:
        result = await db.execute(
            select(UserPromoGroup).where(
                and_(UserPromoGroup.user_id == user_id, UserPromoGroup.promo_group_id == promo_group_id)
            )
        )
        return result.scalar_one_or_none() is not None

    except Exception as error:
        logger.error('Ошибка проверки промогруппы пользователя', error=error)
        return False


async def count_user_promo_groups(db: AsyncSession, user_id: int) -> int:
    """
    Подсчитывает количество промогрупп у пользователя.

    Args:
        db: Сессия БД
        user_id: ID пользователя

    Returns:
        Количество промогрупп
    """
    try:
        result = await db.execute(select(UserPromoGroup).where(UserPromoGroup.user_id == user_id))
        return len(list(result.scalars().all()))

    except Exception as error:
        logger.error('Ошибка подсчета промогрупп пользователя', error=error)
        return 0


async def replace_user_promo_groups(
    db: AsyncSession, user_id: int, promo_group_ids: list[int], assigned_by: str = 'admin'
) -> bool:
    """
    Заменяет все промогруппы пользователя на новый список.

    Args:
        db: Сессия БД
        user_id: ID пользователя
        promo_group_ids: Список ID промогрупп
        assigned_by: Кто назначил

    Returns:
        True если успешно
    """
    try:
        # Удаляем все текущие промогруппы
        await db.execute(select(UserPromoGroup).where(UserPromoGroup.user_id == user_id))
        result = await db.execute(select(UserPromoGroup).where(UserPromoGroup.user_id == user_id))
        for upg in result.scalars().all():
            await db.delete(upg)

        # Добавляем новые
        for promo_group_id in promo_group_ids:
            user_promo_group = UserPromoGroup(user_id=user_id, promo_group_id=promo_group_id, assigned_by=assigned_by)
            db.add(user_promo_group)

        await db.commit()
        logger.info('Промогруппы пользователя заменены на', user_id=user_id, promo_group_ids=promo_group_ids)
        return True

    except Exception as error:
        logger.error('Ошибка замены промогрупп пользователя', error=error)
        await db.rollback()
        return False
