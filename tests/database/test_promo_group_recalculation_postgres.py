"""Пересчёт промогрупп по тратам для всех людей — на PostgreSQL, боевой CRUD.

Автоназначение промогруппы срабатывало только в момент оплаты подписки.
Оператор удалил старые группы и завёл новые с порогами — и 5913 человек
остались в базовой «Путник», хотя по тратам давно заслужили «Властелина»:
никто из них ничего не оплачивал после смены групп. Пересчёт прогоняет то же
правило, что и оплата, по всем, у кого есть траты, — без уведомления админам
на каждого человека.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.crud.promo_group import delete_promo_group, update_promo_group
from app.database.models import Base, PromoGroup, Transaction, TransactionType, User, UserPromoGroup
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)

RUB = 100
THRESHOLDS = {
    'Страж': 1_000 * RUB,
    'Рыцарь': 2_500 * RUB,
    'Герой': 5_000 * RUB,
    'Чемпион': 10_000 * RUB,
    'Властелин': 15_000 * RUB,
    'Легенда': 20_000 * RUB,
}


@pytest.fixture(autouse=True)
def silence_per_user_admin_notifications(monkeypatch):
    """Пересчёт не должен слать админам уведомление на каждого человека."""
    mock = AsyncMock()
    monkeypatch.setattr('app.services.promo_group_assignment._notify_admins_about_auto_assignment', mock)
    return mock


@pytest.fixture(autouse=True)
def no_background_recalculation(monkeypatch):
    """CRUD групп ставит пересчёт в фон — здесь пересчёт вызывается руками."""
    from app.services.promo_group_recalculation import promo_group_recalculation

    monkeypatch.setattr(promo_group_recalculation, 'schedule', lambda reason: False)


async def _group(db, name: str, *, threshold: int | None = None, is_default: bool = False) -> PromoGroup:
    group = PromoGroup(name=name, priority=0, auto_assign_total_spent_kopeks=threshold, is_default=is_default)
    db.add(group)
    await db.flush()
    return group


async def _user(db, telegram_id: int, default: PromoGroup, *, spent: int = 0, gift: int = 0) -> User:
    user = User(telegram_id=telegram_id, first_name=str(telegram_id), language='ru', status='active')
    user.promo_group_id = default.id
    db.add(user)
    await db.flush()
    db.add(UserPromoGroup(user_id=user.id, promo_group_id=default.id, assigned_by='system'))
    if spent:
        db.add(
            Transaction(
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                amount_kopeks=-spent,
                is_completed=True,
            )
        )
    if gift:
        db.add(
            Transaction(
                user_id=user.id, type=TransactionType.GIFT_PAYMENT.value, amount_kopeks=-gift, is_completed=True
            )
        )
    return user


async def _link(db, user: User, group: PromoGroup, assigned_by: str) -> None:
    db.add(UserPromoGroup(user_id=user.id, promo_group_id=group.id, assigned_by=assigned_by))
    user.promo_group_id = group.id
    user.auto_promo_group_assigned = assigned_by == 'auto'
    user.auto_promo_group_threshold_kopeks = group.auto_assign_total_spent_kopeks or 0


async def _members(db) -> dict[str, set[int]]:
    """Кто в какой группе по колонке users.promo_group_id — то, что видят кабинет и цены."""
    rows = await db.execute(select(PromoGroup.name, User.telegram_id).join(User, User.promo_group_id == PromoGroup.id))
    members: dict[str, set[int]] = {}
    for name, telegram_id in rows.all():
        members.setdefault(name, set()).add(telegram_id)
    return members


async def _links(db, user: User) -> set[tuple[str, str]]:
    rows = await db.execute(
        select(PromoGroup.name, UserPromoGroup.assigned_by)
        .join(PromoGroup, PromoGroup.id == UserPromoGroup.promo_group_id)
        .where(UserPromoGroup.user_id == user.id)
    )
    return set(rows.all())


async def _seed_owner_scenario(db) -> dict[str, User | PromoGroup]:
    putnik = await _group(db, 'Путник', is_default=True)
    old_vip = await _group(db, 'Старая VIP', threshold=5_000 * RUB)
    partner = await _group(db, 'Партнёр')
    tiers = {name: await _group(db, name, threshold=threshold) for name, threshold in THRESHOLDS.items()}

    users = {
        'vlastelin': await _user(db, 1, putnik, spent=19_131 * RUB),
        'knight': await _user(db, 2, putnik, spent=3_000 * RUB),
        'low': await _user(db, 3, putnik, spent=500 * RUB),
        'newcomer': await _user(db, 4, putnik),
        'gift_only': await _user(db, 5, putnik, gift=30_000 * RUB),
        'stale': await _user(db, 6, putnik, spent=12_000 * RUB),
        'manual': await _user(db, 7, putnik, spent=11_000 * RUB),
    }
    await _link(db, users['stale'], old_vip, 'auto')
    await _link(db, users['manual'], partner, 'admin')
    await db.commit()
    return {**users, 'putnik': putnik, 'old_vip': old_vip, 'partner': partner, **tiers}


@pytest.mark.asyncio
async def test_recalculation_moves_everyone_to_the_group_their_spending_earned(
    postgres_database, silence_per_user_admin_notifications
):
    from app.services.promo_group_recalculation import recalculate_promo_groups

    async with postgres_session(postgres_database, TABLES) as db:
        seed = await _seed_owner_scenario(db)

        assert await delete_promo_group(db, seed['old_vip']) is True
        assert (await _members(db))['Путник'] == {1, 2, 3, 4, 5, 6}, 'удаление переводит в базовую — как у владельца'

        result = await recalculate_promo_groups(db)

        assert await _members(db) == {
            'Властелин': {1},
            'Рыцарь': {2},
            'Путник': {3, 4, 5},
            'Чемпион': {6, 7},
        }
        assert await _links(db, seed['vlastelin']) == {('Путник', 'system'), ('Властелин', 'auto')}
        assert await _links(db, seed['stale']) == {('Путник', 'system'), ('Чемпион', 'auto')}
        assert await _links(db, seed['manual']) == {('Путник', 'system'), ('Партнёр', 'admin'), ('Чемпион', 'auto')}
        assert await _links(db, seed['low']) == {('Путник', 'system')}
        assert await _links(db, seed['gift_only']) == {('Путник', 'system')}, 'подарок другому — не личные траты'

        assert (result.checked, result.changed) == (5, 4), 'проверены только платившие; изменены четверо'
        silence_per_user_admin_notifications.assert_not_awaited()

        again = await recalculate_promo_groups(db)
        assert (again.checked, again.changed) == (5, 0), 'повторный прогон ничего не трогает'


@pytest.mark.asyncio
async def test_raised_threshold_moves_user_down_to_the_group_he_still_earns(postgres_database):
    from app.services.promo_group_recalculation import recalculate_promo_groups

    async with postgres_session(postgres_database, TABLES) as db:
        seed = await _seed_owner_scenario(db)
        await _link(db, seed['vlastelin'], seed['Властелин'], 'auto')
        await db.commit()

        await update_promo_group(db, seed['Властелин'], auto_assign_total_spent_kopeks=25_000 * RUB)
        result = await recalculate_promo_groups(db)

        assert (await _members(db))['Чемпион'] == {1, 6, 7}
        assert await _links(db, seed['vlastelin']) == {('Путник', 'system'), ('Чемпион', 'auto')}
        assert result.changed >= 1


@pytest.mark.asyncio
async def test_without_auto_groups_nothing_is_checked(postgres_database):
    from app.services.promo_group_recalculation import recalculate_promo_groups

    async with postgres_session(postgres_database, TABLES) as db:
        putnik = await _group(db, 'Путник', is_default=True)
        await _user(db, 1, putnik, spent=19_131 * RUB)
        await db.commit()

        result = await recalculate_promo_groups(db)

        assert (result.checked, result.changed) == (0, 0)
        assert (await _members(db)) == {'Путник': {1}}
