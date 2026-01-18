import logging
from datetime import datetime
from typing import Optional, List
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import PromoCode, PromoCodeUse, PromoCodeType, User

logger = logging.getLogger(__name__)


async def get_promocode_by_code(db: AsyncSession, code: str) -> Optional[PromoCode]:
    result = await db.execute(
        select(PromoCode)
        .options(
            selectinload(PromoCode.uses),
            selectinload(PromoCode.promo_group)
        )
        .where(PromoCode.code == code.upper())
    )
    return result.scalar_one_or_none()


async def get_promocode_by_id(db: AsyncSession, promo_id: int) -> Optional[PromoCode]:
    """
    Получает промокод по ID с eager loading всех связанных данных.
    Используется для избежания lazy loading в async контексте.
    """
    result = await db.execute(
        select(PromoCode)
        .options(
            selectinload(PromoCode.uses),
            selectinload(PromoCode.promo_group)
        )
        .where(PromoCode.id == promo_id)
    )
    return result.scalar_one_or_none()


async def check_promocode_validity(db: AsyncSession, code: str) -> dict:
    """
    Проверяет существование и валидность промокода без активации.
    Возвращает словарь с информацией о промокоде.
    """
    promocode = await get_promocode_by_code(db, code)

    if not promocode:
        return {"valid": False, "error": "not_found", "promocode": None}

    if not promocode.is_valid:
        if promocode.current_uses >= promocode.max_uses:
            return {"valid": False, "error": "used", "promocode": None}
        else:
            return {"valid": False, "error": "expired", "promocode": None}

    return {"valid": True, "error": None, "promocode": promocode}


async def create_promocode(
    db: AsyncSession,
    code: str,
    type: PromoCodeType,
    balance_bonus_kopeks: int = 0,
    subscription_days: int = 0,
    max_uses: int = 1,
    valid_until: Optional[datetime] = None,
    created_by: Optional[int] = None,
    promo_group_id: Optional[int] = None,
    description: Optional[str] = None
) -> PromoCode:

    promocode = PromoCode(
        code=code.upper(),
        type=type.value,
        balance_bonus_kopeks=balance_bonus_kopeks,
        subscription_days=subscription_days,
        max_uses=max_uses,
        valid_until=valid_until,
        created_by=created_by,
        promo_group_id=promo_group_id,
        description=description
    )
    
    db.add(promocode)
    await db.commit()
    await db.refresh(promocode)

    if promo_group_id:
        logger.info(f"✅ Создан промокод: {code} с промогруппой ID {promo_group_id}")
    else:
        logger.info(f"✅ Создан промокод: {code}")
    return promocode


async def use_promocode(
    db: AsyncSession,
    promocode_id: int,
    user_id: int
) -> bool:

    try:
        promocode = await get_promocode_by_id(db, promocode_id)
        if not promocode:
            return False
        
        usage = PromoCodeUse(
            promocode_id=promocode_id,
            user_id=user_id
        )
        db.add(usage)
        
        promocode.current_uses += 1
        
        await db.commit()
        
        logger.info(f"✅ Промокод {promocode.code} использован пользователем {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка использования промокода: {e}")
        await db.rollback()
        return False


async def check_user_promocode_usage(
    db: AsyncSession,
    user_id: int,
    promocode_id: int
) -> bool:
    
    result = await db.execute(
        select(PromoCodeUse).where(
            and_(
                PromoCodeUse.user_id == user_id,
                PromoCodeUse.promocode_id == promocode_id
            )
        )
    )
    return result.scalar_one_or_none() is not None



async def create_promocode_use(db: AsyncSession, promocode_id: int, user_id: int) -> PromoCodeUse:
    promocode_use = PromoCodeUse(
        promocode_id=promocode_id,
        user_id=user_id,
        used_at=datetime.utcnow()
    )
    
    db.add(promocode_use)
    await db.commit()
    await db.refresh(promocode_use)
    
    logger.info(f"📝 Записано использование промокода {promocode_id} пользователем {user_id}")
    return promocode_use


async def get_promocode_use_by_user_and_code(
    db: AsyncSession, 
    user_id: int, 
    promocode_id: int
) -> Optional[PromoCodeUse]:
    result = await db.execute(
        select(PromoCodeUse).where(
            and_(
                PromoCodeUse.user_id == user_id,
                PromoCodeUse.promocode_id == promocode_id
            )
        )
    )
    return result.scalar_one_or_none()


async def get_user_promocodes(db: AsyncSession, user_id: int) -> List[PromoCodeUse]:
    result = await db.execute(
        select(PromoCodeUse)
        .where(PromoCodeUse.user_id == user_id)
        .order_by(PromoCodeUse.used_at.desc())
    )
    return result.scalars().all()



async def get_promocodes_list(
    db: AsyncSession,
    offset: int = 0,
    limit: int = 50,
    is_active: Optional[bool] = None
) -> List[PromoCode]:

    query = select(PromoCode).options(
        selectinload(PromoCode.uses),
        selectinload(PromoCode.promo_group)
    )

    if is_active is not None:
        query = query.where(PromoCode.is_active == is_active)

    query = query.order_by(PromoCode.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


async def get_promocodes_count(
    db: AsyncSession,
    is_active: Optional[bool] = None
) -> int:
    
    query = select(func.count(PromoCode.id))
    
    if is_active is not None:
        query = query.where(PromoCode.is_active == is_active)
    
    result = await db.execute(query)
    return result.scalar()


async def update_promocode(
    db: AsyncSession,
    promocode: PromoCode,
    **kwargs
) -> PromoCode:
    
    for field, value in kwargs.items():
        if hasattr(promocode, field):
            setattr(promocode, field, value)
    
    promocode.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(promocode)
    
    return promocode


async def delete_promocode(db: AsyncSession, promocode: PromoCode) -> bool:
    try:
        from sqlalchemy import delete as sql_delete
        
        await db.execute(
            sql_delete(PromoCodeUse).where(PromoCodeUse.promocode_id == promocode.id)
        )
        
        await db.delete(promocode)
        await db.commit()
        
        logger.info(f"🗑️ Удален промокод: {promocode.code}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка удаления промокода: {e}")
        await db.rollback()
        return False


async def get_promocode_statistics(db: AsyncSession, promocode_id: int) -> dict:
    
    total_uses_result = await db.execute(
        select(func.count(PromoCodeUse.id))
        .where(PromoCodeUse.promocode_id == promocode_id)
    )
    total_uses = total_uses_result.scalar()
    
    today = datetime.utcnow().date()
    today_uses_result = await db.execute(
        select(func.count(PromoCodeUse.id))
        .where(
            and_(
                PromoCodeUse.promocode_id == promocode_id,
                PromoCodeUse.used_at >= today
            )
        )
    )
    today_uses = today_uses_result.scalar()
    
    recent_uses_result = await db.execute(
        select(PromoCodeUse, User)
        .join(User, PromoCodeUse.user_id == User.id)
        .where(PromoCodeUse.promocode_id == promocode_id)
        .order_by(PromoCodeUse.used_at.desc())
        .limit(10)
    )
    recent_uses_data = recent_uses_result.all()
    
    recent_uses = []
    for use, user in recent_uses_data:
        use.user_username = user.username
        use.user_full_name = user.full_name
        use.user_telegram_id = user.telegram_id
        recent_uses.append(use)
    
    return {
        "total_uses": total_uses,
        "today_uses": today_uses,
        "recent_uses": recent_uses
    }

