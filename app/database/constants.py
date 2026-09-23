from app.database.models import SubscriptionStatus


POSTGRES_INT4_MIN = -(2**31)
POSTGRES_INT4_MAX = 2**31 - 1

# Статусы, при которых подписка считается «живой» (индекс uq_subscriptions_user_tariff_active
# защищает именно эти статусы). Здесь, а не в crud.subscription: условиям напоминаний
# он нужен без всего, что тянет за собой CRUD (кольцо импортов через мониторинг).
ALIVE_SUBSCRIPTION_STATUSES: frozenset[str] = frozenset(
    {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.LIMITED.value,
    }
)
