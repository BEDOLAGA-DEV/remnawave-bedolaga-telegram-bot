"""Список пользователей админки: у каждой сортировки два направления.

Раньше направление было зашито: регистрация и деньги — «сначала больше/новее»,
окончание подписки — «сначала скорые». Посмотреть самых старых или тех, у кого
подписка кончается позже всех, было нельзя.

Без направления порядок прежний (кабинет старых версий шлёт только ``sort_by``).
С направлением первичный ключ разворачивается, а люди без значения — без
активности или без подписки — остаются внизу в обе стороны: пустые строки
наверху списка никому не нужны.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.database.crud.user import get_users_list
from app.database.models import SubscriptionStatus, Transaction, TransactionType, User
from tests.crud.test_users_list_filter_sort_matrix import SORTS, TABLES, _subscription, _user
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

NOW = datetime.now(UTC)

#: Все сортировки ручки, включая регистрацию (она — «без флага»).
ALL_SORTS: tuple[str | None, ...] = (None, *SORTS)

#: Порядок по умолчанию: low → mid → high, если ключ по умолчанию «сначала меньше».
NATURAL_ASCENDING = {'order_by_subscription_end', 'order_by_grace'}


async def _seed(db) -> None:
    """Три человека, у которых каждый ключ сортировки растёт в одном порядке: low < mid < high."""
    for rank, name in enumerate(('low', 'mid', 'high'), start=1):
        user = _user(
            100 + rank,
            name,
            created_at=NOW - timedelta(days=30 - rank),
            last_activity=NOW - timedelta(hours=10 - rank),
        )
        user.balance_kopeks = rank * 1000
        db.add(user)
        await db.flush()
        sub = _subscription(user, name)
        sub.end_date = NOW + timedelta(days=rank)
        sub.traffic_used_gb = float(rank * 10)
        # Открытый грейс: ключ «грейс кончается» — дата оверлея; закрытый в ключ не попадает.
        sub.grace_session_open = True
        sub.grace_overlay_expire_at = NOW + timedelta(days=rank, hours=6)
        db.add(sub)
        for _ in range(rank):
            db.add(
                Transaction(
                    user_id=user.id,
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-rank * 100,
                    is_completed=True,
                )
            )
    await db.commit()


def _flags(sort: str | None) -> dict[str, bool]:
    return {sort: True} if sort else {}


@pytest.mark.parametrize('sort', ALL_SORTS)
async def test_every_sort_goes_both_ways(postgres_database: str, sort: str | None) -> None:
    async with postgres_session(postgres_database, list(TABLES)) as db:
        await _seed(db)
        ascending = ['low', 'mid', 'high']
        natural = ascending if sort in NATURAL_ASCENDING else ascending[::-1]

        default = await get_users_list(db, **_flags(sort))
        assert [u.username for u in default] == natural, 'без направления порядок обязан остаться прежним'

        asc = await get_users_list(db, sort_descending=False, **_flags(sort))
        assert [u.username for u in asc] == ascending

        desc = await get_users_list(db, sort_descending=True, **_flags(sort))
        assert [u.username for u in desc] == ascending[::-1]


@pytest.mark.parametrize('sort', ['order_by_last_activity', 'order_by_subscription_end', 'order_by_grace'])
@pytest.mark.parametrize('descending', [False, True])
async def test_people_without_a_value_stay_at_the_bottom(postgres_database: str, sort: str, descending: bool) -> None:
    async with postgres_session(postgres_database, list(TABLES)) as db:
        await _seed(db)
        empty = _user(200, 'empty', created_at=NOW)
        db.add(empty)
        await db.flush()
        # None в конструкторе ORM подменяет умолчанием колонки (now()) — обнуляем запросом.
        await db.execute(update(User).where(User.id == empty.id).values(last_activity=None))
        # Истёкшая подписка не считается «окончанием активной» — ключ пустой.
        # Дата оверлея при ЗАКРЫТОМ грейсе — тоже не ключ: грейса у человека нет.
        expired = _subscription(empty, 'x')
        expired.status = SubscriptionStatus.EXPIRED.value
        expired.grace_session_open = False
        expired.grace_overlay_expire_at = NOW - timedelta(days=1)
        db.add(expired)
        await db.commit()

        users = await get_users_list(db, sort_descending=descending, **{sort: True})
        assert users[-1].username == 'empty'
