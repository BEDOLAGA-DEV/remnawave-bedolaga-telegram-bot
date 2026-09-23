"""CRUD промогрупп ставит пересчёт людей по тратам — на PostgreSQL.

Кабинет, Telegram-админка и внешний API создают, правят и удаляют группы через
одни и те же функции CRUD, поэтому пересчёт ставится именно там. Ставится он
только когда изменилось то, от чего зависит выдача: появился, изменился или
исчез порог, либо люди удалённой группы вернулись в базовую.
"""

from __future__ import annotations

import pytest

from app.database.crud.promo_group import create_promo_group, delete_promo_group, update_promo_group
from app.database.models import Base
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
DISCOUNTS = {'server_discount_percent': 0, 'traffic_discount_percent': 0, 'device_discount_percent': 0}


@pytest.fixture
def scheduled(monkeypatch) -> list[str]:
    from app.services.promo_group_recalculation import promo_group_recalculation

    reasons: list[str] = []
    monkeypatch.setattr(promo_group_recalculation, 'schedule', lambda reason: reasons.append(reason) or True)
    return reasons


@pytest.mark.asyncio
async def test_creating_a_group_with_threshold_schedules_recalculation(postgres_database, scheduled):
    async with postgres_session(postgres_database, TABLES) as db:
        await create_promo_group(db, 'Путник', is_default=True, **DISCOUNTS)
        assert scheduled == [], 'группа без порога никого не переназначит'

        await create_promo_group(db, 'Властелин', auto_assign_total_spent_kopeks=1_500_000, **DISCOUNTS)

    assert scheduled == ['создана группа «Властелин» с порогом']


@pytest.mark.asyncio
async def test_only_a_threshold_change_schedules_recalculation_on_update(postgres_database, scheduled):
    async with postgres_session(postgres_database, TABLES) as db:
        await create_promo_group(db, 'Путник', is_default=True, **DISCOUNTS)
        group = await create_promo_group(db, 'Властелин', auto_assign_total_spent_kopeks=1_500_000, **DISCOUNTS)
        scheduled.clear()

        await update_promo_group(db, group, name='Властелин ✨')
        await update_promo_group(db, group, server_discount_percent=15)
        await update_promo_group(db, group, auto_assign_total_spent_kopeks=1_500_000)
        assert scheduled == [], 'имя, скидки и тот же порог — не повод'

        await update_promo_group(db, group, auto_assign_total_spent_kopeks=2_500_000)
        await update_promo_group(db, group, auto_assign_total_spent_kopeks=0)

    assert scheduled == ['изменён порог группы «Властелин ✨»'] * 2, 'подняли порог; сняли порог'


@pytest.mark.asyncio
async def test_deleting_a_group_schedules_recalculation_while_thresholds_remain(postgres_database, scheduled):
    async with postgres_session(postgres_database, TABLES) as db:
        await create_promo_group(db, 'Путник', is_default=True, **DISCOUNTS)
        old = await create_promo_group(db, 'Старая VIP', auto_assign_total_spent_kopeks=500_000, **DISCOUNTS)
        last = await create_promo_group(db, 'Властелин', auto_assign_total_spent_kopeks=1_500_000, **DISCOUNTS)
        scheduled.clear()

        assert await delete_promo_group(db, old) is True
        assert scheduled == ['удалена группа «Старая VIP»']

        assert await delete_promo_group(db, last) is True
        assert scheduled == ['удалена группа «Старая VIP»'], 'порогов не осталось — пересчитывать нечего'


@pytest.mark.asyncio
async def test_refused_deletion_of_default_group_schedules_nothing(postgres_database, scheduled):
    async with postgres_session(postgres_database, TABLES) as db:
        default = await create_promo_group(db, 'Путник', is_default=True, **DISCOUNTS)
        await create_promo_group(db, 'Властелин', auto_assign_total_spent_kopeks=1_500_000, **DISCOUNTS)
        scheduled.clear()

        assert await delete_promo_group(db, default) is False

    assert scheduled == []
