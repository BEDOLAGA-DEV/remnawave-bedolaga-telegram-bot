from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SavedPaymentMethod


logger = structlog.get_logger(__name__)


DEFAULT_PROVIDER = 'yookassa'


def _resolve_provider_args(
    provider: str | None,
    provider_token: str | None,
    yookassa_payment_method_id: str | None,
) -> tuple[str, str]:
    """Normalise legacy callers that pass ``yookassa_payment_method_id``.

    Returns ``(provider, provider_token)``. Raises ``ValueError`` if the caller
    supplied no usable identifier.
    """
    if provider_token:
        return provider or DEFAULT_PROVIDER, provider_token
    if yookassa_payment_method_id:
        return provider or DEFAULT_PROVIDER, yookassa_payment_method_id
    raise ValueError('provider_token (or yookassa_payment_method_id) is required')


async def create_saved_payment_method(
    db: AsyncSession,
    user_id: int,
    provider_token: str | None = None,
    *,
    provider: str | None = None,
    yookassa_payment_method_id: str | None = None,
    method_type: str = 'bank_card',
    card_first6: str | None = None,
    card_last4: str | None = None,
    card_type: str | None = None,
    card_expiry_month: str | None = None,
    card_expiry_year: str | None = None,
    title: str | None = None,
    valid_thru: datetime | None = None,
) -> SavedPaymentMethod | None:
    """Создаёт или реактивирует сохранённый метод оплаты.

    Provider-agnostic. Either pass ``provider_token`` positionally together with
    ``provider=...`` (new style), or keep using the legacy
    ``yookassa_payment_method_id=...`` keyword (treated as YooKassa).
    """

    provider_name, token = _resolve_provider_args(provider, provider_token, yookassa_payment_method_id)

    update_values: dict[str, Any] = {
        'is_active': True,
        'method_type': method_type,
        'card_first6': card_first6,
        'card_last4': card_last4,
        'card_type': card_type,
        'card_expiry_month': card_expiry_month,
        'card_expiry_year': card_expiry_year,
        'title': title,
        'provider': provider_name,
        'provider_token': token,
        'valid_thru': valid_thru,
        'updated_at': datetime.now(UTC),
    }
    if provider_name == DEFAULT_PROVIDER:
        # keep legacy column in sync for callers that still read it
        update_values['yookassa_payment_method_id'] = token

    # Reactivate / update existing row if (user_id, provider, provider_token)
    # already exists; fall back to the legacy YooKassa column for rows that
    # have not been backfilled yet.
    match = or_(
        (SavedPaymentMethod.provider == provider_name) & (SavedPaymentMethod.provider_token == token),
        SavedPaymentMethod.yookassa_payment_method_id == token,
    )
    result = await db.execute(
        update(SavedPaymentMethod)
        .where(SavedPaymentMethod.user_id == user_id, match)
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
            provider=provider_name,
            method_type=method_type,
            card_last4=card_last4,
        )
        return reactivated

    method = SavedPaymentMethod(
        user_id=user_id,
        provider=provider_name,
        provider_token=token,
        yookassa_payment_method_id=token if provider_name == DEFAULT_PROVIDER else None,
        method_type=method_type,
        card_first6=card_first6,
        card_last4=card_last4,
        card_type=card_type,
        card_expiry_month=card_expiry_month,
        card_expiry_year=card_expiry_year,
        title=title,
        valid_thru=valid_thru,
    )

    db.add(method)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        logger.error(
            'Ошибка создания сохранённого метода оплаты',
            provider=provider_name,
            provider_token=token,
            user_id=user_id,
            e=e,
        )
        return None
    await db.refresh(method)

    logger.info(
        'Создан сохранённый метод оплаты',
        saved_method_id=method.id,
        user_id=user_id,
        provider=provider_name,
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


async def get_user_ids_with_active_payment_methods(
    db: AsyncSession,
    user_ids: list[int],
) -> set[int]:
    """Вернуть подмножество user_ids, у которых есть хотя бы один активный метод оплаты."""
    if not user_ids:
        return set()
    result = await db.execute(
        select(SavedPaymentMethod.user_id)
        .where(
            SavedPaymentMethod.user_id.in_(user_ids),
            SavedPaymentMethod.is_active == True,
        )
        .distinct()
    )
    return set(result.scalars().all())


async def get_payment_method_by_provider_token(
    db: AsyncSession,
    provider: str,
    provider_token: str,
    include_inactive: bool = False,
) -> SavedPaymentMethod | None:
    """Найти сохранённый метод по (provider, provider_token).

    Falls back to the legacy ``yookassa_payment_method_id`` column for
    not-yet-backfilled rows when ``provider='yookassa'``.
    """
    conditions = [
        (SavedPaymentMethod.provider == provider) & (SavedPaymentMethod.provider_token == provider_token),
    ]
    if provider == DEFAULT_PROVIDER:
        conditions.append(SavedPaymentMethod.yookassa_payment_method_id == provider_token)

    query = select(SavedPaymentMethod).where(or_(*conditions))
    if not include_inactive:
        query = query.where(SavedPaymentMethod.is_active == True)
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_payment_method_by_yookassa_id(
    db: AsyncSession,
    yookassa_payment_method_id: str,
    include_inactive: bool = False,
) -> SavedPaymentMethod | None:
    """Backwards-compatible alias for ``get_payment_method_by_provider_token``."""
    return await get_payment_method_by_provider_token(
        db,
        provider=DEFAULT_PROVIDER,
        provider_token=yookassa_payment_method_id,
        include_inactive=include_inactive,
    )


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
