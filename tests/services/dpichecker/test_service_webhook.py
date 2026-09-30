"""Разбор событий вебхука DPI//CHECKER в фасаде: итог ручной проверки обновляет строку без уведомления,
отмена — возврат, повтор доставки — тишина, чужой номер — тишина, monitor.run будит обходчик,
probe.done дописывает цену трафика."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from app.services.dpichecker.service import DpiCheckerService
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


def _fx(name):
    return load_dpichecker_fixture(name)['body']


async def _row(db, *, kind, remote_id, cost='0.0400'):
    admin = User(telegram_id=900 + remote_id % 50, first_name='w', language='ru', status='active')
    db.add(admin)
    await db.flush()
    row = await crud.create_action(
        db,
        kind=kind,
        admin_user_id=admin.id,
        check_type='ip' if kind == crud.KIND_CHECK else None,
        location='russia',
        pop_count=10,
        resource_count=1,
        source='paste',
        source_ref=None,
        label='x',
        targets=[],
        request={},
    )
    row.remote_id, row.status, row.cost_usd = remote_id, 'pending', Decimal(cost)
    await db.commit()
    return row


def _service(db, pokes):
    service = DpiCheckerService(api_factory=lambda: None)
    service.poke_monitor = pokes.append
    return service


async def test_manual_check_completion_updates_row(postgres_database):
    payload = _fx('webhook_check_completed')
    pokes: list[int] = []
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _row(db, kind=crud.KIND_CHECK, remote_id=payload['check']['id'])
        await _service(db, pokes).handle_webhook_in(db, event='check.completed', delivery_id=3, payload=payload)
        await db.refresh(row)
        assert row.status == 'completed' and row.delivery_ids == [3]
    assert pokes == []


async def test_cancelled_event_sets_refund(postgres_database):
    payload = _fx('webhook_check_cancelled')
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _row(db, kind=crud.KIND_CHECK, remote_id=payload['check']['id'])
        await _service(db, []).handle_webhook_in(db, event='check.cancelled', delivery_id=2, payload=payload)
        await db.refresh(row)
        assert (row.status, row.refunded_usd) == ('cancelled', Decimal('0.0400'))


async def test_repeated_delivery_is_ignored(postgres_database):
    payload = _fx('webhook_check_completed')
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _row(db, kind=crud.KIND_CHECK, remote_id=payload['check']['id'])
        service = _service(db, [])
        await service.handle_webhook_in(db, event='check.completed', delivery_id=3, payload=payload)
        row.status = 'pending'
        await db.commit()
        await service.handle_webhook_in(db, event='check.completed', delivery_id=3, payload=payload)
        await db.refresh(row)
        assert row.status == 'pending'


async def test_unknown_remote_check_is_quiet(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        await _service(db, []).handle_webhook_in(
            db, event='check.completed', delivery_id=9, payload=_fx('webhook_check_completed')
        )
        assert (await crud.list_actions(db))[1] == 0


async def test_monitor_run_pokes_watch(postgres_database):
    pokes: list[int] = []
    async with postgres_session(postgres_database, TABLES) as db:
        await _service(db, pokes).handle_webhook_in(
            db, event='monitor.run', delivery_id=6, payload=_fx('webhook_monitor_run')
        )
    assert pokes == [77]


async def test_probe_done_adds_traffic_cost(postgres_database):
    payload = _fx('webhook_probe_done')
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _row(db, kind=crud.KIND_PROBE, remote_id=payload['scan']['id'], cost='1.0000')
        await _service(db, []).handle_webhook_in(db, event='probe.done', delivery_id=8, payload=payload)
        await db.refresh(row)
        assert row.status == 'done' and row.cost_usd == Decimal('1.0331')


async def test_unknown_event_is_quiet(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        await _service(db, []).handle_webhook_in(db, event='something.new', delivery_id=1, payload={})
