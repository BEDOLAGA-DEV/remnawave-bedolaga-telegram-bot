"""Своя запись DPI//CHECKER на PostgreSQL: уникальный ключ идемпотентности, поиск по (вид, номер у сервиса),
дедуп доставок вебхука, траты по админам с учётом возвратов, фильтры истории."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


async def _admin(db, telegram_id: int = 777) -> User:
    user = User(telegram_id=telegram_id, first_name='admin', language='ru', status='active')
    db.add(user)
    await db.flush()
    return user


async def _action(db, admin: User, **extra):
    fields = {
        'kind': crud.KIND_CHECK,
        'admin_user_id': admin.id,
        'check_type': 'ip',
        'location': 'russia',
        'pop_count': 10,
        'resource_count': 1,
        'source': 'paste',
        'source_ref': None,
        'label': 'google.com',
        'targets': [{'value': 'google.com', 'name': 'google.com'}],
        'request': {'resources': ['google.com']},
    }
    return await crud.create_action(db, **{**fields, **extra})


async def test_new_action_gets_unique_key_and_submitting(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        first, second = await _action(db, admin), await _action(db, admin)
        assert first.status == 'submitting'
        assert len(first.idempotency_key) == 32
        assert first.idempotency_key != second.idempotency_key


async def test_found_by_kind_and_remote_id(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _action(db, admin)
        action.remote_id = 5309
        await db.flush()
        assert (await crud.get_by_remote(db, crud.KIND_CHECK, 5309)).id == action.id
        assert await crud.get_by_remote(db, crud.KIND_MONITOR, 5309) is None


async def test_delivery_is_claimed_once(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _action(db, admin, kind=crud.KIND_MONITOR)
        assert await crud.claim_delivery(db, action, 3) is True
        assert await crud.claim_delivery(db, action, 3) is False
        assert await crud.claim_delivery(db, action, 4) is True
        await db.commit()
        await db.refresh(action)
        assert action.delivery_ids == [3, 4]


async def test_spend_by_admin_subtracts_refunds(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        paid = await _action(db, admin)
        paid.cost_usd = Decimal('0.0400')
        refunded = await _action(db, admin)
        refunded.cost_usd, refunded.refunded_usd = Decimal('0.0400'), Decimal('0.0400')
        await _action(db, admin)  # запуск не дошёл до сервиса — цены нет, в траты не входит
        await db.flush()
        assert await crud.spend_by_admin(db) == [(admin.id, Decimal('0.0400'))]


async def test_list_filters_by_kind_type_and_admin(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        other = await _admin(db, telegram_id=778)
        await _action(db, admin)
        await _action(db, admin, check_type='vpn')
        await _action(db, other)
        await _action(db, admin, kind=crud.KIND_NOISY, check_type=None)
        items, total = await crud.list_actions(db, kind=crud.KIND_CHECK, check_type='ip', admin_user_id=admin.id)
        assert total == 1 and items[0].check_type == 'ip' and items[0].admin_user_id == admin.id
        _, all_checks = await crud.list_actions(db, kind=crud.KIND_CHECK)
        assert all_checks == 3


async def test_same_remote_id_allowed_across_kinds_but_not_within(postgres_database):
    from sqlalchemy.exc import IntegrityError

    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        check = await _action(db, admin)
        noisy = await _action(db, admin, kind=crud.KIND_NOISY, check_type=None)
        check.remote_id = noisy.remote_id = 88
        await db.flush()
        dup = await _action(db, admin)
        dup.remote_id = 88
        with pytest.raises(IntegrityError):
            await db.flush()
