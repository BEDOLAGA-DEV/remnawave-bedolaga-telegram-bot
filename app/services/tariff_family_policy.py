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


class ActiveTariffFamilyConflict(ValueError):
    """Для нового тарифа уже занята живая подписка этого семейства."""


async def require_available_entitlement_family(
    db: AsyncSession,
    *,
    user_id: int,
    tariff_id: int,
    current_subscription_id: int | None = None,
    conversion_trial_id: int | None = None,
) -> None:
    """Предплатёжный отказ для второго Main, если это не renewal/trial conversion.

    Вызывающий получает `current_subscription_id` лишь после проверки владения
    подпиской, а `conversion_trial_id` — из штатного серверного resolver'а.
    Обязательная блокировка User и DB-триггеры обеспечиваются отдельно.
    """
    conflict = await find_live_entitlement_family_conflict(
        db,
        user_id=user_id,
        tariff_id=tariff_id,
        current_subscription_id=current_subscription_id,
    )
    if conflict is not None and (
        conversion_trial_id is None or not conflict.is_trial or conflict.id != conversion_trial_id
    ):
        raise ActiveTariffFamilyConflict('Уже есть живая подписка этого семейства; смените тариф существующей подписки')
