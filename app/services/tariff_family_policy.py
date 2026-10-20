"""Поиск живой подписки того же явно заданного коммерческого семейства.

Это примитив для будущего Main-family XOR, а не самостоятельная защита покупки.
Вызывающий должен обеспечить транзакционный порядок, повторное чтение после
захвата блокировки пользователя и отсутствие финансового эффекта до решения.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription, SubscriptionStatus, Tariff


_LIVE_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
    SubscriptionStatus.LIMITED.value,
)


async def find_live_entitlement_family_conflict(
    db: AsyncSession,
    *,
    user_id: int,
    tariff_id: int,
    current_subscription_id: int | None = None,
) -> Subscription | None:
    """Возвращает другую живую подписку семьи покупаемого тарифа.

    `entitlement_family=NULL` не создаёт ограничений для legacy-тарифов.
    При отсутствующем тарифе отказывает явно; это не тот же случай, что NULL.
    `current_subscription_id` исключает продление/смену этой же записи.
    Функция **не блокирует** конкурирующие операции и **не списывает** деньги.
    """
    tariff = await db.scalar(select(Tariff).where(Tariff.id == tariff_id))
    if tariff is None:
        raise ValueError(f'Тариф {tariff_id} не найден')

    family = tariff.entitlement_family
    if family is None:
        return None

    query = (
        select(Subscription)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(_LIVE_STATUSES),
            Tariff.entitlement_family == family,
        )
        .order_by(Subscription.id)
        .limit(1)
    )
    if current_subscription_id is not None:
        query = query.where(Subscription.id != current_subscription_id)
    return (await db.execute(query)).scalar_one_or_none()
