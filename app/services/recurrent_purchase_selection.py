"""Validate explicit recurring purchase selection before provider side effects."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import get_subscription_by_id_for_user, get_subscription_by_user_and_tariff
from app.database.models import Subscription


async def resolve_recurrent_purchase_selection(
    db: AsyncSession,
    *,
    user_id: int,
    tariff_id: int,
    subscription_id: int,
) -> Subscription:
    subscription = await get_subscription_by_id_for_user(db, subscription_id, user_id)
    if subscription is None:
        raise ValueError('Подписка не найдена')
    if subscription.tariff_id != tariff_id:
        # Binding/charge callbacks renew the existing row. Mutating its tariff
        # before payment would give unpaid access and corrupt cancellation flow.
        raise ValueError('Для перехода с classic или другого тарифа оплатите с баланса')
    active = await get_subscription_by_user_and_tariff(db, user_id, tariff_id)
    if active is not None and active.id != subscription.id:
        raise ValueError('Тариф уже активен в другой подписке — выберите её для продления')
    return subscription
