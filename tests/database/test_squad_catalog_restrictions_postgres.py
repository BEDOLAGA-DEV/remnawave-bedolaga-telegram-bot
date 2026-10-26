"""Catalog deletion must never turn a restricted tariff into all-server access."""

import pytest

from app.database.crud.server_squad import get_effective_tariff_squad_uuids, sync_with_remnawave
from app.database.models import Base, ServerSquad, Tariff
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('active', [False, True])
async def test_last_assigned_squad_deletion_preserves_restriction_and_disables_sales(postgres_database, active):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                ServerSquad(squad_uuid='deleted', display_name='Deleted', original_name='Deleted', is_available=True),
                ServerSquad(squad_uuid='other', display_name='Other', original_name='Other', is_available=True),
            ]
        )
        tariff = Tariff(name='Restricted', is_active=active, allowed_squads=['deleted'], period_prices={'30': 1000})
        db.add(tariff)
        await db.commit()
        assert await sync_with_remnawave(db, [{'uuid': 'other', 'name': 'Other'}]) == (0, 0, 1)
        await db.refresh(tariff)
        assert tariff.is_active is False
        assert tariff.allowed_squads == ['deleted']
        assert await get_effective_tariff_squad_uuids(db, tariff.allowed_squads) == ['deleted']
        # Repeated synchronization must not silently re-enable the tariff.
        await sync_with_remnawave(db, [{'uuid': 'other', 'name': 'Other'}])
        await db.refresh(tariff)
        assert tariff.is_active is False
        assert tariff.allowed_squads == ['deleted']


async def test_partial_deletion_and_explicit_all_servers_keep_their_meaning(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                ServerSquad(squad_uuid='deleted', display_name='Deleted', original_name='Deleted', is_available=True),
                ServerSquad(squad_uuid='live', display_name='Live', original_name='Live', is_available=True),
            ]
        )
        restricted = Tariff(name='Restricted', is_active=True, allowed_squads=['deleted', 'live'])
        unrestricted = Tariff(name='All', is_active=True, allowed_squads=[])
        db.add_all([restricted, unrestricted])
        await db.commit()
        await sync_with_remnawave(db, [{'uuid': 'live', 'name': 'Live'}])
        await db.refresh(restricted)
        await db.refresh(unrestricted)
        assert restricted.is_active is True
        assert restricted.allowed_squads == ['live']
        assert unrestricted.is_active is True
        assert unrestricted.allowed_squads == []
        assert await get_effective_tariff_squad_uuids(db, unrestricted.allowed_squads) == ['live']
